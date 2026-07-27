"""One-off migration: the existing single-tenant ledger becomes user #1.

Reads through the OLD document store (files locally, or the JSONB documents
already in Postgres) and writes into the new per-user relational tables. Safe
to re-run: every insert is an upsert keyed on (user_id, natural id), so a
half-finished run can simply be repeated.

Nothing is deleted. The old documents stay exactly where they are, so a bad
migration costs a re-run, not the shop's books.

    python backend/migrate.py --phone 9876543210 [--shop "Sharma Building Materials"]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth
import db
import store
import sqlrepo


def _load_documents(data_dir: Path) -> dict:
    src = store.make_store(data_dir)
    names = ("catalogue.json", "events.json", "customers.json",
             "receivables.json", "payments.json", "learning.json",
             "learning_day60.json", "config.json")
    out = {}
    for n in names:
        default = {} if n.startswith(("learning", "config")) else []
        out[n] = src.read(n, default)
    return out


def migrate(phone: str, shop_name: str = "", data_dir: Path = None) -> dict:
    data_dir = data_dir or Path(__file__).resolve().parent.parent / "data"
    db.init_schema()
    docs = _load_documents(data_dir)

    user = auth.get_or_create_user(phone)
    uid = user["user_id"]
    counts = {}

    with db.connect() as conn:
        # --- catalogue ---
        for s in docs["catalogue.json"] or []:
            conn.execute(
                "INSERT INTO skus (user_id, sku_id, canonical, family,"
                " attributes, default_unit, units, gst_rate,"
                " opening_cost_per_kg, landed_cost_per_kg, aliases)"
                " VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s::jsonb)"
                " ON CONFLICT (user_id, sku_id) DO NOTHING",
                (uid, s["sku_id"], s.get("canonical", ""), s.get("family"),
                 json.dumps(s.get("attributes") or {}, ensure_ascii=False),
                 s.get("default_unit"),
                 json.dumps(s.get("units") or {}, ensure_ascii=False),
                 s.get("gst_rate"), s.get("opening_cost_per_kg"),
                 s.get("landed_cost_per_kg"),
                 json.dumps(s.get("aliases") or [], ensure_ascii=False)))
        counts["skus"] = len(docs["catalogue.json"] or [])

        # --- customers ---
        for c in docs["customers.json"] or []:
            conn.execute(
                "INSERT INTO customers (user_id, customer_id, phone, name,"
                " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (user_id, customer_id) DO NOTHING",
                (uid, c["customer_id"], c.get("phone", ""), c.get("name", ""),
                 c.get("created_at"), c.get("updated_at")))
        counts["customers"] = len(docs["customers.json"] or [])

        # --- events ---
        cols = list(sqlrepo._EVENT_COLUMNS)
        for e in docs["events.json"] or []:
            values = []
            for col in cols:
                v = e.get(col)
                if col == "evidence":
                    v = json.dumps(v or {}, ensure_ascii=False)
                values.append(v)
            conn.execute(
                f"INSERT INTO events (user_id, {', '.join(cols)}) VALUES ("
                + ", ".join(["%s"] * (len(cols) + 1)) + ")"
                " ON CONFLICT (user_id, event_id) DO NOTHING",
                [uid] + values)
        counts["events"] = len(docs["events.json"] or [])

        # --- receivables / payments ---
        for r in docs["receivables.json"] or []:
            conn.execute(
                "INSERT INTO receivables (user_id, receivable_id, customer_id,"
                " amount, deadline, sale_event_ids, created_at, status)"
                " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)"
                " ON CONFLICT (user_id, receivable_id) DO NOTHING",
                (uid, r["receivable_id"], r.get("customer_id"),
                 r.get("amount") or 0, r.get("deadline") or "",
                 json.dumps(r.get("sale_event_ids") or []),
                 r.get("created_at"), r.get("status", "open")))
        counts["receivables"] = len(docs["receivables.json"] or [])

        for p in docs["payments.json"] or []:
            conn.execute(
                "INSERT INTO payments (user_id, payment_id, customer_id,"
                " amount, paid_on, note, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (user_id, payment_id) DO NOTHING",
                (uid, p["payment_id"], p.get("customer_id"),
                 p.get("amount") or 0, p.get("paid_on"), p.get("note", ""),
                 p.get("created_at")))
        counts["payments"] = len(docs["payments.json"] or [])

        # --- learning (both toggle positions) + config ---
        for state, doc in (("day1", docs["learning.json"]),
                           ("day60", docs["learning_day60.json"])):
            if doc:
                conn.execute(
                    "INSERT INTO learning (user_id, state, data)"
                    " VALUES (%s,%s,%s::jsonb)"
                    " ON CONFLICT (user_id, state) DO NOTHING",
                    (uid, state, json.dumps(doc, ensure_ascii=False)))

        cfg = dict(docs["config.json"] or {})
        if shop_name:
            cfg["shop_name"] = shop_name
        conn.execute(
            "INSERT INTO user_config (user_id, data) VALUES (%s,%s::jsonb)"
            " ON CONFLICT (user_id) DO NOTHING",
            (uid, json.dumps(cfg, ensure_ascii=False)))

    auth.complete_onboarding(
        uid, name=user.get("name") or "",
        shop_name=shop_name or (docs["config.json"] or {}).get("shop_name", ""))
    return {"user_id": uid, "phone": user["phone"], **counts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True, help="owner's phone number")
    ap.add_argument("--shop", default="", help="shop name")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    out = migrate(args.phone, args.shop,
                  Path(args.data_dir) if args.data_dir else None)
    for k, v in out.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
