"""
ledger.py — derived state over the append-only event log (spec Section 3 & 4).

THE ONE RULE: stock is NEVER stored. It is replayed from events every time.
There is no current_stock field anywhere. A back-dated event inserts into the
sequence and everything downstream recomputes automatically.

Units: each SKU's `units` dict maps a unit name -> its size in the SKU's BASE
unit (the unit whose multiplier is 1). All internal math is in base units;
display converts back to default_unit. `opening_cost_per_kg` is the seeded cost
per BASE unit (kept as the spec's field name; it is the fallback cost basis).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Union

UNCOUNTED = "UNCOUNTED"  # sentinel — MUST never render as 0

# within-a-day application order (spec Section 3)
_TYPE_ORDER = {"opening_balance": 0, "stock_take": 0, "delivery": 1,
               "sale": 2, "adjustment": 3}


def _d(s: str) -> date:
    return datetime.fromisoformat(s).date() if "T" in str(s) else date.fromisoformat(str(s))


def base_unit(sku: dict) -> str:
    units = sku.get("units", {})
    for u, mult in units.items():
        if mult == 1:
            return u
    return sku.get("default_unit", "unit")


def to_base(qty: float, unit: str, sku: dict) -> float:
    units = sku.get("units", {})
    mult = units.get(unit)
    if mult is None:
        mult = units.get(sku.get("default_unit", ""), 1)
    return qty * mult


def from_base(qty_base: float, unit: str, sku: dict) -> float:
    mult = sku.get("units", {}).get(unit, 1) or 1
    return qty_base / mult


# ---------------------------------------------------------------------------
# Confidence  (spec Section 3)
# ---------------------------------------------------------------------------
def compute_confidence(base_conf: float, occurred_on: str, recorded_at: str,
                       precision: str) -> float:
    try:
        rec = _d(recorded_at)
    except Exception:
        rec = date.today()
    lag_days = max(0, (rec - _d(occurred_on)).days)
    lag_factor = max(0.6, 1.0 - 0.08 * lag_days)
    prec_factor = {"exact": 1.0, "day": 0.9, "week": 0.7, "unknown": 0.5}.get(precision, 0.9)
    return round(base_conf * lag_factor * prec_factor, 3)


# ---------------------------------------------------------------------------
# Stock replay  (three-valued: number or UNCOUNTED)
# ---------------------------------------------------------------------------
def _sorted_events(events: list) -> list:
    return sorted(events, key=lambda e: (_d(e["occurred_on"]),
                                         _TYPE_ORDER.get(e["type"], 5)))


def stock_at(sku: dict, events: list, as_of: Optional[date] = None) -> Union[float, str]:
    """
    Return stock in the SKU's default_unit as of `as_of`, or UNCOUNTED if no
    baseline (opening_balance / stock_take) has ever been established.
    """
    result = _stock_detail(sku, events, as_of)
    return result["qty"]


def _stock_detail(sku: dict, events: list, as_of: Optional[date] = None) -> dict:
    sku_events = [e for e in events if e.get("sku_id") == sku["sku_id"]]
    if as_of is not None:
        sku_events = [e for e in sku_events if _d(e["occurred_on"]) <= as_of]

    baseline_set = False
    qty_base = 0.0
    est = False  # any estimated baseline -> show ~
    for e in _sorted_events(sku_events):
        t = e["type"]
        q = to_base(float(e.get("qty", 0)), e.get("unit", base_unit(sku)), sku)
        if t in ("opening_balance", "stock_take"):
            baseline_set = True
            qty_base = q  # a baseline/observation overwrites the running derived value
            if e.get("count_precision") == "estimated":
                est = True
        elif t == "delivery":
            qty_base += q
        elif t == "sale":
            qty_base -= q
        elif t == "adjustment":
            qty_base += q  # signed
    if not baseline_set:
        return {"qty": UNCOUNTED, "estimated": False, "base": None}
    du = sku.get("default_unit", base_unit(sku))
    return {"qty": round(from_base(qty_base, du, sku), 3),
            "estimated": est, "base": qty_base, "unit": du}


def reconciliation_delta(sku: dict, events: list, take_event: dict) -> dict:
    """
    Compare a stock_take's observed qty against derived stock the moment BEFORE
    the take (spec Section 5). Positive gap = ledger higher than counted
    (unrecorded sales/wastage/pilferage). History is never overwritten.
    """
    on = _d(take_event["occurred_on"])
    # derived stock excluding this very take, up to that day
    others = [e for e in events if e.get("event_id") != take_event.get("event_id")]
    derived = _stock_detail(sku, others, on)
    observed = from_base(
        to_base(float(take_event["qty"]), take_event.get("unit", base_unit(sku)), sku),
        sku.get("default_unit", base_unit(sku)), sku)
    if derived["qty"] == UNCOUNTED:
        return {"observed": observed, "derived": None, "delta": None}
    return {"observed": round(observed, 3),
            "derived": derived["qty"],
            "delta": round(derived["qty"] - observed, 3),
            "unit": sku.get("default_unit")}


# ---------------------------------------------------------------------------
# Cost basis  (spec Section 4 — last purchase cost as of the sale date)
# ---------------------------------------------------------------------------
def landed_cost_as_of(sku: dict, events: list, on: date) -> Optional[float]:
    """
    Cost per BASE unit as of a date: the most recent delivery rate on/before the
    date (deliveries carry landed cost), else the seeded opening_cost_per_kg.
    Replacement-cost logic: use what it costs to restock today, not FIFO/avg.
    """
    deliveries = [e for e in events
                  if e.get("sku_id") == sku["sku_id"]
                  and e["type"] == "delivery"
                  and e.get("rate") is not None
                  and _d(e["occurred_on"]) <= on]
    if deliveries:
        latest = max(deliveries, key=lambda e: _d(e["occurred_on"]))
        return float(latest["rate"])  # rate is per base unit
    if sku.get("opening_cost_per_kg") is not None:
        return float(sku["opening_cost_per_kg"])
    if sku.get("landed_cost_per_kg") is not None:
        return float(sku["landed_cost_per_kg"])
    return None


# ---------------------------------------------------------------------------
# Margin  (spec Section 4 & 9.4) — works with ZERO counted SKUs
# ---------------------------------------------------------------------------
def margin_for_day(catalogue_by_sku: dict, events: list, day: date) -> dict:
    """
    Margin for sales that occurred on `day` with day/exact precision.
    Splits cash vs credit. week-precision items are excluded and counted
    separately as "plus N purani entries".
    """
    cash = credit = margin = 0.0
    provisional_extra = 0
    lines = []
    for e in events:
        if e["type"] != "sale":
            continue
        if _d(e["occurred_on"]) != day:
            continue
        if e.get("precision") == "week":
            provisional_extra += 1
            continue
        sku = catalogue_by_sku.get(e["sku_id"])
        if not sku:
            continue
        qty_base = to_base(float(e["qty"]), e.get("unit", base_unit(sku)), sku)
        sale_rate = float(e.get("rate")) if e.get("rate") is not None else None
        cost = landed_cost_as_of(sku, events, day)
        line_margin = None
        if sale_rate is not None and cost is not None:
            line_margin = (sale_rate - cost) * qty_base
            margin += line_margin
        revenue = (sale_rate * qty_base) if sale_rate is not None else 0.0
        if e.get("payment") == "credit":
            credit += revenue
        else:
            cash += revenue
        lines.append({
            "sku_id": e["sku_id"],
            "canonical": sku.get("canonical"),
            "qty": e["qty"], "unit": e.get("unit"),
            "rate": sale_rate, "cost": cost,
            "margin": round(line_margin, 2) if line_margin is not None else None,
            "payment": e.get("payment", "cash"),
        })
    return {
        "date": day.isoformat(),
        "margin": round(margin, 2),
        "cash": round(cash, 2),
        "credit": round(credit, 2),
        "provisional_extra": provisional_extra,
        "lines": lines,
    }
