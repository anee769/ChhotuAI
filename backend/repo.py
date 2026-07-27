"""
repo.py — persistence behind a narrow interface (spec Section 7).

Business logic NEVER touches storage directly. Default JsonRepo writes to
./data/, flushed on every write (the acceptance test kills the server).
Supabase could swap in behind the same interface but is intentionally not
built for the demo.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import store
import translit

DATA_DIR = Path(os.environ.get("CHHOTU_DATA_DIR",
                               Path(__file__).resolve().parent.parent / "data"))


def _parse_date(s: str) -> date:
    return datetime.fromisoformat(s).date() if "T" in s else date.fromisoformat(s)


def _max_event_num(events: list) -> int:
    n = 0
    for e in events:
        eid = e.get("event_id", "")
        if isinstance(eid, str) and eid.startswith("evt_"):
            try:
                n = max(n, int(eid.split("_")[1]))
            except (ValueError, IndexError):
                pass
    return n


def _next_id(rows: list, field: str, prefix: str) -> str:
    """Next free id, derived from the ids actually present.

    len(rows) + 1 was the old rule and it collides: two instances holding the
    same stale list both produce cust_0008, and a deletion makes it reuse a
    retired id. Taking the max of what exists survives both.
    """
    n = 0
    for row in rows:
        value = row.get(field, "")
        if isinstance(value, str) and value.startswith(prefix + "_"):
            try:
                n = max(n, int(value.split("_")[1]))
            except (ValueError, IndexError):
                pass
    return f"{prefix}_{n + 1:04d}"


class Repo:
    """Interface — see JsonRepo for the concrete implementation."""
    def append_event(self, event: dict) -> str: ...
    def all_events(self) -> list: ...
    def events_for_sku(self, sku_id: str) -> list: ...
    def events_in_range(self, start: date, end: date) -> list: ...
    def load_catalogue(self) -> list: ...
    def sku(self, sku_id: str) -> Optional[dict]: ...
    def load_learning(self) -> dict: ...
    def upsert_learning(self, patch: dict) -> None: ...
    def set_learning_state(self, which: str) -> None: ...


class JsonRepo(Repo):
    def __init__(self, data_dir: Path = DATA_DIR):
        self.dir = Path(data_dir)
        # Files locally, Postgres when DATABASE_URL is set. Both speak the same
        # two primitives, so nothing below this line changes between them.
        self._store = store.make_store(self.dir)
        self._lock = threading.Lock()
        self._catalogue = self._read("catalogue.json", default=[])
        self._by_sku = {s["sku_id"]: s for s in self._catalogue}
        self._events = self._read("events.json", default=[])
        self._customers = self._read("customers.json", default=[])
        self._receivables = self._read("receivables.json", default=[])
        self._payments = self._read("payments.json", default=[])
        self._learning_state = "day1"  # active toggle position
        self._learning = self._read("learning.json", default=self._empty_learning())
        self._config = self._read("config.json", default={
            "gst_default": 18, "gst_by_family": {}})
        self._counter = self._max_event_num()

    # ---- document helpers (see store.py for where they actually land) ----
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read(self, name: str, default):
        return self._store.read(name, default)

    def _write(self, name: str, obj) -> None:
        self._store.write(name, obj)

    @staticmethod
    def _empty_learning() -> dict:
        return {"aliases_learned": [], "attribute_priors": [],
                "unit_priors": [], "corrections": []}

    def refresh(self) -> None:
        """Reload every cached document from the store.

        Writes are atomic now, but reads were still served from whatever this
        instance loaded at cold start — so a second instance's sale stayed
        invisible here until the process restarted. Called once per request,
        which keeps repeated reads inside a request cheap while never showing
        another instance's work as missing.
        """
        with self._lock:
            self._catalogue = self._store.read("catalogue.json", [])
            self._by_sku = {s["sku_id"]: s for s in self._catalogue}
            self._events = self._store.read("events.json", [])
            self._customers = self._store.read("customers.json", [])
            self._receivables = self._store.read("receivables.json", [])
            self._payments = self._store.read("payments.json", [])
            self._config = self._store.read(
                "config.json", {"gst_default": 18, "gst_by_family": {}})
            if self._learning_state == "day1":
                self._learning = self._store.read(
                    "learning.json", self._empty_learning())
            self._counter = _max_event_num(self._events)

    def _max_event_num(self) -> int:
        n = 0
        for e in self._events:
            eid = e.get("event_id", "")
            if eid.startswith("evt_"):
                try:
                    n = max(n, int(eid.split("_")[1]))
                except (ValueError, IndexError):
                    pass
        return n

    # ---- events ----
    def append_event(self, event: dict) -> str:
        # The id is derived INSIDE the mutation, from the live list. Deriving
        # it from the cached copy is how two instances both minted evt_0081
        # and one order disappeared.
        def _append(events):
            event.setdefault("event_id", f"evt_{_max_event_num(events) + 1:04d}")
            event.setdefault("recorded_at",
                             datetime.now().isoformat(timespec="seconds"))
            events.append(event)
            return event["event_id"]

        with self._lock:
            self._events, event_id = self._store.mutate("events.json", [], _append)
            self._counter = _max_event_num(self._events)
        return event_id

    def all_events(self) -> list:
        return list(self._events)

    def events_for_sku(self, sku_id: str) -> list:
        return [e for e in self._events if e.get("sku_id") == sku_id]

    def events_in_range(self, start: date, end: date) -> list:
        out = []
        for e in self._events:
            d = _parse_date(e["occurred_on"])
            if start <= d <= end:
                out.append(e)
        return out

    # ---- catalogue ----
    def load_catalogue(self) -> list:
        return list(self._catalogue)

    def sku(self, sku_id: str) -> Optional[dict]:
        return self._by_sku.get(sku_id)

    def upsert_sku(self, sku: dict) -> None:
        """Invoice ingestion adds/updates catalogue rows (cost basis only)."""
        def _upsert(catalogue):
            existing = next((s for s in catalogue
                             if s.get("sku_id") == sku["sku_id"]), None)
            if existing:
                existing.update(sku)
            else:
                catalogue.append(sku)

        with self._lock:
            self._catalogue, _ = self._store.mutate("catalogue.json", [], _upsert)
            self._by_sku = {s["sku_id"]: s for s in self._catalogue}

    # ---- learning ----
    def load_learning(self) -> dict:
        return self._learning

    def learning_state(self) -> str:
        return self._learning_state

    def upsert_learning(self, patch: dict) -> None:
        def _merge(learning):
            for key, items in patch.items():
                learning.setdefault(key, [])
                if isinstance(items, list):
                    learning[key].extend(items)
                else:
                    learning[key].append(items)

        with self._lock:
            if self._learning_state == "day1":
                self._learning, _ = self._store.mutate(
                    "learning.json", self._empty_learning(), _merge)
            else:
                # Day60 is a read-only demo overlay; keep it in memory only.
                _merge(self._learning)

    def set_learning_state(self, which: str) -> None:
        """Toggle Day1/Day60 — swaps the active learning file at runtime."""
        with self._lock:
            fname = "learning_day60.json" if which == "day60" else "learning_day1.json"
            loaded = self._read(fname, default=self._empty_learning())
            self._learning = loaded
            self._learning_state = which

    # ---- config (GST rates) ----
    def load_config(self) -> dict:
        return self._config

    # Settings the Settings page may write. Anything not listed is ignored, so
    # a stray key in a request body can't quietly become persisted config.
    _SETTING_KEYS = ("shop_name", "reply_language", "silence_timeout_s",
                     "confirm_new_items")

    def save_config(self, patch: dict) -> None:
        def _apply(config):
            if "gst_default" in patch:
                config["gst_default"] = patch["gst_default"]
            if "gst_by_family" in patch:
                config.setdefault("gst_by_family", {}).update(patch["gst_by_family"])
            for key in self._SETTING_KEYS:
                if key in patch:
                    config[key] = patch[key]

        with self._lock:
            self._config, _ = self._store.mutate(
                "config.json", {"gst_default": 18, "gst_by_family": {}}, _apply)

    def gst_rate_for(self, sku: dict) -> float:
        fam = (sku or {}).get("family")
        by_fam = self._config.get("gst_by_family", {})
        if fam in by_fam:
            return by_fam[fam]
        if sku and sku.get("gst_rate") is not None:
            return sku["gst_rate"]
        return self._config.get("gst_default", 18)

    def learning_counts(self) -> dict:
        L = self._learning
        return {
            "aliases": len(L.get("aliases_learned", [])),
            "priors": len(L.get("attribute_priors", [])) + len(L.get("unit_priors", [])),
            "corrections": len(L.get("corrections", [])),
        }

    # ---- customers / credit ledger ----
    @staticmethod
    def normalize_phone(phone: str) -> str:
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if len(digits) == 10:
            return "+91" + digits
        if len(digits) == 12 and digits.startswith("91"):
            return "+" + digits
        return ("+" + digits) if digits else ""

    def customers(self) -> list:
        return list(self._customers)

    def customer(self, customer_id: str) -> Optional[dict]:
        return next((c for c in self._customers if c.get("customer_id") == customer_id), None)

    def customer_by_phone(self, phone: str) -> Optional[dict]:
        norm = self.normalize_phone(phone)
        return next((c for c in self._customers if c.get("phone") == norm), None)

    def upsert_customer(self, phone: str, name: str = None) -> dict:
        norm = self.normalize_phone(phone)
        if not norm:
            raise ValueError("valid phone number required")
        # Enforced HERE rather than at the call site so it holds for every
        # path into the customer table. conversation.py transliterates via the
        # LLM for a nicer spelling, but that can fail silently and return the
        # input unchanged, and the CRM's own POST /api/customers never
        # transliterated at all — which is how a Devanagari name got stored.
        if name:
            name = translit.to_latin_name(name)
        def _upsert(customers):
            row = next((c for c in customers if c.get("phone") == norm), None)
            if row:
                if name:
                    row["name"] = name.strip()
                row["updated_at"] = datetime.now().isoformat(timespec="seconds")
            else:
                row = {
                    "customer_id": _next_id(customers, "customer_id", "cust"),
                    "phone": norm, "name": (name or "").strip(),
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                customers.append(row)
            return dict(row)

        with self._lock:
            self._customers, row = self._store.mutate("customers.json", [], _upsert)
            return row

    def add_receivable(self, customer_id: str, amount: float, deadline: str,
                       sale_event_ids: list) -> dict:
        def _add(receivables):
            row = {
                "receivable_id": _next_id(receivables, "receivable_id", "recv"),
                "customer_id": customer_id,
                "amount": round(float(amount), 2),
                "deadline": deadline,
                "sale_event_ids": list(sale_event_ids),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "status": "open",
            }
            receivables.append(row)
            return dict(row)

        with self._lock:
            self._receivables, row = self._store.mutate("receivables.json", [], _add)
            return row

    def receivables(self) -> list:
        return list(self._receivables)

    def payments(self) -> list:
        return list(self._payments)

    def add_payment(self, customer_id: str, amount: float, paid_on: str,
                    note: str = "") -> dict:
        def _add(payments):
            row = {
                "payment_id": _next_id(payments, "payment_id", "pay"),
                "customer_id": customer_id, "amount": round(float(amount), 2),
                "paid_on": paid_on, "note": note,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            payments.append(row)
            return dict(row)

        with self._lock:
            self._payments, row = self._store.mutate("payments.json", [], _add)
            return row
