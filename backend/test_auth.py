import unittest
from contextlib import contextmanager
from unittest.mock import patch

import auth


class PasswordAuthTests(unittest.TestCase):
    def test_password_hash_round_trip(self):
        encoded = auth.hash_password("strong-pass-123")
        self.assertTrue(auth.verify_password("strong-pass-123", encoded))
        self.assertFalse(auth.verify_password("wrong-pass-123", encoded))
        self.assertNotIn("strong-pass-123", encoded)

    def test_passwords_use_unique_salts(self):
        first = auth.hash_password("strong-pass-123")
        second = auth.hash_password("strong-pass-123")
        self.assertNotEqual(first, second)
        self.assertTrue(auth.verify_password("strong-pass-123", second))

    def test_short_password_is_rejected(self):
        with self.assertRaises(ValueError):
            auth.hash_password("short")

    def test_malformed_hash_fails_closed(self):
        self.assertFalse(auth.verify_password("anything", "not-a-password-hash"))

    def test_phone_normalization(self):
        self.assertEqual(auth.normalize_phone("98765 43210"), "+919876543210")
        self.assertEqual(auth.normalize_phone("+91-98765-43210"), "+919876543210")

    def test_legacy_users_receive_default_password_without_overwrite(self):
        class Result:
            def fetchall(self):
                return [("usr_old_1",), ("usr_old_2",)]

        class FakeConnection:
            def __init__(self):
                self.updates = []

            def execute(self, query, params=None):
                if query.startswith("SELECT user_id"):
                    return Result()
                if query.startswith("UPDATE users"):
                    self.updates.append(params)
                return self

        fake = FakeConnection()

        @contextmanager
        def fake_connect():
            yield fake

        previous = auth._PASSWORD_SCHEMA_READY
        auth._PASSWORD_SCHEMA_READY = False
        try:
            with patch.object(auth.db, "connect", fake_connect):
                auth._ensure_password_schema()
        finally:
            auth._PASSWORD_SCHEMA_READY = previous

        self.assertEqual([row[1] for row in fake.updates],
                         ["usr_old_1", "usr_old_2"])
        for encoded, _ in fake.updates:
            self.assertTrue(auth.verify_password("admin123", encoded))
            self.assertFalse(auth.verify_password("different", encoded))

    def test_password_auth_routes_are_registered(self):
        import main
        paths = {route.path for route in main.app.routes}
        self.assertIn("/api/auth/login", paths)
        self.assertIn("/api/auth/signup", paths)
        self.assertNotIn("/api/auth/otp", paths)
        self.assertNotIn("/api/auth/verify", paths)


if __name__ == "__main__":
    unittest.main()
