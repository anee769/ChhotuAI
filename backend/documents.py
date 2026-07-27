"""Short-lived storage for generated PDFs.

Twilio fetches media from a public HTTPS URL — it cannot authenticate — so a
bill has to be reachable without a session. The protection is therefore the
URL itself: 32 random bytes of token, and a row that expires within days.
Once the message is delivered the link goes dead, which limits how long a
customer's bill is retrievable by anyone who happens to have it.
"""
from __future__ import annotations

import os
import secrets
from datetime import timedelta

import db

DEFAULT_TTL_DAYS = 7


def public_base() -> str:
    """Absolute base URL, since Twilio fetches from outside."""
    for var in ("CHHOTU_PUBLIC_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v if v.startswith("http") else f"https://{v}"
    return "http://127.0.0.1:8000"


def store(user_id: str, kind: str, filename: str, content: bytes,
          ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO documents (token, user_id, kind, filename, content,"
            " expires_at) VALUES (%s,%s,%s,%s,%s, now() + %s)",
            (token, user_id, kind, filename, content,
             timedelta(days=ttl_days)))
        # Opportunistic cleanup: no scheduler needed for something this cheap.
        conn.execute("DELETE FROM documents WHERE expires_at < now()")
    return {"token": token, "filename": filename,
            "url": f"{public_base()}/d/{token}/{filename}"}


def fetch(token: str):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT filename, content FROM documents"
            " WHERE token = %s AND expires_at > now()", (token,)).fetchone()
    if not row:
        return None
    return {"filename": row[0], "content": bytes(row[1])}
