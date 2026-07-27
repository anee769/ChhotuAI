"""Phone-number OTP login, sessions, and user records.

The delivery channel is deliberately pluggable. WhatsApp authentication
templates are the intended production channel — cheapest per message in India
and, unlike SMS, exempt from TRAI's DLT registration — but templates need Meta
approval and a verified number. Until then send_otp() falls back to a dev
sender, and swapping it out later touches one function.

Codes and session tokens are stored HASHED. A database leak must not hand
anyone a working login, and these rows sit in the same database as the ledger.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import db

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 30
SESSION_DAYS = 30

# Dev mode returns the code in the API response so the whole flow is testable
# with no delivery channel. It is a login bypass, so it must be explicit and
# must never be the default on a deployment.
DEV_OTP_ENV = "CHHOTU_DEV_OTP"


def dev_mode() -> bool:
    return (os.environ.get(DEV_OTP_ENV) or "").strip() == "1"


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
# OTP
# ---------------------------------------------------------------------------
def send_otp(phone: str) -> dict:
    """Issue a code for this number. Returns {sent, dev_code?, retry_after?}."""
    norm = normalize_phone(phone)
    if not norm:
        raise ValueError("A valid 10-digit phone number is required.")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT sent_at FROM otp_codes WHERE phone = %s FOR UPDATE",
            (norm,)).fetchone()
        if row and row[0]:
            since = (_now() - row[0]).total_seconds()
            if since < OTP_RESEND_SECONDS:
                # Rate-limit resends: without this the endpoint is a free
                # SMS/WhatsApp cannon pointed at any number someone types.
                return {"sent": False,
                        "retry_after": int(OTP_RESEND_SECONDS - since)}

        code = f"{secrets.randbelow(10**6):06d}"
        conn.execute(
            "INSERT INTO otp_codes (phone, code_hash, expires_at, attempts,"
            " sent_at) VALUES (%s,%s,%s,0,now())"
            " ON CONFLICT (phone) DO UPDATE SET code_hash = EXCLUDED.code_hash,"
            " expires_at = EXCLUDED.expires_at, attempts = 0, sent_at = now()",
            (norm, _hash(code), _now() + timedelta(minutes=OTP_TTL_MINUTES)))

    delivered = _deliver(norm, code)
    out = {"sent": True, "delivered": delivered}
    if dev_mode():
        out["dev_code"] = code
    return out


def _deliver(phone: str, code: str) -> str:
    """Hand the code to whatever channel is configured.

    Returns the channel name. WhatsApp goes here once an authentication
    template is approved; the signature does not change.
    """
    body = (f"{code} is your Chhotu.ai login code. It expires in "
            f"{OTP_TTL_MINUTES} minutes.\nDo not share this code with anyone.")
    try:
        import whatsapp
        if whatsapp.is_configured():
            # SMS first for login: it reaches any handset, whereas WhatsApp
            # needs the recipient to have opted in to the sandbox — which a
            # brand-new user signing up obviously has not.
            try:
                whatsapp.send_sms(phone, body)
                return "sms"
            except Exception:
                whatsapp.send_whatsapp(phone, body)
                return "whatsapp"
    except Exception:
        # A delivery failure must not lose the code that was just stored —
        # dev mode still returns it, and the user can retry.
        pass
    if dev_mode():
        return "dev"
    return "none"


def verify_otp(phone: str, code: str) -> str:
    """Return the phone number on success. Raises ValueError otherwise."""
    norm = normalize_phone(phone)
    code = re.sub(r"\D", "", code or "")
    if not norm or not code:
        raise ValueError("Enter the 6-digit code.")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT code_hash, expires_at, attempts FROM otp_codes"
            " WHERE phone = %s FOR UPDATE", (norm,)).fetchone()
        if not row:
            raise ValueError("Ask for a code first.")
        code_hash, expires_at, attempts = row
        if _now() > expires_at:
            conn.execute("DELETE FROM otp_codes WHERE phone = %s", (norm,))
            raise ValueError("That code expired. Ask for a new one.")
        if attempts >= OTP_MAX_ATTEMPTS:
            conn.execute("DELETE FROM otp_codes WHERE phone = %s", (norm,))
            raise ValueError("Too many wrong attempts. Ask for a new code.")
        # compare_digest so a wrong code can't be found by timing
        if not hmac.compare_digest(code_hash, _hash(code)):
            conn.execute(
                "UPDATE otp_codes SET attempts = attempts + 1 WHERE phone = %s",
                (norm,))
            raise ValueError("That code isn't right.")
        conn.execute("DELETE FROM otp_codes WHERE phone = %s", (norm,))
    return norm


# ---------------------------------------------------------------------------
# Users and sessions
# ---------------------------------------------------------------------------
def get_or_create_user(phone: str) -> dict:
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
    """Every registered shop — used by the nightly reminder run, which has no
    logged-in user to act as."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT user_id, phone, name, shop_name FROM users"
            " WHERE onboarded_at IS NOT NULL").fetchall()
    return [{"user_id": r[0], "phone": r[1], "name": r[2], "shop_name": r[3],
             "onboarded": True} for r in rows]
