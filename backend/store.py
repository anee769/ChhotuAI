"""Where the JSON documents actually live.

The whole app reads and writes exactly two primitives — read(name, default)
and write(name, obj) — over a handful of whole documents (events, customers,
catalogue, learning, config...). Everything above this file works on plain
in-memory Python objects, so swapping the backing store is a local change
here rather than a rewrite of every call site.

Two backends:

  FileStore      the original: one JSON file per document, atomic replace.
                 Used for local runs and the test suite.

  PostgresStore  one JSONB row per document. Serverless hosts (Vercel) give
                 you a read-only filesystem apart from /tmp, and /tmp is
                 per-instance and wiped between invocations — so a file-backed
                 ledger there silently loses every sale. Selected automatically
                 when DATABASE_URL is set.

The document shape is unchanged either way: a row's `data` column holds the
exact JSON that used to sit in the file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

TABLE = "chhotu_documents"


class FileStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def read(self, name: str, default):
        p = self.dir / name
        if not p.exists():
            return default
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def write(self, name: str, obj) -> None:
        # flush on every write: temp file + atomic replace
        p = self.dir / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)


class PostgresStore:
    """One row per document, keyed by the old filename.

    Reads go through a short-lived connection rather than a pooled one: on
    serverless the process may be frozen between requests, and a connection
    held across that boundary comes back dead.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._ensure_table()

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=False)

    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                "  name TEXT PRIMARY KEY,"
                "  data JSONB NOT NULL,"
                "  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
            conn.commit()

    def read(self, name: str, default):
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT data FROM {TABLE} WHERE name = %s", (name,)
            ).fetchone()
        return default if row is None else row[0]

    def write(self, name: str, obj) -> None:
        # A whole document is rewritten on every save, so two concurrent
        # writers would otherwise race and one sale would vanish. The upsert
        # takes a row lock for the duration of the transaction.
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE} (name, data, updated_at)"
                " VALUES (%s, %s::jsonb, now())"
                " ON CONFLICT (name) DO UPDATE"
                "   SET data = EXCLUDED.data, updated_at = now()",
                (name, json.dumps(obj, ensure_ascii=False)),
            )
            conn.commit()

    def is_empty(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()
        return (row[0] if row else 0) == 0


def make_store(data_dir: Path):
    """Postgres when DATABASE_URL is present, files otherwise.

    A first deploy starts with an empty table, which would render as an app
    with no catalogue at all. Seed it once from the JSON files shipped in the
    repo so the shop opens with its products already on file.
    """
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        return FileStore(data_dir)

    store = PostgresStore(dsn)
    if store.is_empty():
        seed_from = FileStore(data_dir)
        for name in ("catalogue.json", "events.json", "customers.json",
                     "receivables.json", "payments.json", "learning.json",
                     "learning_day1.json", "learning_day60.json",
                     "config.json"):
            initial = seed_from.read(name, default=None)
            if initial is not None:
                store.write(name, initial)
    return store
