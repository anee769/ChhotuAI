"""Regression checks for the native Vercel FastAPI entry point."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent.parent


class VercelEntrypointTests(unittest.TestCase):
    def test_root_entrypoint_exports_the_fastapi_app(self):
        spec = importlib.util.spec_from_file_location(
            "chhotu_vercel_entrypoint", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.app.title, "Chhotu.ai — Awaaz se hisaab")

        client = TestClient(module.app)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/api/health").status_code, 200)
        from backend import main as backend_main
        with patch.object(
                backend_main.auth, "authenticate",
                side_effect=backend_main.auth.UserNotFound("Not found")):
            self.assertEqual(
                client.post("/api/auth/login", json={
                    "phone": "0000000000", "password": "invalid",
                }).status_code,
                404,
            )
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_vercel_config_does_not_rewrite_original_request_paths(self):
        config = json.loads((ROOT / "vercel.json").read_text())

        self.assertNotIn("rewrites", config)
        self.assertIn("app.py", config["functions"])

    def test_public_routes_remain_explicitly_open(self):
        from backend import main

        self.assertIn("/", main._OPEN_EXACT)
        self.assertIn("/api/health", main._OPEN_EXACT)
        self.assertIn("/api/auth/", main._OPEN_PREFIXES)


if __name__ == "__main__":
    unittest.main()
