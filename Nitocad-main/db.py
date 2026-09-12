"""
Persistence layer for nl-to-cad, using Python's stdlib sqlite3 -
deliberately not SQLAlchemy/Postgres.

Why sqlite3 and not the SQLAlchemy/Postgres pattern used in Stitchfren:
CAD generation here runs in 1-3 seconds, synchronously, with no Celery
queue. There's no multi-worker fan-out that needs a real network database.
A single-file sqlite DB is genuinely the right-sized choice for this scale,
and it's trivial to swap for Postgres later (same table shapes) if this
ever needs multi-instance horizontal scaling.

Two tables:
  - jobs: every generation request, input/output, for audit history
    (an OKX ASP listing should be able to show what it did and when).
  - api_keys: hashed keys only, never the raw key, same principle as
    Stitchfren's app/core/security.py.

On Railway, the sqlite file itself is still subject to the same ephemeral-
filesystem problem as generated CAD files - see the DB_PATH note below.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from config import settings
from logging_config import get_logger

logger = get_logger(__name__)

# On Railway, mount a persistent volume and point DB_PATH at it
# (e.g. "/data/nl-to-cad.db"). Left as a bare filename by default for local
# dev, where the working directory is stable between runs. Resolved via
# config.settings (not a bare os.getenv here) so it's overridable in tests
# via config.get_settings.cache_clear() + monkeypatched env, and so every
# module agrees on the same value - see config.py's module docstring.
DB_PATH = settings.DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Call once at startup."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                description TEXT NOT NULL,
                part_type TEXT,
                parameters TEXT,
                material TEXT,
                used_deepseek INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL,
                error TEXT,
                step_url TEXT,
                stl_url TEXT,
                warnings TEXT,
                corrections TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                hashed_key TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL DEFAULT 'default',
                is_active INTEGER NOT NULL DEFAULT 1,
                usage_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                tx_hash TEXT PRIMARY KEY,
                purpose TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                verified_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id)")
        # api_keys.hashed_key already has an implicit index from its UNIQUE
        # constraint - no separate CREATE INDEX needed for that lookup.
    logger.info("database ready at %s", DB_PATH)


def check_connection() -> bool:
    """Used by the /healthz readiness check - a cheap round trip that
    proves the sqlite file is reachable and not locked/corrupted, not
    just that the path string is set."""
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
        return True
    except sqlite3.Error:
        logger.exception("database health check failed")
        return False


# ---------------------------------------------------------------- jobs ----

def record_job(
    *,
    description: str,
    success: bool,
    user_id: str = "default",
    part_type: str | None = None,
    parameters: dict | None = None,
    material: str | None = None,
    used_deepseek: bool = False,
    error: str | None = None,
    step_url: str | None = None,
    stl_url: str | None = None,
    warnings: list | None = None,
    corrections: dict | None = None,
) -> str:
    """Persist one generation job and return its id (a uuid4 string, also
    used as the on-disk/R2 filename stem - see storage.py)."""
    job_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, description, part_type, parameters, material,
                used_deepseek, success, error, step_url, stl_url,
                warnings, corrections, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                user_id,
                description,
                part_type,
                json.dumps(parameters) if parameters is not None else None,
                material,
                1 if used_deepseek else 0,
                1 if success else 0,
                error,
                step_url,
                stl_url,
                json.dumps(warnings) if warnings is not None else None,
                json.dumps(corrections) if corrections is not None else None,
                _now(),
            ),
        )
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs(user_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------- api keys ----

def create_api_key(user_id: str = "default") -> str:
    """Generate, hash, and persist a new API key. Returns the raw key -
    shown to the caller exactly once, never stored or logged in plain
    text again after this call returns."""
    import hashlib
    import secrets

    raw_key = secrets.token_urlsafe(32)
    hashed_key = hashlib.sha256(raw_key.encode()).hexdigest()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO api_keys (id, hashed_key, user_id, is_active, usage_count, created_at)
            VALUES (?, ?, ?, 1, 0, ?)
            """,
            (str(uuid.uuid4()), hashed_key, user_id, _now()),
        )
    return raw_key


def validate_api_key(raw_key: str) -> dict[str, Any] | None:
    """Returns the key row (as a dict) if valid and active, else None.
    Bumps usage_count/last_used_at on success."""
    import hashlib

    if not raw_key:
        return None
    hashed_key = hashlib.sha256(raw_key.encode()).hexdigest()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE hashed_key = ?", (hashed_key,)
        ).fetchone()
        if row is None or not row["is_active"]:
            return None
        conn.execute(
            "UPDATE api_keys SET usage_count = usage_count + 1, last_used_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        result = dict(row)
        result["usage_count"] += 1
        return result


# ---------------------------------------------------------------- payments ----
# Backing store for botchain_pay.py's on-chain payment verification.
# tx_hash as PRIMARY KEY is the entire double-spend/replay guarantee:
# reserve_payment() is called BEFORE any RPC verification work, so two
# concurrent requests carrying the same tx_hash can never both pass -
# the loser fails on the INSERT itself, atomically, via sqlite's own
# uniqueness constraint. No application-level lock needed.

def reserve_payment(tx_hash: str, *, purpose: str, user_id: str) -> bool:
    """Atomically claim a tx hash before spending any time verifying it
    on-chain. Returns False if already reserved/used."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO payments (tx_hash, purpose, user_id, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (tx_hash, purpose, user_id, _now()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def mark_payment_verified(tx_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE payments SET status = 'verified', verified_at = ? WHERE tx_hash = ?",
            (_now(), tx_hash),
        )


def release_payment(tx_hash: str) -> None:
    """Drop a reservation that's retryable (not yet mined, not enough
    confirmations) - lets the same tx hash be submitted again once
    it's actually ready, instead of permanently burning it on a timing
    issue."""
    with get_conn() as conn:
        conn.execute("DELETE FROM payments WHERE tx_hash = ? AND status = 'pending'", (tx_hash,))


def invalidate_payment(tx_hash: str) -> None:
    """Mark a reservation permanently unusable - wrong recipient, wrong
    amount, wrong sender, or reverted. Unlike release_payment, does NOT
    delete the row: this tx hash must never be usable again."""
    with get_conn() as conn:
        conn.execute("UPDATE payments SET status = 'invalid' WHERE tx_hash = ?", (tx_hash,))


def get_payment(tx_hash: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM payments WHERE tx_hash = ?", (tx_hash,)).fetchone()
        return dict(row) if row else None
