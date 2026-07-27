"""Tests for the document-storage swap.

The Postgres SQL itself needs a live server and is exercised on first deploy;
what is covered here is everything around it — backend selection, the
first-deploy seeding, and that the file backend still round-trips exactly as
before, since that is what every local run and the rest of the suite uses.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store


class FakePostgresStore:
    """Stands in for the real one: same three methods, a dict for a table."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.rows = {}

    def read(self, name, default):
        return self.rows.get(name, default)

    def write(self, name, obj):
        self.rows[name] = obj

    def is_empty(self):
        return not self.rows


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store.FileStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_document_returns_the_default(self):
        self.assertEqual(self.store.read("nope.json", default=[]), [])
        self.assertIsNone(self.store.read("nope.json", default=None))

    def test_round_trips_unicode_without_mangling_it(self):
        rows = [{"canonical": "लकड़ी", "qty": 5}]
        self.store.write("x.json", rows)
        self.assertEqual(self.store.read("x.json", default=None), rows)
        # written as real UTF-8, not \u escapes — these files get read by hand
        self.assertIn("लकड़ी", (Path(self.tmp.name) / "x.json").read_text("utf-8"))

    def test_write_leaves_no_temp_file_behind(self):
        self.store.write("x.json", {"a": 1})
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["x.json"])


class StoreSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "catalogue.json").write_text(
            json.dumps([{"sku_id": "TMT_12", "canonical": "Bar"}]), encoding="utf-8")
        (self.dir / "config.json").write_text('{"gst_default": 18}', encoding="utf-8")
        self._env = os.environ.get("DATABASE_URL")
        os.environ.pop("DATABASE_URL", None)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._env
        self.tmp.cleanup()

    def test_files_are_used_when_no_database_is_configured(self):
        self.assertIsInstance(store.make_store(self.dir), store.FileStore)

    def test_blank_database_url_does_not_count_as_configured(self):
        os.environ["DATABASE_URL"] = "   "
        self.assertIsInstance(store.make_store(self.dir), store.FileStore)

    def test_first_deploy_seeds_the_empty_database_from_the_repo_files(self):
        os.environ["DATABASE_URL"] = "postgres://x"
        with patch.object(store, "PostgresStore", FakePostgresStore):
            s = store.make_store(self.dir)
        # a fresh deploy must not come up with an empty catalogue
        self.assertEqual(s.read("catalogue.json", None),
                         [{"sku_id": "TMT_12", "canonical": "Bar"}])
        self.assertEqual(s.read("config.json", None), {"gst_default": 18})
        # documents absent from the repo aren't invented
        self.assertIsNone(s.read("events.json", None))

    def test_an_existing_database_is_never_overwritten_by_the_repo_files(self):
        os.environ["DATABASE_URL"] = "postgres://x"
        live = FakePostgresStore("postgres://x")
        live.write("catalogue.json", [{"sku_id": "REAL", "canonical": "Live data"}])
        with patch.object(store, "PostgresStore", lambda dsn: live):
            s = store.make_store(self.dir)
        # the shop's real catalogue must survive every redeploy
        self.assertEqual(s.read("catalogue.json", None),
                         [{"sku_id": "REAL", "canonical": "Live data"}])


class ConcurrentInstanceTests(unittest.TestCase):
    """Two JsonRepo objects over one store = two serverless instances, each
    with its own snapshot loaded at cold start. Every write below used to be
    applied to a stale cached copy, so the second writer erased the first."""

    def setUp(self):
        import repo as R
        self.R = R
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name in ("events.json", "customers.json", "receivables.json",
                     "payments.json", "catalogue.json"):
            (self.dir / name).write_text("[]", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _two(self):
        return self.R.JsonRepo(self.dir), self.R.JsonRepo(self.dir)

    @staticmethod
    def _sale(tag):
        return {"type": "sale", "sku_id": "X", "qty": 1, "unit": "bori",
                "occurred_on": "2026-07-28", "precision": "exact",
                "evidence": {"transcript": tag}}

    def test_two_instances_both_keep_their_order(self):
        a, b = self._two()
        id_a = a.append_event(self._sale("A"))
        id_b = b.append_event(self._sale("B"))
        self.assertNotEqual(id_a, id_b, "duplicate event_id")
        kept = {e["evidence"]["transcript"]
                for e in self.R.JsonRepo(self.dir).all_events()}
        self.assertEqual(kept, {"A", "B"}, "an order was lost")

    def test_a_stale_instance_cannot_erase_newer_orders(self):
        a, b = self._two()
        b.append_event(self._sale("B1"))
        b.append_event(self._sale("B2"))
        # `a` has been warm since before either — its next write must not
        # roll the ledger back to what it remembers.
        a.append_event(self._sale("A1"))
        kept = {e["evidence"]["transcript"]
                for e in self.R.JsonRepo(self.dir).all_events()}
        self.assertEqual(kept, {"B1", "B2", "A1"})

    def test_customer_ids_do_not_collide_across_instances(self):
        a, b = self._two()
        ca = a.upsert_customer("9000000001", "A")
        cb = b.upsert_customer("9000000002", "B")
        self.assertNotEqual(ca["customer_id"], cb["customer_id"])
        self.assertEqual(len(self.R.JsonRepo(self.dir).customers()), 2)

    def test_receivables_and_payments_survive_both_instances(self):
        a, b = self._two()
        ra = a.add_receivable("c1", 100, "2026-08-01", [])
        rb = b.add_receivable("c2", 200, "2026-08-02", [])
        self.assertNotEqual(ra["receivable_id"], rb["receivable_id"])
        pa = a.add_payment("c1", 50, "2026-07-28")
        pb = b.add_payment("c2", 60, "2026-07-28")
        self.assertNotEqual(pa["payment_id"], pb["payment_id"])
        fresh = self.R.JsonRepo(self.dir)
        self.assertEqual(len(fresh.receivables()), 2)
        self.assertEqual(len(fresh.payments()), 2)

    def test_refresh_shows_another_instances_work(self):
        a, b = self._two()
        b.append_event(self._sale("B"))
        self.assertEqual(len(a.all_events()), 0, "precondition: a is stale")
        a.refresh()
        self.assertEqual(len(a.all_events()), 1)

    def test_ids_are_not_reused_after_a_deletion(self):
        a, _ = self._two()
        a.upsert_customer("9000000001", "One")
        a.upsert_customer("9000000002", "Two")
        rows = [c for c in a.customers() if c["customer_id"] != "cust_0001"]
        a._store.write("customers.json", rows)
        a.refresh()
        # len()+1 would hand out cust_0002 again and merge two real customers
        self.assertEqual(a.upsert_customer("9000000003", "Three")["customer_id"],
                         "cust_0003")


if __name__ == "__main__":
    unittest.main()
