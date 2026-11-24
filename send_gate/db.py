"""SQLite schema and connection helper."""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent         TEXT NOT NULL,
    channel       TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    body          TEXT NOT NULL,
    note          TEXT,
    status        TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL REFERENCES drafts(id),
    phase         TEXT NOT NULL,
    ran_at        TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    pack_version  TEXT NOT NULL,
    pack_sha256   TEXT NOT NULL,
    results       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id              INTEGER NOT NULL REFERENCES drafts(id),
    approver              TEXT NOT NULL,
    content_hash          TEXT NOT NULL,
    acknowledged_cautions INTEGER NOT NULL DEFAULT 0,
    approved_at           TEXT NOT NULL,
    voided_at             TEXT,
    void_reason           TEXT
);

CREATE TABLE IF NOT EXISTS sends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id            INTEGER NOT NULL REFERENCES drafts(id),
    approval_id         INTEGER NOT NULL REFERENCES approvals(id),
    content_hash        TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    adapter             TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    provider_message_id TEXT,
    sent_at             TEXT,
    readback_at         TEXT,
    target_timestamp    TEXT,
    readback_hash       TEXT,
    readback_ok         INTEGER,
    error               TEXT
);

CREATE TABLE IF NOT EXISTS opt_outs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (channel, recipient)
);

-- Small facts the gate keeps about itself, such as the day a sample rule
-- pack was first used with this database.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_hash ON drafts(content_hash);
CREATE INDEX IF NOT EXISTS idx_sends_hash ON sends(content_hash);
CREATE INDEX IF NOT EXISTS idx_audit_draft ON audit_events(draft_id);

-- The audit trail is append only. These triggers stop casual edits; the
-- hash chain in entry_hash is what lets a reviewer detect determined ones.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append only'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    """Open the database and make sure the schema exists.

    One connection is shared across FastAPI worker threads; the gate
    serialises every operation with its own lock.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
