"""
Stripe subscription integration - three tiers (Starter/Engineer/
Professional, see config.py's settings.subscription_tiers), each its
own recurring Stripe Price.

This is deliberately a SEPARATE payment rail from botchain_pay.py, not a
unified "payment" abstraction over both - the two have almost nothing in
common mechanically. A BOT payment is verified by us, after the fact, by
looking up a tx_hash the user already sent (see botchain_pay.py's module
docstring). A Stripe subscription is the opposite shape: Stripe holds the
card, charges it on its own schedule, and tells US when something
happened via webhook - there's no tx_hash to verify, no amount to check
against a min_amount, because Stripe already enforced the price at
Checkout. Trying to share code between these two would mean forcing one
of them through the other's assumptions for no real benefit.

Wallet linkage: Stripe's own objects (Customer, Subscription, Invoice)
have no concept of "wallet address." The link is established once, at
POST /subscribe/checkout (this app sets client_reference_id to the
signed-in wallet), and captured into db.subscriptions the moment
checkout.session.completed arrives. Every later webhook (invoice.paid,
customer.subscription.updated/deleted) only carries stripe_customer_id
or stripe_subscription_id, so those look the wallet back up via
db.get_subscription_by_stripe_customer_id - see that function's
docstring for what happens if the link was never established.

Tier resolution: a webhook never carries our own tier name ("starter"
etc), only Stripe's price id. _TIER_BY_PRICE_ID inverts
settings.subscription_tiers once at import time to map back. If two
tiers are accidentally configured with the same STRIPE_PRICE_ID_* env
var (a copy-paste mistake), whichever tier settings.subscription_tiers
iterates last silently wins the mapping - there's no validation against
that here, since Settings has no way to know these are meant to be
distinct at the pydantic level.

Credit-granting event: invoice.paid, not checkout.session.completed.
Both fire for a brand-new subscription (Stripe's normal flow is
checkout.session.completed followed almost immediately by invoice.paid
for that same first period), and using checkout.session.completed to
grant credits/allowance AND invoice.paid to grant renewal credits would
double-grant the first period. invoice.paid is the one event that fires
uniformly for "first payment" and "every renewal" alike, so it is the
ONLY place a tier's monthly_credits is ever granted.
checkout.session.completed's only job is recording the wallet link
(and which tier was purchased) before any invoice event needs it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import stripe

import db
from config import settings
from exceptions import PaymentError
from logging_config import get_logger

logger = get_logger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def _tier_by_price_id() -> dict[str, str]:
    return {
        cfg["stripe_price_id"]: name
        for name, cfg in settings.subscription_tiers.items()
        if cfg["stripe_price_id"]
    }


def _require_configured() -> None:
    if not (settings.STRIPE_SECRET_KEY and settings.STRIPE_WEBHOOK_SECRET):
        raise PaymentError("Subscriptions aren't configured yet (STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET missing).")


def create_checkout_session(wallet_address: str, tier: str, success_url: str, cancel_url: str) -> str:
    """Returns a Stripe-hosted Checkout URL for a NEW subscription at the
    given tier ("starter" | "engineer" | "professional" - see
    config.py's settings.subscription_tiers). client_reference_id
    carries the wallet address through to checkout.session.completed
    below - this is the one and only place that link gets set. If this
    wallet already has an active subscription, the caller (web_app.py)
    should check db.is_subscription_entitled first and skip calling
    this at all; Stripe will happily create a second subscription for
    the same customer, it has no idea "one per wallet" is even a rule
    here."""
    _require_configured()
    tiers = settings.subscription_tiers
    if tier not in tiers:
        raise PaymentError(f"Unknown subscription tier {tier!r} - must be one of {sorted(tiers)}.")
    price_id = tiers[tier]["stripe_price_id"]
    if not price_id:
        raise PaymentError(f"Tier {tier!r} has no Stripe price configured (STRIPE_PRICE_ID_{tier.upper()}).")

    existing = db.get_subscription(wallet_address)
    session_kwargs = dict(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=wallet_address.lower(),
        success_url=success_url,
        cancel_url=cancel_url,
    )
    # Reuse the existing Stripe Customer if this wallet has one from a
    # prior (now-lapsed or different-tier) subscription, so Stripe
    # doesn't fragment one wallet's billing history across multiple
    # Customer objects.
    if existing and existing["stripe_customer_id"]:
        session_kwargs["customer"] = existing["stripe_customer_id"]

    session = stripe.checkout.Session.create(**session_kwargs)
    return session.url


def handle_webhook_event(payload: bytes, sig_header: str) -> str:
    """Verifies the signature (constructs the event via Stripe's own SDK
    helper, which raises on a bad signature - see the SignatureVerificationError
    catch below) then dispatches by event type. Returns a short string
    for the caller to log/respond with; raises PaymentError on anything
    that should surface as a 400 to Stripe (Stripe retries on non-2xx,
    which is the correct behavior for a transient failure here)."""
    _require_configured()
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise PaymentError(f"Invalid Stripe webhook signature/payload: {exc}") from exc

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        return _on_checkout_completed(data)
    if event_type == "invoice.paid":
        return _on_invoice_paid(data)
    if event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        return _on_subscription_status_changed(data)

    logger.info("stripe webhook: unhandled event type %s", event_type)
    return f"ignored:{event_type}"


def _on_checkout_completed(session: dict) -> str:
    wallet_address = session.get("client_reference_id")
    if not wallet_address:
        # Shouldn't happen - we always set client_reference_id when
        # creating the session - but a malformed/manual test event
        # could hit this, and silently doing nothing is safer than
        # guessing a wallet.
        logger.error("stripe checkout.session.completed with no client_reference_id: %s", session.get("id"))
        raise PaymentError("Checkout session has no wallet reference.")

    # Checkout Session webhook payloads do NOT include line_items by
    # default (only available via dashboard-level `expand` config on
    # the webhook endpoint itself, easy to forget/misconfigure) - fetch
    # it explicitly so tier resolution doesn't silently depend on that.
    line_items = stripe.checkout.Session.list_line_items(session["id"], limit=1)
    if not line_items["data"]:
        logger.error("stripe checkout.session.completed with no line items: %s", session.get("id"))
        raise PaymentError("Checkout session has no line items - can't determine tier.")
    price_id = line_items["data"][0]["price"]["id"]
    tier = _tier_by_price_id().get(price_id)
    if tier is None:
        logger.error("stripe checkout.session.completed with unrecognized price %s", price_id)
        raise PaymentError(f"Price {price_id} doesn't match any configured subscription tier.")

    existing = db.get_subscription(wallet_address)
    db.upsert_subscription(
        wallet_address,
        stripe_customer_id=session["customer"],
        stripe_subscription_id=session["subscription"],
        status="incomplete",  # invoice.paid (below) is what actually activates it
        tier=tier,
        subscription_credits=0,  # granted by invoice.paid, not here - see module docstring
        current_period_end=existing["current_period_end"] if existing else None,
    )
    logger.info("stripe checkout completed", extra={"wallet_address": wallet_address, "tier": tier})
    return f"linked:{wallet_address}:{tier}"


def _on_invoice_paid(invoice: dict) -> str:
    customer_id = invoice["customer"]
    sub = db.get_subscription_by_stripe_customer_id(customer_id)
    if sub is None:
        # An invoice for a customer we never linked to a wallet - the
        # checkout.session.completed webhook either hasn't arrived yet
        # (Stripe doesn't guarantee ordering) or never will. Log loudly;
        # raising PaymentError makes Stripe retry this webhook, which
        # gives the missing checkout.session.completed event a chance
        # to arrive and create the link before the retry.
        logger.error("stripe invoice.paid for unlinked customer %s", customer_id)
        raise PaymentError(f"No wallet linked to Stripe customer {customer_id} yet.")

    price_id = invoice["lines"]["data"][0]["price"]["id"]
    tier = _tier_by_price_id().get(price_id, sub["tier"])
    if tier not in settings.subscription_tiers:
        logger.error("stripe invoice.paid with unrecognized price %s for wallet %s", price_id, sub["wallet_address"])
        raise PaymentError(f"Price {price_id} doesn't match any configured subscription tier.")

    monthly_credits = settings.subscription_tiers[tier]["monthly_credits"]
    period_end_ts = invoice["lines"]["data"][0]["period"]["end"]
    current_period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc).isoformat()

    db.upsert_subscription(
        sub["wallet_address"],
        stripe_customer_id=customer_id,
        stripe_subscription_id=invoice["subscription"],
        status="active",
        tier=tier,
        subscription_credits=monthly_credits,  # 0 for Engineer/Professional - see config.py
        current_period_end=current_period_end,
    )

    if invoice.get("billing_reason") == "subscription_create":
        bonus_granted = db.claim_signup_bonus(sub["wallet_address"], qualifying_payment_id=invoice["id"])
        if bonus_granted:
            logger.info("signup bonus granted via subscription", extra={"wallet_address": sub["wallet_address"]})

    logger.info(
        "subscription period granted",
        extra={"wallet_address": sub["wallet_address"], "tier": tier, "current_period_end": current_period_end},
    )
    return f"renewed:{sub['wallet_address']}:{tier}"


def _on_subscription_status_changed(subscription: dict) -> str:
    """customer.subscription.updated fires for lots of things (plan
    change, cancel_at_period_end being set, payment retry state) - we
    only care about the status field itself here. customer.subscription.
    deleted fires once, when the subscription actually ends (at period
    end for a cancel_at_period_end cancellation, or immediately for a
    hard cancel) - by the time either arrives, db.charge_generation
    already stops honoring subscription entitlements once
    current_period_end has passed regardless of what status says (see
    db.is_subscription_entitled), so this mostly keeps that status
    field in sync rather than being the only thing enforcing the
    cutoff. A plan change (Starter -> Professional mid-cycle) also
    fires this event but is NOT handled here - it carries a new price
    id with no corresponding invoice.paid until the next renewal, so
    the tier column stays at whatever the last invoice.paid set it to
    until then. Flagged, not fixed: proration/plan-change handling is
    a separate decision, not implicit in what was asked for here."""
    db.set_subscription_status(subscription["id"], subscription["status"])
    return f"status:{subscription['id']}:{subscription['status']}"
