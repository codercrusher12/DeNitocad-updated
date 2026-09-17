"""
Persistence layer for nl-to-cad, using Python's stdlib sqlite3 -
deliberately not SQLAlchemy/Postgres.

Why sqlite3 and not the SQLAlchemy/Postgres pattern used in Stitchfren:
CAD generation here runs in 1-3 seconds, synchronously, with no Celery
queue. There's no multi-worker fan-out that needs a real network database.
A single-file sqlite DB is genuinely the right-sized choice for this scale,
and it's trivial to swap for Postgres later (same table shapes) if this
ever needs multi-instance horizontal scaling.

Tables:
  - jobs: every generation request, input/output, for audit history
    (an OKX ASP listing should be able to show what it did and when).

nonces and sessions back wallet_auth.py's Sign-In-With-Wallet flow (a
free, signature-based login): nonces are one-time challenges a wallet
signs to prove it holds an address, sessions are the result of a
successfully verified signature. See wallet_auth.py's module docstring
for the full flow - this file only holds the storage, not the crypto.

(There used to be a third table here, api_keys - hashed keys, never
the raw key, same principle as Stitchfren's app/core/security.py. It's
gone: wallet_auth.py's free sign-in replaced the "pay 5 BOT for an API
key cached in one browser" model entirely, and /generate, /export, and
/api/jobs* all authenticate via wallet_auth.get_current_wallet now.
Going forward only, per the project's own migration decision - no
conversion path for old keys, and nothing in this file reads the old
table's data anymore even if it's still sitting in an existing
deployed sqlite file.)

Plus wallet_credits: a prepaid-generation balance per wallet address,
replacing the old "pay BOTCHAIN_PER_CALL_PRICE_BOT fresh on every
/generate call" model. Every purchase (single/pack_1000/pack_10000 -
see web_app.py's POST /credits/purchase) adds credits; every
successful /generate decrements exactly 1, atomically, so pay-as-you-
go and bulk packs are the same mechanism underneath, not three
separate code paths.

On Railway, the sqlite file itself is still subject to the same ephemeral-
filesystem problem as generated CAD files - see the DB_PATH note below.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
        # api_keys table removed here (see module docstring) - init_db()
        # deliberately does NOT drop it from any existing deployed sqlite
        # file (a DROP is destructive and irreversible; simply never
        # creating/reading it again is enough for "going forward only").
        # A fresh deploy's sqlite file just never gets the table at all.
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nonces (
                nonce TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_credits (
                wallet_address TEXT PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_wallet_address ON sessions(wallet_address)")
        # nonces.nonce, sessions.session_id, and wallet_credits.wallet_address
        # are all PRIMARY KEY, so each already has an implicit index -
        # no separate CREATE INDEX needed for those lookups.
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


# ------------------------------------------------------- wallet auth ----
# Backing store for wallet_auth.py's Sign-In-With-Wallet flow. Two
# tables: nonces (one-time challenges, short-lived, one per login
# attempt) and sessions (the result of a successfully verified
# signature, longer-lived). See wallet_auth.py's module docstring for
# how these get used - this is storage only, no crypto here.

def create_nonce(wallet_address: str) -> tuple[str, str]:
    """Generates a fresh nonce and the exact message text a wallet must
    sign, stores both, and returns (nonce, message). The message is
    stored verbatim rather than reconstructed later from a template at
    verify time, so verification can never drift from what the user
    actually saw and signed."""
    import secrets

    nonce = secrets.token_hex(16)
    message = (
        "Sign in to NitoCAD.\n\n"
        f"Wallet: {wallet_address.lower()}\n"
        f"Nonce: {nonce}\n"
        f"Issued: {_now()}\n\n"
        "This request will not trigger a blockchain transaction or cost any gas."
    )
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO nonces (nonce, wallet_address, message, created_at, used) "
            "VALUES (?, ?, ?, ?, 0)",
            (nonce, wallet_address.lower(), message, _now()),
        )
    return nonce, message


def consume_nonce(wallet_address: str, nonce: str, *, ttl_seconds: int) -> str | None:
    """Single-use, address-bound, time-limited. Returns the original
    signed message text on success (the caller needs it to verify the
    signature against), or None if the nonce is missing, already used,
    expired, or was issued for a different address - deliberately not
    distinguishing which of those, so a failure here can't be used to
    probe which nonces exist. Marks it used immediately on a match,
    before signature verification even runs, so it can never be spent
    twice regardless of how verification turns out."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM nonces WHERE nonce = ?", (nonce,)).fetchone()
        if row is None or row["used"] or row["wallet_address"] != wallet_address.lower():
            return None
        created = datetime.fromisoformat(row["created_at"])
        if (datetime.now(timezone.utc) - created).total_seconds() > ttl_seconds:
            return None
        conn.execute("UPDATE nonces SET used = 1 WHERE nonce = ?", (nonce,))
        return row["message"]


def create_session(wallet_address: str, *, ttl_seconds: int) -> tuple[str, str]:
    """Returns (session_id, expires_at). session_id is an opaque random
    token, not a JWT - see wallet_auth.py's module docstring for why
    (instant revocation via a DELETE, not just a short expiry window)."""
    import secrets

    session_id = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, wallet_address, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, wallet_address.lower(), _now(), expires_at),
        )
    return session_id, expires_at


def get_session(session_id: str) -> dict[str, Any] | None:
    """Returns the session row if it exists and hasn't expired. Lazily
    deletes it if expired instead of relying on a separate cleanup job
    - this is the only place expiry is actually checked, so there's no
    risk of a cron job and this check disagreeing on what's valid."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return None
        return dict(row)


def delete_session(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


# ---------------------------------------------------- wallet credits ----
# A prepaid-generation balance per wallet, replacing the old "pay fresh
# on every /generate call" model. add_credits() is called after a
# verified payment (see web_app.py's POST /credits/purchase);
# consume_credit() is called once per successful /generate. Pay-as-you-
# go is just "buy 1 credit, immediately spend it" - same mechanism as
# a 10000-credit pack, not a separate code path.

def get_credit_balance(wallet_address: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT balance FROM wallet_credits WHERE wallet_address = ?",
            (wallet_address.lower(),),
        ).fetchone()
        return row["balance"] if row else 0


def add_credits(wallet_address: str, amount: int) -> int:
    """Upsert: creates the wallet's row on its first purchase, or adds
    to an existing balance. Returns the new balance. amount must be
    positive - this is a top-up function, not a general adjuster (use
    consume_credit to go the other direction, which has its own
    atomicity guarantee that a bare add_credits(-1) wouldn't)."""
    if amount <= 0:
        raise ValueError("add_credits amount must be positive")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO wallet_credits (wallet_address, balance, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                balance = balance + excluded.balance,
                updated_at = excluded.updated_at
            """,
            (wallet_address.lower(), amount, _now()),
        )
        row = conn.execute(
            "SELECT balance FROM wallet_credits WHERE wallet_address = ?",
            (wallet_address.lower(),),
        ).fetchone()
        return row["balance"]


def consume_credit(wallet_address: str, amount: int = 1) -> bool:
    """Atomically decrements the wallet's balance by `amount` IF it has
    enough - the WHERE clause's balance >= ? makes the check-and-
    decrement a single statement, so two concurrent /generate calls for
    a wallet sitting at exactly 1 credit can't both succeed (whichever
    UPDATE's WHERE clause loses the race matches zero rows and returns
    False, the same way db.reserve_payment's tx_hash PRIMARY KEY
    prevents a payment from being double-spent). Returns True if the
    balance was sufficient and got decremented, False otherwise
    (including a wallet with no row at all - never spent anything, so
    never has a balance)."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE wallet_credits SET balance = balance - ?, updated_at = ?
            WHERE wallet_address = ? AND balance >= ?
            """,
            (amount, _now(), wallet_address.lower(), amount),
        )
        return cur.rowcount > 0
