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


if __name__ == "__main__":
    unittest.main()
