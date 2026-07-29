"""Postgres connection and schema for the multi-tenant ledger.

Every row belongs to exactly one user. The user_id is part of each primary key
and every index, so a query that forgets to scope itself returns nothing rather
than another shop's books — the failure mode is empty, not a leak.

Connections are short-lived on purpose: a serverless process can be frozen
between requests and a pooled connection comes back dead.

Two columns stay JSONB, deliberately. `learning` is a bag of aliases, priors
and corrections whose shape changes as the matcher learns, and `config` is
free-form settings. Both are read and written whole, never queried by field,
so columns would buy nothing and cost a migration every time the shape moves.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    phone        TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    name         TEXT NOT NULL DEFAULT '',
    shop_name    TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    onboarded_at TIMESTAMPTZ
);

-- Live deployments created before password authentication are upgraded in
-- place. This remains safe to run for every fresh schema as well.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS skus (
    user_id             TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    sku_id              TEXT NOT NULL,
    canonical           TEXT NOT NULL,
    family              TEXT,
    attributes          JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_unit        TEXT,
    units               JSONB NOT NULL DEFAULT '{}'::jsonb,
    gst_rate            NUMERIC,
    opening_cost_per_kg NUMERIC,
    landed_cost_per_kg  NUMERIC,
    aliases             JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (user_id, sku_id)
);

CREATE TABLE IF NOT EXISTS events (
    row_id           BIGSERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    event_id         TEXT NOT NULL,
    type             TEXT NOT NULL,
    sku_id           TEXT,
    qty              NUMERIC,
    unit             TEXT,
    rate             NUMERIC,
    quoted_rate      NUMERIC,
    rate_unit        TEXT,
    payment          TEXT,
    customer_id      TEXT,
    payment_deadline TEXT,
    occurred_on      DATE NOT NULL,
    precision        TEXT,
    count_precision  TEXT,
    occurred_at      TEXT,
    confidence       NUMERIC,
    source           TEXT,
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at      TEXT,
    UNIQUE (user_id, event_id)
);
-- Stock is derived by replaying a SKU's events in order; margin scans a day.
CREATE INDEX IF NOT EXISTS events_user_sku  ON events(user_id, sku_id);
CREATE INDEX IF NOT EXISTS events_user_date ON events(user_id, occurred_on);

CREATE TABLE IF NOT EXISTS customers (
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    customer_id TEXT NOT NULL,
    phone       TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT,
    updated_at  TEXT,
    PRIMARY KEY (user_id, customer_id),
    -- one shop may not hold the same customer twice; two shops may both hold it
    UNIQUE (user_id, phone)
);

CREATE TABLE IF NOT EXISTS receivables (
    user_id        TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    receivable_id  TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    amount         NUMERIC NOT NULL,
    deadline       TEXT NOT NULL,
    sale_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at     TEXT,
    status         TEXT NOT NULL DEFAULT 'open',
    PRIMARY KEY (user_id, receivable_id)
);
CREATE INDEX IF NOT EXISTS receivables_user_deadline
    ON receivables(user_id, deadline);

CREATE TABLE IF NOT EXISTS payments (
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    payment_id  TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    amount      NUMERIC NOT NULL,
    paid_on     TEXT,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT,
    PRIMARY KEY (user_id, payment_id)
);
CREATE INDEX IF NOT EXISTS payments_user_customer
    ON payments(user_id, customer_id);

-- Read and written whole, never queried by field: a JSONB bag is the honest
-- representation, and it avoids a migration every time the matcher learns a
-- new kind of prior.
CREATE TABLE IF NOT EXISTS learning (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    state   TEXT NOT NULL DEFAULT 'continuous',
    data    JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (user_id, state)
);

-- Structured shop vocabulary. These relationships support matching and
-- clarification only; transactional truth remains in events and receivables.
CREATE TABLE IF NOT EXISTS knowledge_entities (
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    entity_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    canonical  TEXT NOT NULL,
    ref_id     TEXT NOT NULL DEFAULT '',
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, entity_id)
);
CREATE INDEX IF NOT EXISTS knowledge_entities_user_kind
    ON knowledge_entities(user_id, kind);
CREATE INDEX IF NOT EXISTS knowledge_entities_user_ref
    ON knowledge_entities(user_id, ref_id);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    user_id           TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    edge_id           TEXT NOT NULL,
    source_entity_id  TEXT NOT NULL,
    relation          TEXT NOT NULL,
    target_entity_id  TEXT NOT NULL,
    confidence        NUMERIC NOT NULL,
    evidence_count    INTEGER NOT NULL DEFAULT 1,
    evidence          JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status             TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (user_id, edge_id),
    FOREIGN KEY (user_id, source_entity_id)
        REFERENCES knowledge_entities(user_id, entity_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, target_entity_id)
        REFERENCES knowledge_entities(user_id, entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS knowledge_edges_user_source
    ON knowledge_edges(user_id, source_entity_id, relation);
CREATE INDEX IF NOT EXISTS knowledge_edges_user_target
    ON knowledge_edges(user_id, target_entity_id, relation);

-- Generated PDFs, held just long enough for Twilio to fetch them. Twilio
-- pulls media from a public URL, so the token is the only thing guarding a
-- customer's bill: 32 random bytes, and the row expires within days.
CREATE TABLE IF NOT EXISTS documents (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    filename   TEXT NOT NULL,
    content    BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_expiry ON documents(expires_at);

CREATE TABLE IF NOT EXISTS user_config (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    data    JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Short-lived cards created by the voice agent for the logged-in browser.
-- Samvaad calls tools from its own servers, so an HTTP response alone cannot
-- update the open Chhotu.ai tab. These rows bridge that process boundary and
-- expire automatically; they are not conversation history.
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


def dsn() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def is_configured() -> bool:
    return bool((os.environ.get("DATABASE_URL") or "").strip())


@contextmanager
def connect():
    import psycopg
    conn = psycopg.connect(dsn(), autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)


def rows_to_dicts(cur) -> list:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
