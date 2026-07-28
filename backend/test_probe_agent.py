from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_agent


class ProbeLoggingTests(unittest.TestCase):
    def test_sdk_events_never_log_agent_credentials(self):
        event = {
            "agent_variables": {
                "caller_number": "917006322772",
                "agent_secret": "super-secret",
                "shop_key": "private-shop-key",
            },
            "authorization": "Bearer private",
            "nested": [{"api_key": "private-api-key", "text": "safe"}],
        }
        redacted = probe_agent._redact(event)
        rendered = repr(redacted)
        for secret in ("super-secret", "private-shop-key",
                       "Bearer private", "private-api-key"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(
            redacted["agent_variables"]["caller_number"], "917006322772")
        self.assertEqual(redacted["nested"][0]["text"], "safe")


if __name__ == "__main__":
    unittest.main()
