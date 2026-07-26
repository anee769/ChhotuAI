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
    cat = []

    # --- TMT bars: genuine variant explosion (~24 SKUs) ---
    tmt_matrix = [
        # (diameter, [grades], [brands])
        (6,  ["Fe500"],          ["Tata Tiscon", "Jindal Panther"]),
        (8,  ["Fe500", "Fe500D"], ["Tata Tiscon", "Jindal Panther", "SRMB"]),
        (10, ["Fe500", "Fe500D"], ["Tata Tiscon", "Jindal Panther", "SRMB"]),
        (12, ["Fe500", "Fe500D"], ["Tata Tiscon", "Jindal Panther", "SRMB"]),
        (16, ["Fe500D"],          ["Tata Tiscon", "Jindal Panther", "SRMB"]),
        (20, ["Fe500D"],          ["Tata Tiscon", "SRMB"]),
        (25, ["Fe500D"],          ["Tata Tiscon", "SRMB"]),
    ]
    brand_code = {"Tata Tiscon": "TATA", "Jindal Panther": "JINDAL", "SRMB": "SRMB"}
    base_cost = {6: 62, 8: 58, 10: 55, 12: 52, 16: 51, 20: 50, 25: 50}
    for dia, grades, brands in tmt_matrix:
        for grade in grades:
            for brand in brands:
                gcode = grade.replace("Fe", "FE")
                sku_id = f"TMT_{dia}_{gcode}_{brand_code[brand]}"
                cost = base_cost[dia] + (1.5 if grade == "Fe500D" else 0) + \
                    ({"Tata Tiscon": 2, "Jindal Panther": 1, "SRMB": 0}[brand])
                cat.append({
                    "sku_id": sku_id,
                    "canonical": f"{brand} TMT Bar {dia}mm {grade}",
                    "family": "tmt",
                    "attributes": {"diameter_mm": dia, "grade": grade, "brand": brand},
                    "default_unit": "tonne",
                    "units": {"kg": 1, "tonne": 1000, "piece": piece_kg(dia)},
                    "gst_rate": 18,
                    "opening_cost_per_kg": round(cost, 1),
                    "aliases": ["saria", "sariya", "सरिया", "tmt bar", "rod", "tmt",
                                f"{dia}mm rod", f"{dia} mm", brand.split()[0].lower(),
                                brand_code[brand].lower()],
                })

    # --- Cement (~8) ---
    cem = [
        ("UltraTech", "OPC 53", 415), ("UltraTech", "PPC", 385),
        ("ACC", "OPC 43", 372), ("ACC", "PPC", 360),
        ("Ambuja", "OPC 53", 405), ("Ambuja", "PPC", 378),
        ("JK", "OPC 43", 365), ("JK", "PPC", 352),
    ]
    for brand, typ, cost in cem:
        bcode = brand.upper().replace(" ", "")
        tcode = typ.replace(" ", "")
        cat.append({
            "sku_id": f"CEM_{bcode}_{tcode}",
            "canonical": f"{brand} {typ} Cement 50kg",
            "family": "cement",
            "attributes": {"brand": brand, "type": typ, "weight_kg": 50},
            "default_unit": "bori",
            "units": {"bori": 1, "tonne": 20, "kg": 0.02},
            "gst_rate": 28,
            "opening_cost_per_kg": cost,  # per bori (base unit)
            "aliases": ["cement", "सीमेंट", "bori", "bag", brand.lower(),
                        typ.lower(), f"{brand.lower()} cement"],
        })

    # --- PVC pipe (~8) ---
    for size, sname in [(0.5, '½"'), (0.75, '¾"'), (1.0, '1"'),
                        (1.5, '1.5"'), (2.0, '2"')]:
        for cls, ccode, mult in [("Class 3", "C3", 1.0), ("Class 5", "C5", 1.35)]:
            if size in (1.5, 2.0) and cls == "Class 3":
                continue  # not every combo carried -> 8 SKUs
            base = 90 + size * 180
            cat.append({
                "sku_id": f"PIPE_{str(size).replace('.', '')}_{ccode}",
                "canonical": f'Supreme PVC Pipe {sname} {cls}',
                "family": "pipe",
                "attributes": {"size_inch": size, "class": cls},
                "default_unit": "piece",
                "units": {"piece": 1, "bundle": 10},
                "gst_rate": 18,
                "opening_cost_per_kg": round(base * mult, 1),  # per piece (base)
                "aliases": ["pipe", "पाइप", "paip", "pvc", f'{sname} pipe',
                            f"{size} inch pipe"],
            })

    # --- Fittings (~4) ---
    for name, code, cost in [("Elbow 1\"", "ELBOW_1", 28), ("Tee 1\"", "TEE_1", 34),
                             ("Coupler 1\"", "COUP_1", 18), ("Elbow 2\"", "ELBOW_2", 62)]:
        cat.append({
            "sku_id": f"FIT_{code}",
            "canonical": f"PVC {name}",
            "family": "fitting",
            "attributes": {},
            "default_unit": "piece",
            "units": {"piece": 1, "box": 50},
            "gst_rate": 18,
            "opening_cost_per_kg": cost,
            "aliases": ["fitting", name.lower(), "elbow", "tee", "coupler"],
        })

    # --- Fasteners (~2) ---
    for name, code, cost in [("Anchor Bolt 10mm", "ANCHOR_10", 12),
                             ("Wood Screw 2\"", "SCREW_2", 2)]:
        cat.append({
            "sku_id": f"FAST_{code}",
            "canonical": name,
            "family": "fastener",
            "attributes": {},
            "default_unit": "piece",
            "units": {"piece": 1, "box": 100},
            "gst_rate": 18,
            "opening_cost_per_kg": cost,
            "aliases": ["bolt", "screw", "kabza", name.lower()],
        })

    # --- Paint (~4) ---
    for name, code, cost, unit in [
        ("Asian Paints Apcolite Emulsion White", "AP_EMUL_W", 210, "litre"),
        ("Asian Paints Tractor Emulsion", "AP_TRACTOR", 165, "litre"),
        ("Berger Enamel Glossy White", "BRG_ENAMEL_W", 240, "litre"),
        ("Berger Silk Luxury Emulsion", "BRG_SILK", 320, "litre")]:
        cat.append({
            "sku_id": f"PAINT_{code}",
            "canonical": name,
            "family": "paint",
            "attributes": {},
            "default_unit": "litre",
            "units": {"litre": 1, "bucket": 20},
            "gst_rate": 18,
            "opening_cost_per_kg": cost,
            "aliases": ["paint", "rang", name.lower().split()[0].lower()],
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
    all_ids = [s["sku_id"] for s in cat]

    # 8 SKUs left with NO baseline and NO activity -> UNCOUNTED in the demo.
    uncounted = {"TMT_25_FE500D_SRMB", "FAST_ANCHOR_10", "FAST_SCREW_2",
                 "FIT_ELBOW_2", "PAINT_BRG_SILK", "PIPE_20_C5",
                 "CEM_JK_PPC", "FIT_COUP_1"}

    counted = [i for i in all_ids if i not in uncounted]  # ~42 get a baseline

    # 12 counted SKUs with zero movement in 60 days -> frozen capital.
    frozen = {"CEM_AMBUJA_PPC", "CEM_ACC_PPC", "PIPE_15_C5", "PIPE_10_C5",
              "TMT_6_FE500_JINDAL", "TMT_20_FE500D_SRMB", "FIT_TEE_1",
              "PAINT_BRG_ENAMEL_W", "TMT_16_FE500D_JINDAL", "PIPE_075_C5",
              "CEM_JK_OPC43", "TMT_8_FE500_SRMB"}

    # 1) opening_balance for ~42 counted SKUs at period start
    for sid in counted:
        sku = by_id[sid]
        fam = sku["family"]
        if fam == "tmt":
            qty, unit, cp = round(random.uniform(1.5, 6.0), 1), "tonne", "estimated"
        elif fam == "cement":
            qty, unit, cp = random.randint(40, 220), "bori", "exact"
        elif fam == "pipe":
            qty, unit, cp = random.randint(20, 160), "piece", "exact"
        else:
            qty, unit, cp = random.randint(30, 300), sku["default_unit"], "exact"
        add(type="opening_balance", sku_id=sid, qty=qty, unit=unit, rate=None,
            payment=None, occurred_on=d(PERIOD_START), precision="day",
            count_precision=cp, occurred_at=None, recorded_at=rec(PERIOD_START),
            confidence=0.9, source="voice_count",
            evidence={"transcript": f"opening stock {qty} {unit}"})

    # 2) Steel price rise mid-period: early deliveries cheap, later dearer.
    #    Deliveries carry landed cost (rate per base unit).
    steel_targets = ["TMT_12_FE500D_TATA", "TMT_12_FE500_JINDAL",
                     "TMT_10_FE500D_TATA", "TMT_16_FE500D_TATA"]
    for sid in steel_targets:
        sku = by_id[sid]
        base = sku["opening_cost_per_kg"]
        # early delivery ~base, late delivery +8% (the rise)
        early = PERIOD_START + timedelta(days=6)
        late = PERIOD_START + timedelta(days=26)
        add(type="delivery", sku_id=sid, qty=round(random.uniform(2, 5), 1),
            unit="tonne", rate=round(base * 0.99, 1), payment="credit",
            occurred_on=d(early), precision="day", count_precision=None,
            occurred_at=None, recorded_at=rec(early), confidence=0.95,
            source="invoice_photo", evidence={"transcript": "delivery received"})
        add(type="delivery", sku_id=sid, qty=round(random.uniform(2, 5), 1),
            unit="tonne", rate=round(base * 1.08, 1), payment="credit",
            occurred_on=d(late), precision="day", count_precision=None,
            occurred_at=None, recorded_at=rec(late), confidence=0.95,
            source="invoice_photo",
            evidence={"transcript": "delivery received (price up)"})

    # cement + pipe deliveries (no dramatic rise)
    for sid in ["CEM_ULTRATECH_OPC53", "CEM_ACC_OPC43", "PIPE_10_C3"]:
        sku = by_id[sid]
        dd = PERIOD_START + timedelta(days=random.randint(8, 20))
        add(type="delivery", sku_id=sid, qty=random.randint(40, 120),
            unit=sku["default_unit"], rate=sku["opening_cost_per_kg"],
            payment="credit", occurred_on=d(dd), precision="day",
            count_precision=None, occurred_at=None, recorded_at=rec(dd),
            confidence=0.95, source="invoice_photo",
            evidence={"transcript": "delivery"})

    # 3) Sales across the period (skip frozen + uncounted). cash/credit mix.
    sellable = [i for i in counted if i not in frozen]
    for day_off in range(2, 42):
        dt = PERIOD_START + timedelta(days=day_off)
        for _ in range(random.randint(1, 4)):
            sid = random.choice(sellable)
            sku = by_id[sid]
            fam = sku["family"]
            cost = sku["opening_cost_per_kg"]
            # sale rate = cost * healthy margin
            markup = random.uniform(1.05, 1.18)
            if fam == "tmt":
                qty, unit = round(random.uniform(0.2, 1.5), 1), "tonne"
            elif fam == "cement":
                qty, unit = random.randint(5, 40), "bori"
            elif fam == "pipe":
                qty, unit = random.randint(2, 20), "piece"
            else:
                qty, unit = random.randint(2, 15), sku["default_unit"]
            pay = "credit" if random.random() < 0.4 else "cash"
            add(type="sale", sku_id=sid, qty=qty, unit=unit,
                rate=round(cost * markup, 1), payment=pay, occurred_on=d(dt),
                precision="exact" if day_off % 3 else "day", count_precision=None,
                occurred_at=(dt.isoformat() + "T13:20:00") if day_off % 3 else None,
                recorded_at=rec(dt), confidence=0.85, source="voice_live",
                evidence={"transcript": f"{qty} {unit} {sku['canonical']}"})

    # 4) TODAY's sales so the Today tab shows live numbers (cash + credit)
    today_sales = [
        ("TMT_12_FE500D_TATA", 1.5, "tonne", "cash"),
        ("CEM_ULTRATECH_OPC53", 30, "bori", "cash"),
        ("TMT_16_FE500D_TATA", 0.8, "tonne", "credit"),
        ("PIPE_10_C3", 12, "piece", "credit"),
    ]
    for sid, qty, unit, pay in today_sales:
        sku = by_id[sid]
        cost = sku["opening_cost_per_kg"]
        add(type="sale", sku_id=sid, qty=qty, unit=unit,
            rate=round(cost * 1.12, 1), payment=pay, occurred_on=d(TODAY),
            precision="exact", count_precision=None,
            occurred_at=TODAY.isoformat() + "T11:00:00", recorded_at=rec(TODAY),
            confidence=0.9, source="voice_live",
            evidence={"transcript": f"{qty} {unit} {sku['canonical']}"})

    # a week-precision entry -> "plus N purani entries", excluded from today
    add(type="sale", sku_id="CEM_ACC_OPC43", qty=15, unit="bori",
        rate=by_id["CEM_ACC_OPC43"]["opening_cost_per_kg"] * 1.1, payment="cash",
        occurred_on=d(TODAY - timedelta(days=3)), precision="week",
        count_precision=None, occurred_at=None, recorded_at=rec(TODAY),
        confidence=0.6, source="voice_recall",
        evidence={"transcript": "pichle hafte kuch cement gaya tha"})

    # 5) one stock_take that DISAGREES with derived stock (reconciliation delta)
    #    Pick a counted, actively-sold SKU.
    st_sku = "CEM_ULTRATECH_OPC53"
    add(type="stock_take", sku_id=st_sku, qty=38, unit="bori", rate=None,
        payment=None, occurred_on=d(TODAY - timedelta(days=1)), precision="day",
        count_precision="exact", occurred_at=None,
        recorded_at=rec(TODAY - timedelta(days=1)), confidence=0.92,
        source="voice_count",
        evidence={"transcript": "aaj gina to 48 bori nikla"})

    return events


# ---------------------------------------------------------------------------
# Learning states
# ---------------------------------------------------------------------------
def learning_day1() -> dict:
    return {"aliases_learned": [], "attribute_priors": [],
            "unit_priors": [], "corrections": []}


def learning_day60() -> dict:
    now = TODAY.isoformat()
    aliases = [
        ("chhota rod", "TMT_8_FE500_TATA"), ("barah mm", "TMT_12_FE500D_TATA"),
        ("solah mm", "TMT_16_FE500D_TATA"), ("tiscon barah", "TMT_12_FE500D_TATA"),
        ("das mm saria", "TMT_10_FE500D_TATA"), ("jindal barah", "TMT_12_FE500_JINDAL"),
        ("ultratech 53", "CEM_ULTRATECH_OPC53"), ("acc chalis tetalis", "CEM_ACC_OPC43"),
        ("ambuja", "CEM_AMBUJA_OPC53"), ("ppc cement", "CEM_ULTRATECH_PPC"),
        ("ek inch pipe", "PIPE_10_C3"), ("aadha inch", "PIPE_05_C3"),
        ("do inch pipe", "PIPE_2_C5"), ("motta rod", "TMT_20_FE500D_TATA"),
        ("srmb barah", "TMT_12_FE500D_SRMB"), ("panther das", "TMT_10_FE500_JINDAL"),
        ("emulsion white", "PAINT_AP_EMUL_W"), ("enamel", "PAINT_BRG_ENAMEL_W"),
        ("elbow", "FIT_ELBOW_1"), ("coupler ek inch", "FIT_COUP_1"),
        ("tractor paint", "PAINT_AP_TRACTOR"), ("pauna inch pipe", "PIPE_075_C3"),
        ("chhabis mm", "TMT_25_FE500D_TATA"), ("tata das", "TMT_10_FE500D_TATA"),
        ("cement ultratech", "CEM_ULTRATECH_OPC53"),
    ]
    priors = [
        {"family": "tmt", "attribute": "diameter_mm", "value": 12, "count": 19},
        {"family": "tmt", "attribute": "grade", "value": "Fe500D", "count": 31},
        {"family": "tmt", "attribute": "brand", "value": "Tata Tiscon", "count": 22},
        {"family": "cement", "attribute": "brand", "value": "UltraTech", "count": 18},
        {"family": "tmt", "attribute": "grade", "value": "Fe500", "count": 7},
        {"family": "pipe", "attribute": "class", "value": "Class 3", "count": 11},
    ]
    unit_priors = [
        {"sku_id": "TMT_12_FE500D_TATA", "unit": "tonne", "count": 14},
        {"sku_id": "CEM_ULTRATECH_OPC53", "unit": "bori", "count": 26},
        {"sku_id": "PIPE_10_C3", "unit": "piece", "count": 9},
    ]
    corrections = [
        {"spoken": "chhota rod", "chosen_sku": "TMT_8_FE500_TATA",
         "rejected": ["TMT_6_FE500_TATA", "TMT_10_FE500D_TATA"], "ts": now},
        {"spoken": "motta rod", "chosen_sku": "TMT_20_FE500D_TATA",
         "rejected": ["TMT_16_FE500D_TATA", "TMT_25_FE500D_TATA"], "ts": now},
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
    dump("notifications.json", [])
    print(f"catalogue: {len(cat)} SKUs")
    print(f"events:    {len(events)}")
    print("learning:  day1 (empty) + day60 (~25 aliases) written")


if __name__ == "__main__":
    main()
