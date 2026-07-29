"""Short-lived bill and summary cards for an active browser voice session."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import db

_TABLE = """
CREATE TABLE IF NOT EXISTS voice_presentations (
    presentation_id TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_presentations_user_created
    ON voice_presentations(user_id, created_at);
CREATE INDEX IF NOT EXISTS voice_presentations_expiry
    ON voice_presentations(expires_at);
"""


def _ensure(conn) -> None:
    # Safe on old production databases as well as fresh installs.
    conn.execute(_TABLE)


def store(user_id: str, kind: str, payload: dict, *, minutes: int = 30) -> dict:
    if kind not in ("bill", "summary"):
        raise ValueError("Unsupported presentation kind.")
    presentation_id = "vp_" + secrets.token_hex(12)
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    with db.connect() as conn:
        _ensure(conn)
        conn.execute(
            """INSERT INTO voice_presentations
               (presentation_id, user_id, kind, payload, expires_at)
               VALUES (%s, %s, %s, %s::jsonb, %s)""",
            (presentation_id, user_id, kind,
             json.dumps(payload, ensure_ascii=False), expires),
        )
        conn.execute("DELETE FROM voice_presentations WHERE expires_at <= now()")
    return {"presentation_id": presentation_id, "kind": kind,
            "payload": payload}


def list_since(user_id: str, since: datetime) -> list[dict]:
    with db.connect() as conn:
        _ensure(conn)
        cur = conn.execute(
            """SELECT presentation_id, kind, payload, created_at
               FROM voice_presentations
               WHERE user_id = %s AND created_at >= %s AND expires_at > now()
               ORDER BY created_at ASC LIMIT 20""",
            (user_id, since),
        )
        rows = db.rows_to_dicts(cur)
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return rows


def get(user_id: str, presentation_id: str, *, kind: str = None) -> dict | None:
    """Return one unexpired presentation owned by this shop.

    The ownership predicate is intentional: a presentation id is a convenient
    conversation reference, not an authentication credential.
    """
    if not presentation_id:
        return None
    with db.connect() as conn:
        _ensure(conn)
        if kind is None:
            cur = conn.execute(
                """SELECT presentation_id, kind, payload, created_at
                   FROM voice_presentations
                   WHERE user_id = %s AND presentation_id = %s
                     AND expires_at > now()
                   LIMIT 1""",
                (user_id, presentation_id),
            )
        else:
            # Do not express an optional filter as ``%s IS NULL OR kind = %s``.
            # PostgreSQL cannot infer a datatype for a parameter used only by
            # ``IS NULL`` and raises IndeterminateDatatype before send_bill can
            # load its preview. Separate queries keep every placeholder tied
            # to a typed column.
            cur = conn.execute(
                """SELECT presentation_id, kind, payload, created_at
                   FROM voice_presentations
                   WHERE user_id = %s AND presentation_id = %s
                     AND expires_at > now() AND kind = %s
                   LIMIT 1""",
                (user_id, presentation_id, kind),
            )
        rows = db.rows_to_dicts(cur)
    if not rows:
        return None
    row = rows[0]
    row["created_at"] = row["created_at"].isoformat()
    return row


def latest(user_id: str, kind: str, *, max_age_minutes: int = 10) -> dict | None:
    """Return the most recent short-lived card for a follow-up action.

    This is a compatibility path for a committed agent version that has not
    yet been updated to pass ``presentation_id``. The ten-minute window keeps
    an unrelated old preview from becoming today's WhatsApp bill.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    with db.connect() as conn:
        _ensure(conn)
        cur = conn.execute(
            """SELECT presentation_id, kind, payload, created_at
               FROM voice_presentations
               WHERE user_id = %s AND kind = %s
                 AND created_at >= %s AND expires_at > now()
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, kind, cutoff),
        )
        rows = db.rows_to_dicts(cur)
    if not rows:
        return None
    row = rows[0]
    row["created_at"] = row["created_at"].isoformat()
    return row
