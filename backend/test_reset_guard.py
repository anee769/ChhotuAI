"""Reset destroys the whole ledger. These pin down who is allowed to call it.

/api/reset runs seed.main(), which replaces every sale, customer and udhaar
balance with demo data. It was unauthenticated on a public URL and now targets
the Neon database, where there is no second copy to recover from.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("CHHOTU_TODAY", "2026-07-26")
import main


class ResetGuardTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("CHHOTU_ADMIN_TOKEN", "DATABASE_URL", "VERCEL")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_local_demo_with_no_token_still_works(self):
        allowed, _ = main._reset_allowed("")
        self.assertTrue(allowed)

    def test_a_deployment_refuses_by_default(self):
        # This is the case that mattered: live, real data, no token set.
        os.environ["DATABASE_URL"] = "postgres://x"
        allowed, why = main._reset_allowed("")
        self.assertFalse(allowed)
        self.assertIn("disabled", why)

    def test_vercel_alone_is_enough_to_refuse(self):
        os.environ["VERCEL"] = "1"
        self.assertFalse(main._reset_allowed("")[0])

    def test_a_configured_token_must_match(self):
        os.environ["CHHOTU_ADMIN_TOKEN"] = "s3cret"
        self.assertTrue(main._reset_allowed("s3cret")[0])
        for wrong in ("", "nope", "s3cre", "s3cret "):
            with self.subTest(token=wrong):
                self.assertFalse(main._reset_allowed(wrong)[0])

    def test_the_token_overrides_the_deployment_block(self):
        os.environ["DATABASE_URL"] = "postgres://x"
        os.environ["CHHOTU_ADMIN_TOKEN"] = "s3cret"
        self.assertTrue(main._reset_allowed("s3cret")[0])
        self.assertFalse(main._reset_allowed("")[0])


if __name__ == "__main__":
    unittest.main()
