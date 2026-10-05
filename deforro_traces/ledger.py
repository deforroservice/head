"""SQLite ledger: one row per internal reference, so bulk runs are resumable and idempotent."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Local pipeline states (TRACES' own status is kept separately in traces_status).
INVALID = "INVALID"            # failed pre-flight validation
BLOCKED_RISK = "BLOCKED_RISK"  # risk gate refused it
SUBMITTING = "SUBMITTING"      # request in flight; recover via internal reference on resume
SUBMITTED = "SUBMITTED"        # TRACES returned a UUID
REJECTED = "REJECTED"          # TRACES refused it (SOAP fault or REJECTED status)
FAILED = "FAILED"              # transport error after retries; retried on next run
DONE = "DONE"                  # reference + verification number obtained
WITHDRAWN = "WITHDRAWN"

FINAL_TRACES_STATUSES = {"AVAILABLE", "REJECTED", "WITHDRAWN", "CANCELLED", "ARCHIVED", "GROUPED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dds (
    internal_reference  TEXT PRIMARY KEY,
    batch_id            TEXT,
    state               TEXT NOT NULL,
    payload_hash        TEXT,
    uuid                TEXT,
    traces_status       TEXT,
    reference_number    TEXT,
    verification_number TEXT,
    version             TEXT,
    risk_level          TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    issues              TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dds_state ON dds(state);
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_reference  TEXT NOT NULL,
    at                  TEXT NOT NULL,
    event               TEXT NOT NULL,
    detail              TEXT
);
"""


class LedgerError(Exception):
    pass


@dataclass
class Row:
    internal_reference: str
    batch_id: str | None
    state: str
    payload_hash: str | None
    uuid: str | None
    traces_status: str | None
    reference_number: str | None
    verification_number: str | None
    version: str | None
    risk_level: str | None
    attempts: int
    issues: str | None
    last_error: str | None
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | Path, environment: str):
        self.path = str(path)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        bound = self.db.execute("SELECT value FROM meta WHERE key='environment'").fetchone()
        if bound is None:
            self.db.execute("INSERT INTO meta VALUES ('environment', ?)", (environment,))
        elif bound[0] != environment:
            # UUIDs from acceptance mean nothing in production and vice versa.
            raise LedgerError(
                f"ledger {self.path} belongs to environment {bound[0]!r}, not {environment!r}; "
                "use a separate ledger per environment"
            )
        self.environment = environment

    def close(self) -> None:
        self.db.close()

    def get(self, ref: str) -> Row | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM dds WHERE internal_reference=?", (ref,)).fetchone()
        return Row(**dict(r)) if r else None

    def upsert(self, ref: str, event: str, detail: Any = None, **fields: Any) -> None:
        now = _now()
        with self.lock:
            exists = self.db.execute("SELECT 1 FROM dds WHERE internal_reference=?", (ref,)).fetchone()
            if exists:
                if fields:
                    cols = ", ".join(f"{k}=?" for k in fields)
                    self.db.execute(
                        f"UPDATE dds SET {cols}, updated_at=? WHERE internal_reference=?",
                        (*fields.values(), now, ref),
                    )
            else:
                fields.setdefault("state", INVALID)
                cols = ", ".join(fields)
                marks = ", ".join("?" for _ in fields)
                self.db.execute(
                    f"INSERT INTO dds (internal_reference, {cols}, created_at, updated_at) "
                    f"VALUES (?, {marks}, ?, ?)",
                    (ref, *fields.values(), now, now),
                )
            self.db.execute(
                "INSERT INTO events (internal_reference, at, event, detail) VALUES (?, ?, ?, ?)",
                (ref, now, event, json.dumps(detail, default=str) if detail is not None else None),
            )

    def bump_attempts(self, ref: str) -> None:
        with self.lock:
            self.db.execute("UPDATE dds SET attempts = attempts + 1 WHERE internal_reference=?", (ref,))

    def rows(self, states: list[str] | None = None) -> list[Row]:
        with self.lock:
            if states:
                marks = ", ".join("?" for _ in states)
                cur = self.db.execute(
                    f"SELECT * FROM dds WHERE state IN ({marks}) ORDER BY created_at", states
                )
            else:
                cur = self.db.execute("SELECT * FROM dds ORDER BY created_at")
            return [Row(**dict(r)) for r in cur.fetchall()]

    def pending_poll(self) -> list[Row]:
        return self.rows([SUBMITTED])

    def counts(self) -> dict[str, int]:
        with self.lock:
            cur = self.db.execute("SELECT state, COUNT(*) FROM dds GROUP BY state")
            return {state: n for state, n in cur.fetchall()}

    def events(self, ref: str) -> list[dict[str, Any]]:
        with self.lock:
            cur = self.db.execute(
                "SELECT at, event, detail FROM events WHERE internal_reference=? ORDER BY id", (ref,)
            )
            return [dict(r) for r in cur.fetchall()]
