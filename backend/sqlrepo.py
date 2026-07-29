"""SqlRepo — the ledger for ONE user, over real Postgres tables.

Implements the same interface as JsonRepo so the 113 call sites across
main.py, conversation.py, crm.py and learning.py keep working unchanged. The
difference is that every statement carries a user_id, so a shop can only ever
see its own books.

Two details that matter and are easy to get wrong:

- NUMERIC comes back from psycopg as Decimal, and Decimal + float raises
  TypeError. ledger.py does float arithmetic throughout, so every numeric is
  converted on the way out.
- DATE comes back as datetime.date, but the whole codebase treats
  occurred_on as an ISO string. Converted on the way out too.

Reads are memoised per instance and dropped by refresh(), which the request
middleware calls. Within a request that keeps repeated all_events() calls to
one query; across requests nothing is ever stale.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import db
import translit
from learning import merge_learning, merge_seed_learning

_EVENT_COLUMNS = (
    "event_id", "type", "sku_id", "qty", "unit", "rate", "quoted_rate",
    "rate_unit", "payment", "customer_id", "payment_deadline", "occurred_on",
    "precision", "count_precision", "occurred_at", "confidence", "source",
    "evidence", "recorded_at",
)

_NUMERIC_EVENT_FIELDS = ("qty", "rate", "quoted_rate", "confidence")


def _plain(value):
    """Decimal -> float, date -> ISO string. Everything downstream assumes
    plain JSON types; a stray Decimal blows up ledger arithmetic."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _clean_row(row: dict) -> dict:
    return {k: _plain(v) for k, v in row.items()}


def _empty_learning() -> dict:
    return {"aliases_learned": [], "attribute_priors": [],
            "unit_priors": [], "corrections": []}


class SqlRepo:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self._lock = threading.Lock()
        self._memo: dict = {}

    # ---- caching -------------------------------------------------------
    def refresh(self) -> None:
        with self._lock:
            self._memo.clear()

    def _cached(self, key, loader):
        if key not in self._memo:
            self._memo[key] = loader()
        return self._memo[key]

    def _invalidate(self, *keys) -> None:
        for k in keys:
            self._memo.pop(k, None)

    # ---- events --------------------------------------------------------
    def append_event(self, event: dict) -> str:
        cols = list(_EVENT_COLUMNS)
        with db.connect() as conn:
            # The id is derived inside the transaction from this user's rows,
            # so two concurrent writers cannot mint the same one.
            if not event.get("event_id"):
                row = conn.execute(
                    "SELECT event_id FROM events WHERE user_id = %s"
                    " ORDER BY row_id DESC LIMIT 1", (self.user_id,)).fetchone()
                last = 0
                if row and str(row[0]).startswith("evt_"):
                    try:
                        last = int(str(row[0]).split("_")[1])
                    except (ValueError, IndexError):
                        last = 0
                # A gap-free counter needs the max, not the newest row.
                row = conn.execute(
                    "SELECT COALESCE(MAX(NULLIF(regexp_replace(event_id,"
                    " '\\D', '', 'g'), '')::bigint), 0) FROM events"
                    " WHERE user_id = %s", (self.user_id,)).fetchone()
                last = max(last, int(row[0] or 0))
                event["event_id"] = f"evt_{last + 1:04d}"
            event.setdefault("recorded_at",
                             datetime.now().isoformat(timespec="seconds"))
            values = []
            for c in cols:
                v = event.get(c)
                if c == "evidence":
                    v = json.dumps(v or {}, ensure_ascii=False)
                values.append(v)
            conn.execute(
                f"INSERT INTO events (user_id, {', '.join(cols)}) VALUES ("
                + ", ".join(["%s"] * (len(cols) + 1)) + ")",
                [self.user_id] + values)
        self._invalidate("events")
        return event["event_id"]

    def _load_events(self) -> list:
        with db.connect() as conn:
            cur = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} FROM events"
                " WHERE user_id = %s ORDER BY row_id", (self.user_id,))
            rows = db.rows_to_dicts(cur)
        return [_clean_row(r) for r in rows]

    def all_events(self) -> list:
        return list(self._cached("events", self._load_events))

    def events_for_sku(self, sku_id: str) -> list:
        return [e for e in self.all_events() if e.get("sku_id") == sku_id]

    def events_in_range(self, start: date, end: date) -> list:
        out = []
        for e in self.all_events():
            d = date.fromisoformat(str(e["occurred_on"])[:10])
            if start <= d <= end:
                out.append(e)
        return out

    # ---- catalogue -----------------------------------------------------
    def _load_catalogue(self) -> list:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT sku_id, canonical, family, attributes, default_unit,"
                " units, gst_rate, opening_cost_per_kg, landed_cost_per_kg,"
                " aliases FROM skus WHERE user_id = %s ORDER BY sku_id",
                (self.user_id,))
            rows = db.rows_to_dicts(cur)
        out = []
        for r in rows:
            r = _clean_row(r)
            # landed_cost is absent for a SKU that has never been invoiced;
            # the ledger distinguishes absent from zero, so don't fill it in.
            if r.get("landed_cost_per_kg") is None:
                r.pop("landed_cost_per_kg", None)
            out.append(r)
        return out

    def load_catalogue(self) -> list:
        return list(self._cached("catalogue", self._load_catalogue))

    def sku(self, sku_id: str) -> Optional[dict]:
        return next((s for s in self.load_catalogue()
                     if s["sku_id"] == sku_id), None)

    def upsert_sku(self, sku: dict) -> None:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO skus (user_id, sku_id, canonical, family,"
                " attributes, default_unit, units, gst_rate,"
                " opening_cost_per_kg, landed_cost_per_kg, aliases)"
                " VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s::jsonb)"
                " ON CONFLICT (user_id, sku_id) DO UPDATE SET"
                "   canonical = EXCLUDED.canonical,"
                "   family = EXCLUDED.family,"
                "   attributes = EXCLUDED.attributes,"
                "   default_unit = EXCLUDED.default_unit,"
                "   units = EXCLUDED.units,"
                "   gst_rate = EXCLUDED.gst_rate,"
                "   opening_cost_per_kg = EXCLUDED.opening_cost_per_kg,"
                "   landed_cost_per_kg = COALESCE(EXCLUDED.landed_cost_per_kg,"
                "                                 skus.landed_cost_per_kg),"
                "   aliases = EXCLUDED.aliases",
                (self.user_id, sku["sku_id"], sku.get("canonical", ""),
                 sku.get("family"),
                 json.dumps(sku.get("attributes") or {}, ensure_ascii=False),
                 sku.get("default_unit"),
                 json.dumps(sku.get("units") or {}, ensure_ascii=False),
                 sku.get("gst_rate"), sku.get("opening_cost_per_kg"),
                 sku.get("landed_cost_per_kg"),
                 json.dumps(sku.get("aliases") or [], ensure_ascii=False)))
        self._invalidate("catalogue")

    def delete_sku(self, sku_id: str) -> bool:
        """Remove a product. Only ever safe when nothing references it.

        Stock is derived by replaying events, so deleting a SKU that has any
        would silently rewrite history. The caller checks that; this just
        refuses to be the place the rule is forgotten.
        """
        with db.connect() as conn:
            used = conn.execute(
                "SELECT 1 FROM events WHERE user_id = %s AND sku_id = %s LIMIT 1",
                (self.user_id, sku_id)).fetchone()
            if used:
                return False
            conn.execute("DELETE FROM skus WHERE user_id = %s AND sku_id = %s",
                         (self.user_id, sku_id))
        self._invalidate("catalogue")
        return True

    # ---- learning ------------------------------------------------------
    def _load_learning_row(self, state: str) -> dict:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM learning WHERE user_id = %s AND state = %s",
                (self.user_id, state)).fetchone()
        return (row[0] if row else None) or _empty_learning()

    def learning_state(self) -> str:
        return self._cached("learning_state", lambda: self._read_state())

    def _read_state(self) -> str:
        cfg = self.load_config()
        return cfg.get("learning_state", "day1")

    def load_learning(self) -> dict:
        return self._cached(
            "learning", lambda: self._load_learning_row(self.learning_state()))

    def upsert_learning(self, patch: dict) -> None:
        state = self.learning_state()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM learning WHERE user_id = %s AND state = %s"
                " FOR UPDATE", (self.user_id, state)).fetchone()
            data = (row[0] if row else None) or _empty_learning()
            merge_learning(data, patch)
            conn.execute(
                "INSERT INTO learning (user_id, state, data)"
                " VALUES (%s,%s,%s::jsonb)"
                " ON CONFLICT (user_id, state) DO UPDATE SET data = EXCLUDED.data",
                (self.user_id, state, json.dumps(data, ensure_ascii=False)))
        self._invalidate("learning")

    def seed_learning(self, seed: dict) -> None:
        """Fill missing baseline memories without overwriting shop learning."""
        state = self.learning_state()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM learning WHERE user_id = %s AND state = %s"
                " FOR UPDATE", (self.user_id, state)).fetchone()
            data = (row[0] if row else None) or _empty_learning()
            merge_seed_learning(data, seed)
            conn.execute(
                "INSERT INTO learning (user_id, state, data)"
                " VALUES (%s,%s,%s::jsonb)"
                " ON CONFLICT (user_id, state) DO UPDATE SET data = EXCLUDED.data",
                (self.user_id, state, json.dumps(data, ensure_ascii=False)))
        self._invalidate("learning")

    def set_learning_state(self, which: str) -> None:
        self.save_config({"learning_state": "day60" if which == "day60" else "day1"})
        self._invalidate("learning", "learning_state")

    def learning_counts(self) -> dict:
        L = self.load_learning()
        return {
            "aliases": len(L.get("aliases_learned", [])),
            "priors": len(L.get("attribute_priors", [])) + len(L.get("unit_priors", [])),
            "corrections": len(L.get("corrections", [])),
        }

    # ---- config --------------------------------------------------------
    def _load_config(self) -> dict:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM user_config WHERE user_id = %s",
                (self.user_id,)).fetchone()
        cfg = (row[0] if row else None) or {}
        cfg.setdefault("gst_default", 18)
        cfg.setdefault("gst_by_family", {})
        return cfg

    def load_config(self) -> dict:
        return self._cached("config", self._load_config)

    _SETTING_KEYS = ("shop_name", "shop_type", "gstin", "address", "phone",
                     "reply_language", "silence_timeout_s",
                     "confirm_new_items", "learning_state")

    def save_config(self, patch: dict) -> None:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM user_config WHERE user_id = %s FOR UPDATE",
                (self.user_id,)).fetchone()
            cfg = (row[0] if row else None) or {"gst_default": 18,
                                                "gst_by_family": {}}
            if "gst_default" in patch:
                cfg["gst_default"] = patch["gst_default"]
            if "gst_by_family" in patch:
                cfg.setdefault("gst_by_family", {}).update(patch["gst_by_family"])
            for key in self._SETTING_KEYS:
                if key in patch:
                    cfg[key] = patch[key]
            conn.execute(
                "INSERT INTO user_config (user_id, data) VALUES (%s,%s::jsonb)"
                " ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data",
                (self.user_id, json.dumps(cfg, ensure_ascii=False)))
        self._invalidate("config", "learning_state", "learning")

    def gst_rate_for(self, sku: dict) -> float:
        cfg = self.load_config()
        fam = (sku or {}).get("family")
        by_fam = cfg.get("gst_by_family", {})
        if fam in by_fam:
            return by_fam[fam]
        if sku and sku.get("gst_rate") is not None:
            return sku["gst_rate"]
        return cfg.get("gst_default", 18)

    # ---- customers / credit -------------------------------------------
    @staticmethod
    def normalize_phone(phone: str) -> str:
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if len(digits) == 10:
            return "+91" + digits
        if len(digits) == 12 and digits.startswith("91"):
            return "+" + digits
        return ("+" + digits) if digits else ""

    def _load_customers(self) -> list:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT customer_id, phone, name, created_at, updated_at"
                " FROM customers WHERE user_id = %s ORDER BY customer_id",
                (self.user_id,))
            return [_clean_row(r) for r in db.rows_to_dicts(cur)]

    def customers(self) -> list:
        return list(self._cached("customers", self._load_customers))

    def customer(self, customer_id: str) -> Optional[dict]:
        return next((c for c in self.customers()
                     if c.get("customer_id") == customer_id), None)

    def customer_by_phone(self, phone: str) -> Optional[dict]:
        norm = self.normalize_phone(phone)
        return next((c for c in self.customers() if c.get("phone") == norm), None)

    def upsert_customer(self, phone: str, name: str = None) -> dict:
        norm = self.normalize_phone(phone)
        if not norm:
            raise ValueError("valid phone number required")
        # Names are always stored in Latin script, enforced here so it holds
        # for every path into the table.
        clean = translit.to_latin_name(name) if name else None
        now = datetime.now().isoformat(timespec="seconds")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT customer_id, name FROM customers"
                " WHERE user_id = %s AND phone = %s FOR UPDATE",
                (self.user_id, norm)).fetchone()
            if row:
                cid = row[0]
                conn.execute(
                    "UPDATE customers SET name = COALESCE(%s, name),"
                    " updated_at = %s WHERE user_id = %s AND customer_id = %s",
                    (clean, now, self.user_id, cid))
            else:
                cid = _next_id(conn, self.user_id, "customers",
                               "customer_id", "cust")
                conn.execute(
                    "INSERT INTO customers (user_id, customer_id, phone, name,"
                    " created_at) VALUES (%s,%s,%s,%s,%s)",
                    (self.user_id, cid, norm, clean or "", now))
        self._invalidate("customers")
        return self.customer(cid)

    def update_customer(self, customer_id: str, phone: str, name: str) -> dict:
        norm = self.normalize_phone(phone)
        clean = translit.to_latin_name(name).strip() if name else ""
        if not norm or len(clean) < 2:
            raise ValueError("valid name and phone number required")
        now = datetime.now().isoformat(timespec="seconds")
        with db.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM customers WHERE user_id = %s"
                " AND customer_id = %s", (self.user_id, customer_id)).fetchone()
            if not existing:
                raise ValueError("customer not found")
            collision = conn.execute(
                "SELECT 1 FROM customers WHERE user_id = %s AND phone = %s"
                " AND customer_id <> %s",
                (self.user_id, norm, customer_id)).fetchone()
            if collision:
                raise ValueError("another customer already uses this phone number")
            conn.execute(
                "UPDATE customers SET phone = %s, name = %s, updated_at = %s"
                " WHERE user_id = %s AND customer_id = %s",
                (norm, clean, now, self.user_id, customer_id))
        self._invalidate("customers")
        return self.customer(customer_id)

    def delete_customer(self, customer_id: str) -> bool:
        """Delete only a contact with no ledger, credit, or payment history."""
        with db.connect() as conn:
            for table in ("events", "receivables", "payments"):
                used = conn.execute(
                    f"SELECT 1 FROM {table}"
                    " WHERE user_id = %s AND customer_id = %s LIMIT 1",
                    (self.user_id, customer_id)).fetchone()
                if used:
                    return False
            cur = conn.execute(
                "DELETE FROM customers"
                " WHERE user_id = %s AND customer_id = %s",
                (self.user_id, customer_id))
            removed = cur.rowcount > 0
        self._invalidate("customers")
        return removed

    def _load_receivables(self) -> list:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT receivable_id, customer_id, amount, deadline,"
                " sale_event_ids, created_at, status FROM receivables"
                " WHERE user_id = %s ORDER BY receivable_id", (self.user_id,))
            return [_clean_row(r) for r in db.rows_to_dicts(cur)]

    def receivables(self) -> list:
        return list(self._cached("receivables", self._load_receivables))

    def add_receivable(self, customer_id: str, amount: float, deadline: str,
                       sale_event_ids: list) -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        with db.connect() as conn:
            rid = _next_id(conn, self.user_id, "receivables",
                           "receivable_id", "recv")
            conn.execute(
                "INSERT INTO receivables (user_id, receivable_id, customer_id,"
                " amount, deadline, sale_event_ids, created_at, status)"
                " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,'open')",
                (self.user_id, rid, customer_id, round(float(amount), 2),
                 deadline, json.dumps(list(sale_event_ids)), now))
        self._invalidate("receivables")
        return next(r for r in self.receivables() if r["receivable_id"] == rid)

    def _load_payments(self) -> list:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT payment_id, customer_id, amount, paid_on, note,"
                " created_at FROM payments WHERE user_id = %s"
                " ORDER BY payment_id", (self.user_id,))
            return [_clean_row(r) for r in db.rows_to_dicts(cur)]

    def payments(self) -> list:
        return list(self._cached("payments", self._load_payments))

    def add_payment(self, customer_id: str, amount: float, paid_on: str,
                    note: str = "") -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        with db.connect() as conn:
            pid = _next_id(conn, self.user_id, "payments", "payment_id", "pay")
            conn.execute(
                "INSERT INTO payments (user_id, payment_id, customer_id,"
                " amount, paid_on, note, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (self.user_id, pid, customer_id, round(float(amount), 2),
                 paid_on, note, now))
        self._invalidate("payments")
        return next(p for p in self.payments() if p["payment_id"] == pid)


def _next_id(conn, user_id: str, table: str, field: str, prefix: str) -> str:
    """Next free id for this user, from the max already present.

    Derived inside the caller's transaction, so two concurrent writers can't
    produce the same one — and taking the max rather than a row count means a
    deleted row's id is never handed out twice.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(NULLIF(regexp_replace({field}, '\\D', '', 'g'),"
        f" '')::bigint), 0) FROM {table} WHERE user_id = %s", (user_id,)
    ).fetchone()
    return f"{prefix}_{int(row[0] or 0) + 1:04d}"
