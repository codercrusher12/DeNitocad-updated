"""
Sign-In-With-Wallet authentication for nl-to-cad.

Replaced the old identity model (an API key, purchased with a 5 BOT
payment, cached in one browser's localStorage) with a free,
signature-based login that works from any device: connect a wallet,
sign a short challenge message (no gas, no transaction, nothing
on-chain at all), and the server verifies the signature recovers to
the claimed address, then issues a session. The old system (security.py,
db.py's api_keys table, POST /api/keys/generate) has since been fully
removed - see db.py's module docstring and web_app.py's note where
that endpoint used to live - so this is now the only auth path.

Flow:
  1. POST /auth/nonce {wallet_address} -> {nonce, message}
     Client shows `message` to the wallet for personal_sign - exactly
     as returned, unmodified, or verification will fail.
  2. POST /auth/verify {wallet_address, nonce, signature} -> {session_id, expires_at}
     Server re-derives the signing address from the signature and the
     EXACT message it stored at step 1 (never trusts a client-supplied
     copy of the message text), confirms it matches wallet_address,
     and only then issues a session (db.create_session).
  3. Every subsequent request sends `Authorization: Bearer <session_id>`;
     get_current_wallet() below resolves that back to a wallet address.

Session tokens are opaque random strings stored server-side (db.py's
sessions table), not JWTs - chosen deliberately so a session can be
revoked instantly (delete the row) instead of only expiring on a
timer. This app already does a DB round trip per request for job/key
lookups, so the extra session lookup costs nothing new.

A SECOND identity path exists alongside the wallet flow above: Google
sign-in (see google_auth.py and verify_google_and_create_session below,
POST /auth/google). It's additive, not a replacement - Stripe-only (no
wallet to pay BOT/USDT from), issues the exact same kind of session,
under an account_id shaped like "email:{google_sub}" instead of a real
wallet address, which every other identity-keyed table (wallet_credits,
subscriptions, jobs, sessions itself) accepts without any changes,
since none of them actually validate that string as an Ethereum
address - see db.get_or_create_email_account's docstring.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import db
import google_auth
from config import settings
from logging_config import get_logger

logger = get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)


def issue_nonce(wallet_address: str) -> dict:
    """Wraps db.create_nonce for the POST /auth/nonce route."""
    nonce, message = db.create_nonce(wallet_address)
    return {"nonce": nonce, "message": message}


def verify_and_create_session(wallet_address: str, nonce: str, signature: str) -> dict:
    """Wraps db.consume_nonce + signature recovery + db.create_session
    for the POST /auth/verify route. Every failure mode (expired
    nonce, wrong wallet, malformed/wrong signature) raises the same
    401 with the same generic detail, deliberately - distinguishing
    them in the response would let a caller probe whether a given
    nonce exists or was issued for a given address."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    message = db.consume_nonce(
        wallet_address, nonce, ttl_seconds=settings.WALLET_NONCE_TTL_SECONDS
    )
    if message is None:
        raise HTTPException(status_code=401, detail="Invalid, expired, or already-used nonce")

    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:  # noqa: BLE001 - any malformed signature lands here
        logger.warning("wallet signature recovery failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid signature") from exc

    if recovered.lower() != wallet_address.lower():
        raise HTTPException(status_code=401, detail="Signature does not match wallet address")

    session_id, expires_at = db.create_session(
        recovered.lower(), ttl_seconds=settings.WALLET_SESSION_TTL_SECONDS
    )
    return {
        "session_id": session_id,
        "wallet_address": recovered.lower(),
        "expires_at": expires_at,
    }


def verify_google_and_create_session(id_token: str) -> dict:
    """The Google-identity counterpart to verify_and_create_session
    above - same shape of function (verify proof of identity, mint a
    session), different proof (a Google ID token instead of an
    Ethereum signature). Grants the 1-credit email signup bonus exactly
    once, the moment this account_id is created for the first time -
    see db.get_or_create_email_account's docstring for how that
    first-time determination is made atomically, and config.py's
    EMAIL_SIGNUP_BONUS_CREDITS comment for why this bonus is granted
    immediately rather than gated on a first payment the way the
    wallet flow's (much larger) 10-credit bonus is.

    Returns the same {session_id, wallet_address, expires_at} shape as
    verify_and_create_session, plus "email" - the frontend can reuse
    every existing wallet-session function (getWalletSession,
    setWalletSession, the Authorization header on every other call)
    completely unchanged, since as far as those are concerned this is
    just another string in the wallet_address field. "email" is purely
    a display convenience on top, nothing downstream keys on it."""
    claims = google_auth.verify_google_id_token(id_token)
    account_id, is_new = db.get_or_create_email_account(claims["sub"], claims["email"])
    if is_new:
        db.add_credits(account_id, settings.EMAIL_SIGNUP_BONUS_CREDITS)

    session_id, expires_at = db.create_session(account_id, ttl_seconds=settings.WALLET_SESSION_TTL_SECONDS)
    return {
        "session_id": session_id,
        "wallet_address": account_id,
        "expires_at": expires_at,
        "email": claims["email"],
    }


def resolve_session(session_id: str) -> dict | None:
    """Looks up a session_id and returns {"wallet_address": ...,
    "session_id": ...} if valid, None otherwise - no HTTPException, no
    FastAPI dependency machinery, just the lookup. get_current_wallet
    below wraps this for HTTP routes (Authorization header -> 401s);
    a2mcp_botchain/server.py's MCP tools call this directly instead,
    since MCP tool functions take explicit arguments, not FastAPI
    dependency injection, but need the exact same session check."""
    if not session_id:
        return None
    session = db.get_session(session_id)
    if session is None:
        return None
    return {"wallet_address": session["wallet_address"], "session_id": session["session_id"]}


def get_current_wallet(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """FastAPI dependency - raises 401 on a missing/invalid/expired
    session, otherwise returns {"wallet_address": ..., "session_id": ...}.
    The now-removed security.py's get_current_key worked the same way
    for the old API-key path this replaced entirely - see this
    module's docstring."""
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing session. Sign in via POST /auth/nonce then "
            "POST /auth/verify, and send the result back as "
            "'Authorization: Bearer <session_id>'.",
        )

    wallet = resolve_session(creds.credentials)
    if wallet is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return wallet


def get_current_wallet_optional(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict | None:
    """Same as get_current_wallet, but returns None instead of raising
    401 when there's no session - for routes like GET /download/{fmt}/
    {filename} that must serve both anonymous /preview output (public
    by design, no wallet to check) and a paid job's files (must match
    the signed-in wallet). The route itself decides what None means in
    context; this dependency only resolves "who, if anyone, is asking,"
    it doesn't decide whether that's sufficient."""
    if creds is None or not creds.credentials:
        return None
    return resolve_session(creds.credentials)
