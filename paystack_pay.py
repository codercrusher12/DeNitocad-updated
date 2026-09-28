"""
Paystack subscription integration - replaces stripe_pay.py as the active
payment rail for subscriptions (see config.py's Paystack comment block for
why, and what's kept dormant rather than deleted).

Confirmed against Paystack's current docs before writing this, not
assumed from memory or from Stripe's shape - the two systems differ in
ways that matter:

  - Signature: HMAC-SHA512 of the RAW request body, hex digest, in the
    x-paystack-signature header, signed with the SAME PAYSTACK_SECRET_KEY
    used for API calls. No separate webhook-signing secret exists at all
    (unlike Stripe's STRIPE_WEBHOOK_SECRET) - if this file used a second
    secret, it would be silently checking against nothing.

  - No Checkout Session object. POST /transaction/initialize, given an
    email, amount, and a plan code, returns an authorization_url to
    redirect to - that's the whole "start a subscription" step. Paystack
    auto-creates the subscription once that first charge succeeds.

  - Requires a real customer EMAIL - Paystack's entire Customer/
    Transaction model is email-first, it has no concept of a wallet.
    Per the explicit architecture decision this was built against:

        wallet_address -> NitoCAD subscription -> Paystack customer/email
            -> Paystack subscription

    The wallet stays the identity throughout. Email exists ONLY because
    Paystack requires one to run a charge - a webhook is resolved back
    to a wallet via db.get_subscription_by_paystack_customer_code
    (populated the moment we ourselves initiate the first charge, see
    _on_charge_success below), never by re-deriving a wallet from an
    email address. See db.upsert_paystack_subscription's docstring.

  - Idempotency: charge.success can be, and eventually will be,
    redelivered (a slow response, a retry after any non-2xx, Paystack's
    own at-least-once delivery guarantee). db.mark_paystack_reference_
    processed makes reprocessing the same transaction reference a no-op
    rather than a double-grant - required, not optional, per the
    explicit instruction this was built against.

  - current_period_end: Paystack's webhook payload shapes for a RENEWAL
    charge (as opposed to the first one) are not consistently documented
    across their own docs and third-party integration guides - genuinely
    unclear whether a reliable "next payment date" field is present on
    every renewal's charge.success. Rather than trust an unconfirmed
    field, this computes current_period_end itself as grant-time + 30
    days: every tier here bills monthly, so this is a deliberate,
    uncertainty-avoiding approximation, not a guess dressed up as a
    fact - flagged explicitly rather than silently relying on a payload
    shape that couldn't be confirmed against live docs.

  - Server-side verification: on every charge.success, this calls
    GET /transaction/verify/{reference} before granting anything, rather
    than trusting the webhook body's fields directly - this is Paystack's
    OWN documented recommendation ("please make a server-side call to
    our verification endpoint to confirm the status... before you give
    value"), not an extra precaution invented here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx

import db
from config import settings
from exceptions import PaymentError
from logging_config import get_logger

logger = get_logger(__name__)

_PAYSTACK_BASE_URL = "https://api.paystack.co"


def _tier_by_plan_code() -> dict[str, str]:
    return {
        cfg["paystack_plan_code"]: name
        for name, cfg in settings.subscription_tiers.items()
        if cfg["paystack_plan_code"]
    }


def _require_configured() -> None:
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaymentError("Paystack isn't configured yet (PAYSTACK_SECRET_KEY missing).")
    if settings.PAYSTACK_CURRENCY != "USD":
        # Deliberately strict, not a default-to-NGN fallback - see this
        # module's and config.py's docstrings: USD is NOT assumed
        # enabled on the Paystack account, and silently charging NGN
        # amounts equal to the USD figures would misprice every tier by
        # roughly 1500x. This blocks ALL subscription checkout, not just
        # a currency mismatch warning, until someone who has actually
        # confirmed USD settlement in the Paystack dashboard sets
        # PAYSTACK_CURRENCY=USD explicitly.
        raise PaymentError(
            "Paystack subscriptions are blocked: PAYSTACK_CURRENCY is not set to 'USD'. "
            "Confirm USD is enabled for this Paystack business account before setting it."
        )


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    expected = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


def initialize_subscription(wallet_address: str, tier: str, email: str, callback_url: str) -> str:
    """Returns a Paystack-hosted authorization_url for a NEW subscription
    at the given tier. email is the user-SUPPLIED real email (per the
    explicit decision: never a synthesized .invalid address - Paystack
    sends real receipts/payment notifications to it, and it's how a
    human at Paystack support or in your own dashboard can find this
    customer). metadata.wallet_address is what lets _on_charge_success
    below link the resulting Paystack customer back to this wallet the
    moment the first charge succeeds - see this module's docstring."""
    _require_configured()
    tiers = settings.subscription_tiers
    if tier not in tiers:
        raise PaymentError(f"Unknown subscription tier {tier!r} - must be one of {sorted(tiers)}.")
    plan_code = tiers[tier]["paystack_plan_code"]
    if not plan_code:
        raise PaymentError(f"Tier {tier!r} has no Paystack plan configured (PAYSTACK_PLAN_CODE_{tier.upper()}).")

    payload = {
        "email": email,
        # Paystack's own docs note the plan's configured amount takes
        # precedence over this once a plan code is attached - sent
        # anyway because the API requires the field, and as a sanity
        # value if that ever changes.
        "amount": tiers[tier]["usd_cents"],
        "currency": settings.PAYSTACK_CURRENCY,
        "plan": plan_code,
        "callback_url": callback_url,
        "metadata": {"wallet_address": wallet_address.lower(), "tier": tier},
    }
    resp = httpx.post(f"{_PAYSTACK_BASE_URL}/transaction/initialize", headers=_headers(), json=payload, timeout=15)
    body = resp.json()
    if not resp.is_success or not body.get("status"):
        raise PaymentError(f"Paystack transaction initialize failed: {body.get('message', resp.text)}")
    return body["data"]["authorization_url"]


def handle_webhook_event(raw_body: bytes, signature_header: str) -> str:
    """Verifies the signature FIRST (see this module's docstring on the
    HMAC-SHA512 scheme) then dispatches by event type. Raises
    PaymentError on anything that should surface as a non-2xx to
    Paystack, which retries on non-2xx - the correct behavior for a
    transient failure (see _on_charge_success's unlinked-customer case
    for exactly this)."""
    if not _verify_signature(raw_body, signature_header):
        raise PaymentError("Invalid Paystack webhook signature.")

    body = json.loads(raw_body)
    event = body.get("event")
    data = body.get("data") or {}

    if event == "charge.success":
        return _on_charge_success(data)
    if event == "subscription.create":
        return _on_subscription_create(data)
    if event in ("subscription.disable", "subscription.not_renew"):
        return _on_subscription_status_changed(data, event)

    logger.info("paystack webhook: unhandled event type %s", event)
    return f"ignored:{event}"


def _on_charge_success(data: dict) -> str:
    reference = data.get("reference")
    if not reference:
        raise PaymentError("charge.success payload has no reference.")
    if not db.mark_paystack_reference_processed(reference):
        logger.info("paystack charge.success for already-processed reference %s - skipped", reference)
        return f"duplicate:{reference}"

    # Server-side verification, per Paystack's own recommendation - see
    # this module's docstring. Also the authoritative source for
    # customer/plan/metadata, rather than trusting the webhook body's
    # own copies of those fields.
    resp = httpx.get(f"{_PAYSTACK_BASE_URL}/transaction/verify/{reference}", headers=_headers(), timeout=15)
    verified = resp.json()
    if not resp.is_success or not verified.get("status") or verified["data"]["status"] != "success":
        raise PaymentError(f"Paystack transaction verify failed for reference {reference}: {verified}")
    tx = verified["data"]

    customer_code = tx["customer"]["customer_code"]
    email = tx["customer"]["email"]
    metadata = tx.get("metadata") or {}

    existing = None
    wallet_address = metadata.get("wallet_address")
    if wallet_address:
        existing = db.get_subscription(wallet_address)
    else:
        # No metadata means this charge wasn't initiated by our own
        # initialize_subscription call - the normal shape for a
        # Paystack-initiated recurring renewal charge, which carries no
        # metadata of ours. Resolve the wallet via the customer_code
        # link established on the FIRST charge instead.
        existing = db.get_subscription_by_paystack_customer_code(customer_code)
        if existing is None:
            # An unlinked customer_code with no metadata to fall back on -
            # log loudly and raise so Paystack retries; a delayed or
            # out-of-order initial charge is the most likely explanation,
            # and a retry gives it a chance to have landed by then.
            logger.error("paystack charge.success for unlinked customer %s, no metadata", customer_code)
            raise PaymentError(f"No wallet linked to Paystack customer {customer_code} and no metadata to link one.")
        wallet_address = existing["wallet_address"]

    plan_code = (tx.get("plan") or {}).get("plan_code")
    tier = _tier_by_plan_code().get(plan_code) if plan_code else None
    if tier is None:
        tier = existing["tier"] if existing else None
    if tier is None:
        logger.error("paystack charge.success: can't determine tier for wallet %s (plan_code=%s)", wallet_address, plan_code)
        raise PaymentError(f"Could not determine subscription tier for reference {reference}.")

    monthly_credits = settings.subscription_tiers[tier]["monthly_credits"]
    current_period_end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    db.upsert_paystack_subscription(
        wallet_address,
        email=email,
        paystack_customer_code=customer_code,
        # Preserve whatever subscription_code/plan_code a subscription.create
        # webhook may have already recorded - this event doesn't reliably
        # carry subscription_code itself, and upsert_paystack_subscription
        # overwrites the whole row, so an empty string here would erase it
        # if subscription.create happened to arrive first. See
        # db.set_paystack_subscription_code's docstring.
        paystack_subscription_code=(existing or {}).get("paystack_subscription_code") or "",
        paystack_plan_code=plan_code or (existing or {}).get("paystack_plan_code") or "",
        status="active",
        tier=tier,
        subscription_credits=monthly_credits,
        current_period_end=current_period_end,
    )

    if existing is None:
        bonus_granted = db.claim_signup_bonus(wallet_address, qualifying_payment_id=reference)
        if bonus_granted:
            logger.info("signup bonus granted via paystack subscription", extra={"wallet_address": wallet_address})

    logger.info(
        "paystack subscription period granted",
        extra={"wallet_address": wallet_address, "tier": tier, "current_period_end": current_period_end},
    )
    return f"charged:{wallet_address}:{tier}"


def _on_subscription_create(data: dict) -> str:
    """Purely a linking step - see db.set_paystack_subscription_code's
    docstring for why this deliberately does NOT touch tier/credits/
    current_period_end, which _on_charge_success above already owns."""
    customer_code = (data.get("customer") or {}).get("customer_code")
    subscription_code = data.get("subscription_code")
    plan_code = (data.get("plan") or {}).get("plan_code", "")
    if not (customer_code and subscription_code):
        logger.warning("paystack subscription.create missing customer_code or subscription_code: %s", data)
        return "ignored:incomplete"
    db.set_paystack_subscription_code(customer_code, subscription_code, plan_code)
    return f"linked_subscription:{subscription_code}"


def _on_subscription_status_changed(data: dict, event: str) -> str:
    subscription_code = data.get("subscription_code")
    if not subscription_code:
        logger.warning("paystack %s missing subscription_code: %s", event, data)
        return "ignored:incomplete"
    status = "not_renewing" if event == "subscription.not_renew" else "disabled"
    db.set_paystack_subscription_status(subscription_code, status)
    return f"status:{subscription_code}:{status}"
