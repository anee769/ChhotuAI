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

DATA_DIR = Path(os.environ.get("CHHOTU_DATA_DIR",
                               Path(__file__).resolve().parent.parent / "data"))


def _parse_date(s: str) -> date:
    return datetime.fromisoformat(s).date() if "T" in s else date.fromisoformat(s)


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
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._catalogue = self._read("catalogue.json", default=[])
        self._by_sku = {s["sku_id"]: s for s in self._catalogue}
        self._events = self._read("events.json", default=[])
        self._learning_state = "day1"  # active toggle position
        self._learning = self._read("learning.json", default=self._empty_learning())
        self._config = self._read("config.json", default={
            "gst_default": 18, "gst_by_family": {}})
        self._counter = self._max_event_num()

    # ---- file helpers ----
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read(self, name: str, default):
        p = self._path(name)
        if not p.exists():
            return default
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def _write(self, name: str, obj) -> None:
        # flush on every write: temp file + atomic replace
        p = self._path(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    @staticmethod
    def _empty_learning() -> dict:
        return {"aliases_learned": [], "attribute_priors": [],
                "unit_priors": [], "corrections": []}

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
        with self._lock:
            self._counter += 1
            event.setdefault("event_id", f"evt_{self._counter:04d}")
            event.setdefault("recorded_at", datetime.now().isoformat(timespec="seconds"))
            self._events.append(event)
            self._write("events.json", self._events)
        return event["event_id"]

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
        with self._lock:
            if sku["sku_id"] in self._by_sku:
                self._by_sku[sku["sku_id"]].update(sku)
            else:
                self._catalogue.append(sku)
                self._by_sku[sku["sku_id"]] = sku
            self._write("catalogue.json", self._catalogue)

    # ---- learning ----
    def load_learning(self) -> dict:
        return self._learning

    def learning_state(self) -> str:
        return self._learning_state

    def upsert_learning(self, patch: dict) -> None:
        with self._lock:
            for key, items in patch.items():
                self._learning.setdefault(key, [])
                if isinstance(items, list):
                    self._learning[key].extend(items)
                else:
                    self._learning[key].append(items)
            if self._learning_state == "day1":
                self._write("learning.json", self._learning)

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

    def save_config(self, patch: dict) -> None:
        with self._lock:
            if "gst_default" in patch:
                self._config["gst_default"] = patch["gst_default"]
            if "gst_by_family" in patch:
                self._config.setdefault("gst_by_family", {}).update(patch["gst_by_family"])
            self._write("config.json", self._config)

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
