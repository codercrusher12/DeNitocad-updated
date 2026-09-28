"""
Google Sign-In verification: takes the ID token Google Identity Services
hands the browser after a Google sign-in, verifies it was actually signed
by Google for THIS app's client id (not some other site's), and returns
the two fields anything downstream needs - the account's stable id and
its email.

This is deliberately the only thing this module does. Session creation,
the 1-credit signup bonus, and the "is this a brand-new account"
decision all live in wallet_auth.py's verify_google_and_create_session
and db.get_or_create_email_account - kept out of here so this module
stays a pure "is this token real" check, easy to reason about and to
swap libraries on without touching account logic.

Why `sub`, not `email`, is the account's real identity: Google's `sub`
claim is a stable, permanent, Google-assigned id for one Google account -
literally what it exists for. Email addresses can be changed by the
user or reused after an old account is deleted; `sub` never changes and
is never reissued. db.get_or_create_email_account keys the account on
`sub`; email is stored alongside for display/receipts only, never as
the lookup key.
"""

from __future__ import annotations

from fastapi import HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from config import settings
from logging_config import get_logger

logger = get_logger(__name__)

_google_request = google_requests.Request()


def verify_google_id_token(token: str) -> dict:
    """Returns {"sub": ..., "email": ...} for a valid token. Raises
    HTTPException (same pattern as wallet_auth.py's signature-
    verification failures, which also raise HTTPException directly
    rather than a custom exception type) if the token is expired,
    malformed, or wasn't issued for GOOGLE_OAUTH_CLIENT_ID - that last
    check is what stops a token minted for a DIFFERENT app from being
    replayed against this one; verify_oauth2_token performs it
    internally when given audience=..., not something this function
    checks after the fact."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google sign-in isn't configured yet.")

    try:
        claims = google_id_token.verify_oauth2_token(
            token, _google_request, audience=settings.GOOGLE_OAUTH_CLIENT_ID
        )
    except ValueError as exc:
        logger.warning("Google ID token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired Google sign-in token.") from exc

    if not claims.get("email_verified", False):
        # Google lets an account exist with an unverified email in some
        # flows - refusing this here means "signed in with Google" also
        # means "we know this email is real," which the 1-credit bonus
        # and any future email-based communication both rely on.
        raise HTTPException(status_code=401, detail="Google account's email is not verified.")

    return {"sub": claims["sub"], "email": claims["email"]}
