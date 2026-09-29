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
                anchor_tx TEXT,
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
        # Subscription credits are a SEPARATE pool from wallet_credits
        # (purchased credits), not a modifier on the same balance - see
        # consume_credit_waterfall's docstring for why they can't share
        # a column: subscription credits reset to 100 every renewal and
        # expire at period end, purchased credits never expire and never
        # reset. Mixing them into one number would make either "reset to
        # 100" wipe out credits the user paid real money for, or "never
        # expires" make the subscription's own reset a no-op.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                wallet_address TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'stripe',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                email TEXT,
                paystack_customer_code TEXT,
                paystack_subscription_code TEXT,
                paystack_plan_code TEXT,
                status TEXT NOT NULL DEFAULT 'inactive',
                tier TEXT NOT NULL DEFAULT 'starter',
                subscription_credits INTEGER NOT NULL DEFAULT 0,
                current_period_end TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        # One row per wallet, ever - the PRIMARY KEY is what makes
        # claim_signup_bonus's INSERT a one-time-only atomic claim
        # instead of a check-then-credit race. qualifying_payment_id is
        # kept for audit (which tx/invoice/session actually unlocked
        # this), not re-validated on read.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_bonus_claims (
                wallet_address TEXT PRIMARY KEY,
                claimed_at TEXT NOT NULL,
                qualifying_payment_id TEXT NOT NULL
            )
            """
        )
        # A SECOND identity path alongside wallet sign-in, not a
        # replacement - see get_or_create_email_account's docstring.
        # account_id is what gets used everywhere else in this file
        # (wallet_credits.wallet_address, subscriptions.wallet_address,
        # jobs.user_id, sessions.wallet_address, all of it) - those
        # tables never learn a new identity type exists, they just see
        # another string in a column that was always just TEXT. Only
        # this table and google_auth.py know Google was involved at
        # all. Keyed on google_sub, NOT email - see this module's own
        # docstring on why sub is the permanent identity and email
        # isn't.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_accounts (
                account_id TEXT PRIMARY KEY,
                google_sub TEXT UNIQUE NOT NULL,
                email TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_wallet_address ON sessions(wallet_address)")
        # jobs(user_id, created_at) backs charge_generation's rolling-24h
        # COUNT(*) query - the two single-column indexes above already
        # help, but a composite index matching that query's exact WHERE
        # shape (user_id = ? AND created_at > ?) avoids a merge of two
        # index scans on every /generate call from a subscribed wallet.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user_id, created_at)")
        # subscriptions.tier didn't exist before the three-tier model -
        # ALTER TABLE ADD COLUMN on a table that already has it raises
        # OperationalError, so this is guarded rather than IF NOT EXISTS
        # (sqlite's ALTER TABLE has no such clause). Safe to run every
        # startup: a fresh CREATE TABLE above already includes the
        # column via subscriptions' own definition below, so this only
        # ever fires against an old deployed file that predates tiers.
        try:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN tier TEXT NOT NULL DEFAULT 'starter'")
        except sqlite3.OperationalError:
            pass
        # Same reasoning, same pattern, for the columns the Paystack
        # switch needs on an already-deployed subscriptions table - see
        # get_or_create_paystack... functions below and config.py's
        # Paystack comment block for why each of these exists.
        for column_sql in (
            "ALTER TABLE subscriptions ADD COLUMN provider TEXT NOT NULL DEFAULT 'stripe'",
            "ALTER TABLE subscriptions ADD COLUMN email TEXT",
            "ALTER TABLE subscriptions ADD COLUMN paystack_customer_code TEXT",
            "ALTER TABLE subscriptions ADD COLUMN paystack_subscription_code TEXT",
            "ALTER TABLE subscriptions ADD COLUMN paystack_plan_code TEXT",
            # On-chain provenance tx hash (DesignRegistry.anchorDesign) - set
            # after a paid STEP export succeeds. Existing DBs lack this column.
            "ALTER TABLE jobs ADD COLUMN anchor_tx TEXT",
        ):
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass
        # Idempotency for paystack_pay.py's charge.success handler - see
        # mark_paystack_reference_processed's docstring. A transaction
        # reference is unique per Paystack transaction (their guarantee,
        # not ours), so it's exactly the right PRIMARY KEY: the same
        # reference can never legitimately need processing twice.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paystack_processed_references (
                reference TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
            """
        )
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


def set_job_anchor_tx(job_id: str, tx_hash: str) -> None:
    """Persist the DesignRegistry anchor transaction hash on the job row
    so the customer can open it on the explorer later (My Projects, STEP
    export response headers). Idempotent: later calls overwrite."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET anchor_tx = ? WHERE id = ?",
            (tx_hash, job_id),
        )


def find_remote_url_for_filename(filename: str, fmt: str) -> str | None:
    """Best-effort lookup of a durable URL for a local output filename.

    Local files are named ``{part_type}_{uuid}.stl`` while R2 keys use a
    different uuid, so object-key matching is impossible. When the stored
    URL itself still ends with the local filename (the non-R2 fallback
    path ``/download/{fmt}/{filename}``), return that. Otherwise return
    None — callers with a job_id should use GET /download/job/{job_id}/{fmt}.
    """
    col = "stl_url" if fmt == "stl" else "step_url"
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {col} AS url FROM jobs WHERE {col} LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f"%/{filename}%",),
        ).fetchone()
        return row["url"] if row and row["url"] else None


def list_jobs(user_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def find_job_owner(filename: str) -> str | None:
    """Resolve a /download/{fmt}/{filename} request back to whichever
    job produced that file, for the ownership check web_app.py's
    download routes need before streaming a private artifact. Two
    naming schemes exist on disk, so this checks both:

    On-demand exports (cad_generator.export_format_for_job - STEP,
    IGES, DXF, PDF, and STL fetched via /export/stl/{job_id} rather
    than inline) name files "{job_id}_{fmt}.{ext}" - job_id parses
    straight out of the filename (it's a real uuid4, hyphenated), no
    DB round trip needed for the common case.

    The inline STL /generate and /preview build at generation time
    (cad_generator._generate_single_part) instead names files
    "{part_type}_{uuid4().hex}.stl" - a DIFFERENT, non-job-id uuid
    (no hyphens), so that prefix never parses as a job id. Those are
    found by matching the filename against the jobs table's own
    stored stl_url/step_url column instead, which is populated at
    record_job time with whatever URL the file was actually served at.

    Returns the owning job's user_id (a wallet address, or the literal
    string "anonymous" for a /preview job - see web_app.py's own
    handling of that value), or None if no job claims this filename at
    all."""
    import uuid as _uuid
    from pathlib import Path as _Path

    candidate_job_id = _Path(filename).stem.rsplit("_", 1)[0]
    try:
        _uuid.UUID(candidate_job_id)
    except ValueError:
        pass
    else:
        job = get_job(candidate_job_id)
        if job is not None:
            return job["user_id"]

    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM jobs WHERE step_url LIKE ? OR stl_url LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f"%/{filename}", f"%/{filename}"),
        ).fetchone()
        return row["user_id"] if row else None


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
    never has a balance).

    This spends PURCHASED credits only. For the subscription-aware
    spend order (subscription credits first, this pool second), use
    consume_credit_waterfall instead - this function is kept as-is
    because add_credits/consume_credit are still the right pair for
    purchased credits specifically, and other code may reasonably want
    to spend from that pool alone."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE wallet_credits SET balance = balance - ?, updated_at = ?
            WHERE wallet_address = ? AND balance >= ?
            """,
            (amount, _now(), wallet_address.lower(), amount),
        )
        return cur.rowcount > 0


def get_subscription(wallet_address: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE wallet_address = ?",
            (wallet_address.lower(),),
        ).fetchone()
        return dict(row) if row else None


def get_subscription_by_stripe_customer_id(stripe_customer_id: str) -> dict[str, Any] | None:
    """Stripe's invoice/subscription webhook events carry a customer id,
    never our wallet_address - this is how stripe_pay.py's webhook
    handler maps one back to the other. Relies on
    checkout.session.completed having already run upsert_subscription
    once (with whatever status it has at that point) to create the
    wallet_address<->stripe_customer_id link in the first place; an
    invoice event for a customer_id with no matching row here means
    that linking step hasn't happened yet, which stripe_pay.py treats
    as an error worth logging loudly, not silently ignoring."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
        return dict(row) if row else None


def get_subscription_by_paystack_customer_code(paystack_customer_code: str) -> dict[str, Any] | None:
    """The Paystack counterpart to get_subscription_by_stripe_customer_id -
    same role: a renewal charge.success webhook carries Paystack's
    customer_code, never our wallet_address, so this is how
    paystack_pay.py's webhook handler resolves one back to the other.
    Only works for a customer_code this app has already linked to a
    wallet - see upsert_paystack_subscription, which is what creates
    that link on the FIRST successful charge (the one carrying our own
    metadata.wallet_address, set at POST /transaction/initialize)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE paystack_customer_code = ?",
            (paystack_customer_code,),
        ).fetchone()
        return dict(row) if row else None


def mark_paystack_reference_processed(reference: str) -> bool:
    """Returns True the FIRST time this transaction reference is seen,
    False every time after - the idempotency guard paystack_pay.py's
    charge.success handler checks before granting anything. Paystack
    (like most webhook senders) can and does redeliver the same event
    - a timeout on your end, a retry after a non-200 response, or just
    their own at-least-once delivery guarantee - and a transaction
    reference is unique per Paystack transaction by THEIR guarantee,
    which is exactly what makes it safe as a PRIMARY KEY here: the
    INSERT itself is the atomic check, not a SELECT-then-INSERT that a
    concurrent redelivery could race."""
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO paystack_processed_references (reference, processed_at) VALUES (?, ?)",
                (reference, _now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def upsert_paystack_subscription(
    wallet_address: str,
    *,
    email: str,
    paystack_customer_code: str,
    paystack_subscription_code: str,
    paystack_plan_code: str,
    status: str,
    tier: str,
    subscription_credits: int,
    current_period_end: str | None,
) -> None:
    """The Paystack counterpart to upsert_subscription - same shape,
    called on the first successful charge AND on every renewal charge,
    always sets provider='paystack' so charge_generation/
    is_subscription_entitled (both provider-agnostic - they only read
    tier/subscription_credits/status/current_period_end) don't need to
    know or care which rail granted the period. email/paystack_customer_
    code/paystack_subscription_code/paystack_plan_code exist ONLY for
    reconciliation and support lookups - the wallet stays the identity
    throughout (see config.py's Paystack comment block); a webhook is
    resolved back to a wallet via get_subscription_by_paystack_customer_
    code, never by re-deriving anything from the email."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions (
                wallet_address, provider, email, paystack_customer_code,
                paystack_subscription_code, paystack_plan_code, status, tier,
                subscription_credits, current_period_end, updated_at
            ) VALUES (?, 'paystack', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                provider = 'paystack',
                email = excluded.email,
                paystack_customer_code = excluded.paystack_customer_code,
                paystack_subscription_code = excluded.paystack_subscription_code,
                paystack_plan_code = excluded.paystack_plan_code,
                status = excluded.status,
                tier = excluded.tier,
                subscription_credits = excluded.subscription_credits,
                current_period_end = excluded.current_period_end,
                updated_at = excluded.updated_at
            """,
            (
                wallet_address.lower(),
                email,
                paystack_customer_code,
                paystack_subscription_code,
                paystack_plan_code,
                status,
                tier,
                subscription_credits,
                current_period_end,
                _now(),
            ),
        )


def upsert_subscription(
    wallet_address: str,
    *,
    stripe_customer_id: str,
    stripe_subscription_id: str,
    status: str,
    tier: str,
    subscription_credits: int,
    current_period_end: str | None,
) -> None:
    """Called on checkout completion AND on every renewal invoice - both
    are "start a fresh subscription_credits allotment for a period",
    the only difference is whether a row already existed. subscription_
    credits is passed in rather than always hardcoded to 100 here, so
    the plan's credit amount can change later without editing this
    function. Overwrites (does not add to) any leftover
    subscription_credits from the prior period - see consume_credit_
    waterfall's docstring: unused subscription credits do NOT roll
    over, this is where that reset actually happens. tier is one of
    config.py's settings.subscription_tiers keys ("starter",
    "engineer", "professional") - charge_generation reads it back to
    find the right daily_generation_cap and pool-vs-no-pool behavior
    for this wallet."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions (
                wallet_address, stripe_customer_id, stripe_subscription_id,
                status, tier, subscription_credits, current_period_end, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                stripe_customer_id = excluded.stripe_customer_id,
                stripe_subscription_id = excluded.stripe_subscription_id,
                status = excluded.status,
                tier = excluded.tier,
                subscription_credits = excluded.subscription_credits,
                current_period_end = excluded.current_period_end,
                updated_at = excluded.updated_at
            """,
            (
                wallet_address.lower(),
                stripe_customer_id,
                stripe_subscription_id,
                status,
                tier,
                subscription_credits,
                current_period_end,
                _now(),
            ),
        )


def set_subscription_status(stripe_subscription_id: str, status: str) -> None:
    """Called on a Stripe webhook that changes status without granting a
    new period (cancellation taking effect, payment failure, etc.) -
    looked up by stripe_subscription_id since these webhooks don't
    carry our wallet_address. Deliberately does NOT touch
    subscription_credits: per the agreed model, credits earned in the
    current period stay spendable until current_period_end regardless
    of status, they're cleared by the *next* period simply never
    arriving (upsert_subscription is never called again), not by this
    function zeroing them early."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET status = ?, updated_at = ? WHERE stripe_subscription_id = ?",
            (status, _now(), stripe_subscription_id),
        )


def set_paystack_subscription_status(paystack_subscription_code: str, status: str) -> None:
    """The Paystack counterpart to set_subscription_status - same
    reasoning: called on subscription.disable / subscription.not_renew,
    which change status without granting a new period, looked up by
    paystack_subscription_code since those webhooks carry that, not our
    wallet_address. Same deliberate omission too: does not touch
    subscription_credits, which stay spendable until current_period_end
    regardless of status - see set_subscription_status's docstring."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET status = ?, updated_at = ? WHERE paystack_subscription_code = ?",
            (status, _now(), paystack_subscription_code),
        )


def set_paystack_subscription_code(
    paystack_customer_code: str, paystack_subscription_code: str, paystack_plan_code: str
) -> None:
    """Deliberately narrow - touches ONLY these two columns, unlike
    upsert_paystack_subscription which overwrites the whole row.
    Paystack's charge.success and subscription.create webhooks both
    fire for a brand-new subscription with no guaranteed order; charge.
    success (via paystack_pay._on_charge_success) is what grants
    credits/allowance and sets current_period_end, and it may arrive
    before subscription.create has told us the subscription_code at
    all. Using the full upsert here instead would risk re-writing
    tier/subscription_credits/current_period_end with whatever this
    narrower event happens to know (often nothing useful for those
    fields), clobbering what charge.success already correctly set."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET paystack_subscription_code = ?, paystack_plan_code = ?, updated_at = ? "
            "WHERE paystack_customer_code = ?",
            (paystack_subscription_code, paystack_plan_code, _now(), paystack_customer_code),
        )


def is_subscription_entitled(wallet_address: str) -> bool:
    """Whether this wallet currently gets subscription-gated features
    (prompt refinement, file exports - whatever product decides those
    mean, see the open question flagged separately). Checks BOTH status
    and current_period_end: status can lag reality by up to a webhook
    delivery delay, but current_period_end is the actual boundary
    Stripe already charged for, so a wallet stays entitled through the
    period it paid for even if e.g. the cancellation webhook arrives a
    few seconds early."""
    sub = get_subscription(wallet_address)
    if sub is None or not sub["current_period_end"]:
        return False
    if sub["status"] not in ("active", "trialing"):
        return False
    return datetime.fromisoformat(sub["current_period_end"]) > datetime.now(timezone.utc)


def consume_credit_waterfall(wallet_address: str, amount: int = 1) -> str | None:
    """Spends `amount` credits, subscription pool first, purchased pool
    second - the agreed order, so a subscriber's included credits get
    used before anything they separately paid for. Both attempts run
    inside the SAME connection (one `with get_conn()` block, one
    implicit transaction), which matters: checking the subscription
    balance and falling back to the purchased balance are two
    statements, and without a shared transaction a second concurrent
    call for the same wallet could interleave between them and double-
    spend the same purchased credit that two "subscription insufficient"
    checks both just saw. Returns "subscription" or "purchased" for
    which pool paid, or None if neither had enough."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE subscriptions SET subscription_credits = subscription_credits - ?, updated_at = ?
            WHERE wallet_address = ? AND subscription_credits >= ?
                AND status IN ('active', 'trialing')
                AND current_period_end > ?
            """,
            (amount, _now(), wallet_address.lower(), amount, _now()),
        )
        if cur.rowcount > 0:
            return "subscription"

        cur = conn.execute(
            """
            UPDATE wallet_credits SET balance = balance - ?, updated_at = ?
            WHERE wallet_address = ? AND balance >= ?
            """,
            (amount, _now(), wallet_address.lower(), amount),
        )
        if cur.rowcount > 0:
            return "purchased"

        return None


def count_generations_last_24h(wallet_address: str) -> int:
    """Backs every subscription tier's daily_generation_cap. Deliberately
    a rolling 24 hours from "now", not a UTC-midnight counter column -
    see the conversation that settled this: no timezone to store per
    wallet, no midnight-reset cliff, at the cost of being a COUNT query
    over jobs rather than an O(1) counter compare. Counts real,
    successful, credit/allowance-consuming generations only - reuses
    the jobs table's own user_id + created_at rather than a separate
    log table, since every /generate call already writes a jobs row
    keyed by the wallet address (anonymous /preview jobs use user_id
    "anonymous" and never match a real wallet address here, so they
    never count against anyone's cap)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE user_id = ? AND created_at > ?",
            (wallet_address.lower(), cutoff),
        ).fetchone()
        return row["c"]


def charge_generation(wallet_address: str) -> str | None:
    """The full policy for what pays for one /generate call, given this
    wallet's current subscription (if any). Looks up the tier's config
    itself (settings.subscription_tiers) rather than making the caller
    pass it in - web_app.py just calls charge_generation(wallet), full
    stop. Returns which mechanism paid:

        "subscription_daily" - Engineer/Professional: no credit pool at
            all (tier_config["monthly_credits"] == 0), generation is
            free and uncounted against any balance, as long as
            count_generations_last_24h is under daily_generation_cap.

        "subscription_credit" - Starter: paid from the subscription_
            credits pool, gated by BOTH the pool having enough left AND
            still being under daily_generation_cap - the cap is a
            throttle on the pool here, not a separate free allowance.

        "purchased" - used whenever there's no active subscription, or
            the active one's relevant limit (today's cap, or the
            pool) is already used up for this wallet. A paying
            subscriber who goes over their daily/pool limit falls back
            to spending their own purchased credits rather than being
            hard-blocked until the window clears - see web_app.py's
            /generate docstring for why that's the chosen default,
            easy to flip.

        None - nothing left to charge; caller should 402.

    KNOWN, ACCEPTED RACE: the daily-cap check (count_generations_last_24h)
    and the jobs-row insert that makes a generation count happen in two
    separate steps, not one atomic statement (unlike the credit-pool
    checks below, which use UPDATE...WHERE and so remain atomic).
    Two concurrent /generate calls for the same wallet, sitting exactly
    at cap - 1 remaining, could both read "under cap" and both proceed,
    letting a subscriber generate one extra than their cap that instant.
    Same category as design_registry.py's documented nonce race:
    self-limiting (costs nothing but a slightly generous cap enforcement
    for that wallet, not a cross-wallet or revenue-losing bug), not
    worth a row-locking scheme at this scale. Left on the same backlog
    if daily concurrent volume per wallet ever becomes real."""
    sub = get_subscription(wallet_address)
    if sub is not None and is_subscription_entitled(wallet_address):
        tier_config = settings.subscription_tiers.get(sub["tier"])
        if tier_config is None:
            # Subscribed to a tier name that no longer exists in
            # config.py (renamed/removed) - fail safe to purchased
            # credits below rather than crashing on a KeyError.
            logger.error("subscription %s has unknown tier %r", wallet_address, sub["tier"])
        else:
            cap = tier_config["daily_generation_cap"]
            used_today = count_generations_last_24h(wallet_address)

            if tier_config["monthly_credits"] > 0:
                if used_today < cap:
                    with get_conn() as conn:
                        cur = conn.execute(
                            "UPDATE subscriptions SET subscription_credits = subscription_credits - 1, updated_at = ? "
                            "WHERE wallet_address = ? AND subscription_credits >= 1",
                            (_now(), wallet_address.lower()),
                        )
                        if cur.rowcount > 0:
                            return "subscription_credit"
            else:
                if used_today < cap:
                    return "subscription_daily"

    if consume_credit(wallet_address):
        return "purchased"
    return None


def claim_signup_bonus(wallet_address: str, qualifying_payment_id: str) -> bool:
    """One-time 10-credit bonus, unlocked by the wallet's first verified
    payment (BOT, USDT, or Stripe) - not by connecting a wallet, see
    the earlier discussion: a wallet costs nothing to generate, so
    gating on wallet creation alone has no real economic friction and
    would just be farmed. signup_bonus_claims.wallet_address is a
    PRIMARY KEY, so the INSERT itself is the one-time guarantee - two
    concurrent calls for the same wallet can't both succeed, whichever
    one hits the constraint second gets sqlite3.IntegrityError and this
    returns False without granting credits twice. Call this AFTER the
    payment is already verified and recorded, never before - passing
    a payment that turns out invalid would grant free credits for
    nothing."""
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO signup_bonus_claims (wallet_address, claimed_at, qualifying_payment_id) VALUES (?, ?, ?)",
                (wallet_address.lower(), _now(), qualifying_payment_id),
            )
        except sqlite3.IntegrityError:
            return False

    add_credits(wallet_address, 10)
    return True


def get_or_create_email_account(google_sub: str, email: str) -> tuple[str, bool]:
    """The entry point for the Google sign-in identity path - called on
    EVERY Google sign-in, not just the first one, same as how a wallet
    calls db.create_session on every sign-in regardless of whether
    that wallet's been seen before. Returns (account_id, is_new_account).

    account_id is "email:{google_sub}" - a string shaped nothing like a
    real wallet address, deliberately: nothing downstream (wallet_credits,
    subscriptions, jobs, sessions - see this table's own comment in
    init_db) validates that its identity column IS an Ethereum address,
    they just treat it as an opaque TEXT primary key, so reusing that
    exact machinery for a non-wallet identity works as long as the two
    kinds of identifier can never collide. They can't: a real wallet
    address is always "0x" + 40 hex chars, never contains a literal
    colon.

    is_new_account is True only the FIRST time this google_sub is ever
    seen - the INSERT's UNIQUE(google_sub) constraint makes that
    determination atomic (two simultaneous sign-ins for a brand-new
    Google account can't both see is_new=True), the same pattern
    claim_signup_bonus uses for wallets. The caller (wallet_auth.
    verify_google_and_create_session) uses is_new_account to decide
    whether to grant the 1-credit email signup bonus - NOT gated on a
    first payment the way the wallet flow's 10-credit bonus is, see
    config.py's EMAIL_SIGNUP_BONUS_CREDITS comment for why that's a
    deliberately different tradeoff for this identity type."""
    account_id = f"email:{google_sub}"
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO email_accounts (account_id, google_sub, email, created_at) VALUES (?, ?, ?, ?)",
                (account_id, google_sub, email, _now()),
            )
            return account_id, True
        except sqlite3.IntegrityError:
            # Already exists - update the stored email in case it
            # changed at Google's end since last sign-in (sub is
            # permanent, email is not - see this module's docstring).
            conn.execute(
                "UPDATE email_accounts SET email = ? WHERE account_id = ?",
                (email, account_id),
            )
            return account_id, False
