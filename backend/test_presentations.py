import unittest
from contextlib import nullcontext
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import presentations


class _Cursor:
    description = [
        SimpleNamespace(name="presentation_id"),
        SimpleNamespace(name="kind"),
        SimpleNamespace(name="payload"),
        SimpleNamespace(name="created_at"),
    ]

    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if str(query).lstrip().startswith("SELECT"):
            return _Cursor()
        return _Cursor()


class PresentationLookupTests(unittest.TestCase):
    def _lookup_call(self, kind):
        conn = _Connection()
        with patch.object(presentations.db, "connect",
                          return_value=nullcontext(conn)):
            out = presentations.get("user_1", "vp_1", kind=kind)
        self.assertIsNone(out)
        return next(call for call in conn.calls
                    if str(call[0]).lstrip().startswith("SELECT"))

    def test_kind_filter_uses_a_typed_column_parameter(self):
        query, params = self._lookup_call("bill")
        self.assertIn("kind = %s", query)
        self.assertNotIn("%s IS NULL", query)
        self.assertEqual(params, ("user_1", "vp_1", "bill"))

    def test_lookup_without_kind_omits_the_optional_parameter(self):
        query, params = self._lookup_call(None)
        self.assertNotIn("kind = %s", query)
        self.assertNotIn("%s IS NULL", query)
        self.assertEqual(params, ("user_1", "vp_1"))


if __name__ == "__main__":
    unittest.main()
