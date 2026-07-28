import os
import unittest
from unittest.mock import patch

import samvaad_runtime


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class SamvaadRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "SAMVAAD_API_KEY": "server-only-key",
                "SAMVAAD_ORG_ID": "org-1",
                "SAMVAAD_WORKSPACE_ID": "workspace-1",
                "SAMVAAD_APP_ID": "app-1",
                "SAMVAAD_AGENT_VERSION": "7",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_browser_config_never_exposes_api_key(self):
        config = samvaad_runtime.browser_config()
        self.assertTrue(config["enabled"])
        self.assertEqual(config["version"], 7)
        self.assertEqual(config["proxy_base_url"], "/api/voice/samvaad/")
        self.assertNotIn("api_key", config)
        self.assertNotIn("server-only-key", repr(config))

    def test_app_defaults_to_committed_version_five(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(samvaad_runtime.settings().version, 5)

    async def test_signed_url_is_limited_to_configured_agent(self):
        with self.assertRaises(PermissionError):
            await samvaad_runtime.get_signed_url(
                "other-org",
                "workspace-1",
                "app-1",
                interaction_type="call",
                version=7,
            )

    async def test_signed_url_uses_server_key_and_pinned_version(self):
        calls = []
        response = _Response(
            payload={
                "url": "wss://signed.example/session",
                "reference_id": "ref-123",
            }
        )
        factory = lambda **_kwargs: _Client(response, calls)
        with patch.object(samvaad_runtime.httpx, "AsyncClient", factory):
            result = await samvaad_runtime.get_signed_url(
                "org-1",
                "workspace-1",
                "app-1",
                interaction_type="call",
                version=7,
            )

        self.assertEqual(result["reference_id"], "ref-123")
        self.assertEqual(len(calls), 1)
        _url, kwargs = calls[0]
        self.assertEqual(kwargs["headers"]["X-API-Key"], "server-only-key")
        self.assertEqual(kwargs["params"]["interaction_type"], "call")
        self.assertEqual(kwargs["params"]["version"], 7)

    async def test_browser_cannot_select_another_pinned_version(self):
        with self.assertRaises(PermissionError):
            await samvaad_runtime.get_signed_url(
                "org-1",
                "workspace-1",
                "app-1",
                interaction_type="call",
                version=8,
            )


if __name__ == "__main__":
    unittest.main()
