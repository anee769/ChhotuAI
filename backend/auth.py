"""Phone/password authentication, sessions, and multi-tenant user records.

Passwords and session tokens are stored only as salted hashes. Password hashes
do not use CHHOTU_SECRET: rotating the session secret should sign everyone out,
not invalidate every user's password.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

import db

SESSION_DAYS = 30
PASSWORD_ITERATIONS = 600_000
LEGACY_DEFAULT_PASSWORD = "admin123"
_PASSWORD_SCHEMA_READY = False
_PASSWORD_SCHEMA_LOCK = threading.Lock()


class UserNotFound(ValueError):
    pass


class PasswordSetupRequired(ValueError):
    pass


class InvalidCredentials(ValueError):
    pass


class AccountExists(ValueError):
    pass


def _pepper() -> str:
    # Falls back to a per-process value so a misconfigured deploy fails closed
    # (existing sessions stop validating) rather than open.
    return os.environ.get("CHHOTU_SECRET") or _FALLBACK_SECRET


_FALLBACK_SECRET = secrets.token_hex(32)


def _hash(value: str) -> str:
    return hmac.new(_pepper().encode(), (value or "").encode(),
                    hashlib.sha256).hexdigest()


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return ("+" + digits) if len(digits) >= 10 else ""


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def _ensure_password_schema() -> None:
    """Upgrade existing databases lazily, without deleting user data."""
    global _PASSWORD_SCHEMA_READY
    if _PASSWORD_SCHEMA_READY:
        return
    with _PASSWORD_SCHEMA_LOCK:
        if _PASSWORD_SCHEMA_READY:
            return
        with db.connect() as conn:
            conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
            # Accounts created by the removed OTP flow have no password. Give
            # only those legacy rows the requested demo password; never
            # overwrite a password created through signup.
            rows = conn.execute(
                "SELECT user_id FROM users WHERE password_hash IS NULL"
                " FOR UPDATE").fetchall()
            for (user_id,) in rows:
                conn.execute(
                    "UPDATE users SET password_hash = %s WHERE user_id = %s"
                    " AND password_hash IS NULL",
                    (hash_password(LEGACY_DEFAULT_PASSWORD), user_id))
        _PASSWORD_SCHEMA_READY = True


def validate_password(password: str) -> str:
    password = str(password or "")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if len(password) > 128:
        raise ValueError("Password is too long.")
    return password


def hash_password(password: str) -> str:
    password = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = (encoded or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", str(password or "").encode("utf-8"),
            bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def authenticate(phone: str, password: str) -> dict:
    norm = normalize_phone(phone)
    if not norm:
        raise InvalidCredentials("Enter a valid 10-digit mobile number.")
    _ensure_password_schema()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_id, phone, name, shop_name, onboarded_at, password_hash"
            " FROM users WHERE phone = %s", (norm,)).fetchone()
    if not row:
        raise UserNotFound("No account exists for this number.")
    if not row[5]:
        raise PasswordSetupRequired("This existing account needs a password.")
    if not verify_password(password, row[5]):
        raise InvalidCredentials("Mobile number or password is incorrect.")
    return {"user_id": row[0], "phone": row[1], "name": row[2],
            "shop_name": row[3], "onboarded": row[4] is not None}


def signup(phone: str, name: str, password: str) -> dict:
    norm = normalize_phone(phone)
    name = (name or "").strip()
    if not norm:
        raise ValueError("Enter a valid 10-digit mobile number.")
    if len(name) < 2:
        raise ValueError("Enter the owner's name.")
    encoded = hash_password(password)
    _ensure_password_schema()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_id, phone, name, shop_name, onboarded_at, password_hash"
            " FROM users WHERE phone = %s FOR UPDATE", (norm,)).fetchone()
        if row and row[5]:
            raise AccountExists("An account already exists for this number.")
        if row:
            # Legacy OTP accounts have no password. Requiring the exact stored
            # owner name prevents a bare phone number from claiming an account.
            if row[2] and row[2].strip().casefold() != name.casefold():
                raise InvalidCredentials(
                    "Owner name does not match the existing account.")
            conn.execute(
                "UPDATE users SET name = %s, password_hash = %s WHERE user_id = %s",
                (name, encoded, row[0]))
            user_id, shop_name, onboarded = row[0], row[3], row[4]
        else:
            user_id = "usr_" + secrets.token_hex(8)
            conn.execute(
                "INSERT INTO users (user_id, phone, password_hash, name)"
                " VALUES (%s,%s,%s,%s)",
                (user_id, norm, encoded, name))
            shop_name, onboarded = "", None
    return {"user_id": user_id, "phone": norm, "name": name,
            "shop_name": shop_name, "onboarded": onboarded is not None}


# ---------------------------------------------------------------------------
# Users and sessions
# ---------------------------------------------------------------------------
def get_or_create_user(phone: str) -> dict:
    """Compatibility helper for the one-time data migration script."""
    norm = normalize_phone(phone)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_id, phone, name, shop_name, onboarded_at FROM users"
            " WHERE phone = %s", (norm,)).fetchone()
        if row:
            return {"user_id": row[0], "phone": row[1], "name": row[2],
                    "shop_name": row[3], "onboarded": row[4] is not None}
        user_id = "usr_" + secrets.token_hex(8)
        conn.execute(
            "INSERT INTO users (user_id, phone) VALUES (%s,%s)"
            " ON CONFLICT (phone) DO NOTHING", (user_id, norm))
        row = conn.execute(
            "SELECT user_id, phone, name, shop_name, onboarded_at FROM users"
            " WHERE phone = %s", (norm,)).fetchone()
    return {"user_id": row[0], "phone": row[1], "name": row[2],
            "shop_name": row[3], "onboarded": row[4] is not None}


def complete_onboarding(user_id: str, name: str = "", shop_name: str = "") -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET name = COALESCE(NULLIF(%s,''), name),"
            " shop_name = COALESCE(NULLIF(%s,''), shop_name),"
            " onboarded_at = now() WHERE user_id = %s",
            (name or "", shop_name or "", user_id))


def issue_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at)"
            " VALUES (%s,%s,%s)",
            (_hash(token), user_id, _now() + timedelta(days=SESSION_DAYS)))
    return token


def user_for_token(token: str):
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.user_id, u.phone, u.name, u.shop_name, u.onboarded_at"
            " FROM sessions s JOIN users u ON u.user_id = s.user_id"
            " WHERE s.token_hash = %s AND s.expires_at > now()",
            (_hash(token),)).fetchone()
    if not row:
        return None
    return {"user_id": row[0], "phone": row[1], "name": row[2],
            "shop_name": row[3], "onboarded": row[4] is not None}


def revoke_session(token: str) -> None:
    if not token:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))


def all_users() -> list:
    """Every registered shop — used by jobs that have no logged-in user."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT user_id, phone, name, shop_name FROM users"
            " WHERE onboarded_at IS NOT NULL").fetchall()
    return [{"user_id": r[0], "phone": r[1], "name": r[2], "shop_name": r[3],
             "onboarded": True} for r in rows]
