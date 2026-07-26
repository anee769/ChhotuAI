"""
seed.py — generate catalogue.json, events.json, learning_day1/day60.json.

Deterministic (fixed seed) so `--demo` resets to a known state. Genuine variant
explosion so the judge sees the problem in the data itself. NO opening_stock in
the catalogue; NO current_stock anywhere. Quantities live only in the event log.

Run:  python backend/seed.py
"""
from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)
DATA = Path(os.environ.get(
    "CHHOTU_DATA_DIR", Path(__file__).resolve().parent.parent / "data"
))
TODAY = date(2026, 7, 26)
PERIOD_START = TODAY - timedelta(days=42)  # ~6 weeks
DEVANAGARI = {"tmt": "सरिया", "cement": "सीमेंट", "pipe": "पाइप"}


def piece_kg(d_mm: int) -> float:
    # standard 12 m bar weight = d^2/162 * 12
    return round((d_mm * d_mm) / 162.0 * 12.0, 2)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
def build_catalogue() -> list:
    """A deliberately SMALL, clean catalogue: 2 families, 2 brands, a couple of
    variants each — enough to show variant disambiguation, margin, udhaar,
    frozen/uncounted capital and invoice landed-cost, but tidy on the dashboard.

      1. Tata Tiscon TMT 12mm Fe500D   (sariya, the star SKU)
      2. Tata Tiscon TMT 16mm Fe500D   (sariya, second size)
      3. UltraTech OPC 53 Cement 50kg  (cement, counted + active)
      4. UltraTech PPC Cement 50kg     (cement, kept UNCOUNTED in the demo)
      5. Tata Tiscon TMT 20mm Fe500D   (sariya, counted but FROZEN — dead stock)
      6. UltraTech PSC Cement 50kg     (cement, counted but FROZEN — dead stock)
    """
    cat = []

    # --- TMT bars: one brand, one grade, three sizes (20mm kept as frozen stock) ---
    for dia, cost in [(12, 55.5), (16, 54.5), (20, 53.5)]:
        cat.append({
            "sku_id": f"TMT_{dia}_FE500D_TATA",
            "canonical": f"Tata Tiscon TMT Bar {dia}mm Fe500D",
            "family": "tmt",
            "attributes": {"diameter_mm": dia, "grade": "Fe500D", "brand": "Tata Tiscon"},
            "default_unit": "tonne",
            "units": {"kg": 1, "tonne": 1000, "piece": piece_kg(dia)},
            "gst_rate": 18,
            "opening_cost_per_kg": cost,
            "aliases": ["saria", "sariya", "सरिया", "tmt bar", "rod", "tmt",
                        f"{dia}mm rod", f"{dia} mm", "tata", "tiscon"],
        })

    # --- Cement: one brand, three types (PSC kept as frozen stock) ---
    for typ, tcode, cost in [("OPC 53", "OPC53", 415), ("PPC", "PPC", 385),
                              ("PSC", "PSC", 370)]:
        cat.append({
            "sku_id": f"CEM_ULTRATECH_{tcode}",
            "canonical": f"UltraTech {typ} Cement 50kg",
            "family": "cement",
            "attributes": {"brand": "UltraTech", "type": typ, "weight_kg": 50},
            "default_unit": "bori",
            "units": {"bori": 1, "tonne": 20, "kg": 0.02},
            "gst_rate": 28,
            "opening_cost_per_kg": cost,  # per bori (base unit)
            "aliases": ["cement", "सीमेंट", "bori", "bag", "ultratech",
                        typ.lower(), "ultratech cement"],
        })

    return cat


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def d(dt: date) -> str:
    return dt.isoformat()


def rec(dt: date, lag: int = 0) -> str:
    return (dt + timedelta(days=lag)).isoformat() + "T20:40:00"


def build_events(cat: list) -> list:
    events = []
    n = 0

    def add(**kw):
        nonlocal n
        n += 1
        kw.setdefault("event_id", f"evt_{n:04d}")
        events.append(kw)

    by_id = {s["sku_id"]: s for s in cat}

    TMT12, TMT16, TMT20 = "TMT_12_FE500D_TATA", "TMT_16_FE500D_TATA", "TMT_20_FE500D_TATA"
    OPC53, PPC, PSC = "CEM_ULTRATECH_OPC53", "CEM_ULTRATECH_PPC", "CEM_ULTRATECH_PSC"

    # UltraTech PPC is deliberately left UNCOUNTED (no baseline, no activity)
    # so the demo shows a "abhi tak gina nahi" SKU.
    # TMT 20mm and PSC cement are counted ONCE and then never sold/restocked —
    # they show up as frozen capital (dead stock, zero movement in 60 days).
    counted = [TMT12, TMT16, OPC53]
    frozen_only = [TMT20, PSC]

    # 1) opening_balance at period start for the 3 active + 2 frozen SKUs
    opening = {TMT12: (15.0, "tonne", "estimated"),
               TMT16: (10.0, "tonne", "estimated"),
               OPC53: (400, "bori", "exact"),
               TMT20: (1.8, "tonne", "exact"),
               PSC: (22, "bori", "exact")}
    for sid, (qty, unit, cp) in opening.items():
        add(type="opening_balance", sku_id=sid, qty=qty, unit=unit, rate=None,
            payment=None, occurred_on=d(PERIOD_START), precision="day",
            count_precision=cp, occurred_at=None, recorded_at=rec(PERIOD_START),
            confidence=0.9, source="voice_count",
            evidence={"transcript": f"opening stock {qty} {unit}"})

    # 2) Steel price rise mid-period on the star SKU (12mm): early delivery
    #    cheap, later delivery +8% -> powers the dashboard "steel rise" marker.
    for sid in (TMT12, TMT16):
        base = by_id[sid]["opening_cost_per_kg"]
        early = PERIOD_START + timedelta(days=6)
        late = PERIOD_START + timedelta(days=26)
        add(type="delivery", sku_id=sid, qty=4.0, unit="tonne",
            rate=round(base * 0.99, 1), payment="credit", occurred_on=d(early),
            precision="day", count_precision=None, occurred_at=None,
            recorded_at=rec(early), confidence=0.95, source="invoice_photo",
            evidence={"transcript": "delivery received"})
        add(type="delivery", sku_id=sid, qty=4.0, unit="tonne",
            rate=round(base * 1.08, 1), payment="credit", occurred_on=d(late),
            precision="day", count_precision=None, occurred_at=None,
            recorded_at=rec(late), confidence=0.95, source="invoice_photo",
            evidence={"transcript": "delivery received (price up)"})

    # cement delivery (no dramatic rise)
    dd = PERIOD_START + timedelta(days=12)
    add(type="delivery", sku_id=OPC53, qty=120, unit="bori",
        rate=by_id[OPC53]["opening_cost_per_kg"], payment="credit",
        occurred_on=d(dd), precision="day", count_precision=None,
        occurred_at=None, recorded_at=rec(dd), confidence=0.95,
        source="invoice_photo", evidence={"transcript": "delivery"})

    # 3) Sales across the period — cash/credit mix, healthy margin.
    for day_off in range(2, 42):
        dt = PERIOD_START + timedelta(days=day_off)
        for _ in range(random.randint(1, 2)):
            sid = random.choice(counted)
            sku = by_id[sid]
            cost = sku["opening_cost_per_kg"]
            markup = random.uniform(1.06, 1.16)
            if sku["family"] == "tmt":
                qty, unit = round(random.uniform(0.2, 0.8), 1), "tonne"
            else:
                qty, unit = random.randint(5, 20), "bori"
            pay = "credit" if random.random() < 0.4 else "cash"
            add(type="sale", sku_id=sid, qty=qty, unit=unit,
                rate=round(cost * markup, 1), payment=pay, occurred_on=d(dt),
                precision="exact" if day_off % 3 else "day", count_precision=None,
                occurred_at=(dt.isoformat() + "T13:20:00") if day_off % 3 else None,
                recorded_at=rec(dt), confidence=0.85, source="voice_live",
                evidence={"transcript": f"{qty} {unit} {sku['canonical']}"})

    # 4) TODAY's sales so the Today tab shows live numbers (cash + credit)
    today_sales = [
        (TMT12, 1.5, "tonne", "cash"),
        (OPC53, 30, "bori", "cash"),
        (TMT16, 0.8, "tonne", "credit"),
    ]
    for sid, qty, unit, pay in today_sales:
        cost = by_id[sid]["opening_cost_per_kg"]
        add(type="sale", sku_id=sid, qty=qty, unit=unit,
            rate=round(cost * 1.12, 1), payment=pay, occurred_on=d(TODAY),
            precision="exact", count_precision=None,
            occurred_at=TODAY.isoformat() + "T11:00:00", recorded_at=rec(TODAY),
            confidence=0.9, source="voice_live",
            evidence={"transcript": f"{qty} {unit} {by_id[sid]['canonical']}"})

    # a week-precision entry -> "plus N purani entries", excluded from today
    add(type="sale", sku_id=OPC53, qty=15, unit="bori",
        rate=round(by_id[OPC53]["opening_cost_per_kg"] * 1.1, 1), payment="cash",
        occurred_on=d(TODAY - timedelta(days=3)), precision="week",
        count_precision=None, occurred_at=None, recorded_at=rec(TODAY),
        confidence=0.6, source="voice_recall",
        evidence={"transcript": "pichle hafte kuch cement gaya tha"})

    # 5) one stock_take that DISAGREES with derived stock (reconciliation delta)
    add(type="stock_take", sku_id=OPC53, qty=38, unit="bori", rate=None,
        payment=None, occurred_on=d(TODAY - timedelta(days=1)), precision="day",
        count_precision="exact", occurred_at=None,
        recorded_at=rec(TODAY - timedelta(days=1)), confidence=0.92,
        source="voice_count",
        evidence={"transcript": "aaj gina to 38 bori nikla"})

    return events


# ---------------------------------------------------------------------------
# Learning states
# ---------------------------------------------------------------------------
def learning_day1() -> dict:
    return {"aliases_learned": [], "attribute_priors": [],
            "unit_priors": [], "corrections": []}


def learning_day60() -> dict:
    now = TODAY.isoformat()
    T12, T16 = "TMT_12_FE500D_TATA", "TMT_16_FE500D_TATA"
    OPC, PPC = "CEM_ULTRATECH_OPC53", "CEM_ULTRATECH_PPC"
    aliases = [
        ("barah mm", T12), ("solah mm", T16), ("tiscon barah", T12),
        ("tiscon solah", T16), ("tata barah mm", T12), ("tata solah mm", T16),
        ("chhota sariya", T12), ("mota sariya", T16), ("patla rod", T12),
        ("mota rod", T16), ("12 wala", T12), ("16 wala", T16),
        ("das mm", T12), ("sariya barah", T12), ("sariya solah", T16),
        ("ultratech 53", OPC), ("ultratech opc", OPC), ("opc cement", OPC),
        ("ppc cement", PPC), ("ultratech ppc", PPC), ("cement ultratech", OPC),
        ("teri cement", OPC), ("safed cement", PPC), ("bori cement", OPC),
    ]
    priors = [
        {"family": "tmt", "attribute": "diameter_mm", "value": 12, "count": 19},
        {"family": "tmt", "attribute": "grade", "value": "Fe500D", "count": 31},
        {"family": "tmt", "attribute": "brand", "value": "Tata Tiscon", "count": 22},
        {"family": "cement", "attribute": "brand", "value": "UltraTech", "count": 18},
        {"family": "cement", "attribute": "type", "value": "OPC 53", "count": 15},
    ]
    unit_priors = [
        {"sku_id": T12, "unit": "tonne", "count": 14},
        {"sku_id": OPC, "unit": "bori", "count": 26},
        {"sku_id": T16, "unit": "tonne", "count": 8},
    ]
    corrections = [
        {"spoken": "chhota sariya", "chosen_sku": T12,
         "rejected": [T16], "ts": now},
        {"spoken": "mota rod", "chosen_sku": T16,
         "rejected": [T12], "ts": now},
    ]
    return {
        "aliases_learned": [{"phrase": p, "sku_id": s, "confirmed_at": now}
                            for p, s in aliases],
        "attribute_priors": priors,
        "unit_priors": unit_priors,
        "corrections": corrections,
    }


def build_customers() -> list:
    return [
        {"customer_id": "cust_0001", "name": "Pankaj Sharma",
         "phone": "+919876543210", "created_at": "2026-06-18T11:20:00"},
        {"customer_id": "cust_0002", "name": "Manoj Sutar",
         "phone": "+919810234567", "created_at": "2026-06-22T16:05:00"},
        {"customer_id": "cust_0003", "name": "Amit Construction",
         "phone": "+919560112233", "created_at": "2026-07-02T10:10:00"},
        {"customer_id": "cust_0004", "name": "Rakesh Hardware",
         "phone": "+919999001122", "created_at": "2026-07-09T14:35:00"},
        {"customer_id": "cust_0005", "name": "Sunita Traders",
         "phone": "+919811223344", "created_at": "2026-07-14T12:00:00"},
        {"customer_id": "cust_0006", "name": "Deepak Contractor",
         "phone": "+919650778899", "created_at": "2026-07-20T17:15:00"},
    ]


def build_receivables() -> list:
    return [
        {"receivable_id": "recv_0001", "customer_id": "cust_0001",
         "amount": 48500.0, "deadline": "2026-07-28",
         "sale_event_ids": ["evt_0101"], "created_at": "2026-07-18T18:10:00",
         "status": "open"},
        {"receivable_id": "recv_0002", "customer_id": "cust_0002",
         "amount": 18200.0, "deadline": "2026-07-24",
         "sale_event_ids": ["evt_0102"], "created_at": "2026-07-12T13:25:00",
         "status": "open"},
        {"receivable_id": "recv_0003", "customer_id": "cust_0003",
         "amount": 76000.0, "deadline": "2026-08-05",
         "sale_event_ids": ["evt_0103"], "created_at": "2026-07-21T15:40:00",
         "status": "open"},
        {"receivable_id": "recv_0004", "customer_id": "cust_0004",
         "amount": 12600.0, "deadline": "2026-07-27",
         "sale_event_ids": ["evt_0104"], "created_at": "2026-07-19T12:15:00",
         "status": "open"},
        {"receivable_id": "recv_0005", "customer_id": "cust_0005",
         "amount": 31500.0, "deadline": "2026-07-30",
         "sale_event_ids": ["evt_0105"], "created_at": "2026-07-16T17:55:00",
         "status": "open"},
        {"receivable_id": "recv_0006", "customer_id": "cust_0006",
         "amount": 9400.0, "deadline": "2026-07-23",
         "sale_event_ids": ["evt_0106"], "created_at": "2026-07-15T11:30:00",
         "status": "open"},
    ]


def build_payments() -> list:
    return [
        {"payment_id": "pay_0001", "customer_id": "cust_0001",
         "amount": 15000.0, "paid_on": "2026-07-23", "note": "UPI",
         "created_at": "2026-07-23T19:05:00"},
        {"payment_id": "pay_0002", "customer_id": "cust_0002",
         "amount": 8200.0, "paid_on": "2026-07-20", "note": "Cash",
         "created_at": "2026-07-20T18:40:00"},
        {"payment_id": "pay_0003", "customer_id": "cust_0005",
         "amount": 31500.0, "paid_on": "2026-07-25", "note": "Bank transfer",
         "created_at": "2026-07-25T16:25:00"},
    ]


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    cat = build_catalogue()
    events = build_events(cat)
    day1 = learning_day1()

    def dump(name, obj):
        with open(DATA / name, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    dump("catalogue.json", cat)
    dump("events.json", events)
    dump("learning_day1.json", day1)
    dump("learning_day60.json", learning_day60())
    dump("learning.json", day1)  # active = day1
    dump("customers.json", build_customers())
    dump("receivables.json", build_receivables())
    dump("payments.json", build_payments())
    print(f"catalogue: {len(cat)} SKUs")
    print(f"events:    {len(events)}")
    print("learning:  day1 (empty) + day60 (~25 aliases) written")


if __name__ == "__main__":
    main()
