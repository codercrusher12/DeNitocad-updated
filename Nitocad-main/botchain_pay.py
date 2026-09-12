"""
BOT Chain payment verification for the agent-to-agent interaction point.

BOT Chain has no live native payment protocol yet (AgentPay is still on
BOT Chain's own roadmap, not shipped - see easycad's own decision log).
This module is a manual substitute: a caller pays by sending native BOT
directly to settings.TREASURY_ADDRESS, then passes the resulting tx hash
to this backend, which verifies the payment on-chain before serving the
gated action.

Two payment points (see web_app.py / a2mcp_botchain/server.py):
  - one-time settings.BOTCHAIN_KEY_ISSUE_PRICE_BOT to mint an API key
  - settings.BOTCHAIN_PER_CALL_PRICE_BOT on every /generate,
    /export/{fmt}/{job_id}, or MCP generate_cad_part/export_format call

Confirmation threshold is settings.BOTCHAIN_CONFIRMATION_BLOCKS (1 by
default) - accepted as soon as the tx is mined, not re-checked after
more blocks land. Worth noting: ShieldGuard's own on-chain payment
checks (connections.js, webhook.js) don't wait for extra confirmations
either, they accept on receipt.status == 1 alone, so 1-block here is if
anything more conservative than the existing working pattern on this
chain, not a shortcut invented for this project.

Double-spend/replay protection is done by db.py's `payments` table,
which has tx_hash as a PRIMARY KEY. reserve_payment() runs BEFORE any
RPC work, so two concurrent requests carrying the same tx_hash can
never both pass verification - the loser fails on the INSERT itself,
atomically, via sqlite's own uniqueness constraint.

expected_sender binding: reserving a tx_hash only proves it hasn't been
spent *twice* - it says nothing about who the tx_hash belongs to. Chain
data is public, so without a sender check, anyone watching for a
qualifying payment to the treasury could grab someone else's tx_hash
and spend it against their own call first. ShieldGuard's connections.js
and webhook.js both check the payment's sender against an expected
wallet for exactly this reason (see their own comments on the bug this
fixed). Callers here that already know which wallet should be paying
(an existing API key's owner, or a job's owner) must pass
expected_sender; key issuance and MCP's generate_cad_part are the two
exceptions, since there's no prior identity to check against - the
paying wallet becomes the identity, not the other way around.
"""

from __future__ import annotations

from decimal import Decimal

from web3 import Web3
from web3.exceptions import TransactionNotFound

import db
from config import settings
from exceptions import PaymentError
from logging_config import get_logger

logger = get_logger(__name__)

KEY_ISSUE_PRICE_BOT = Decimal(str(settings.BOTCHAIN_KEY_ISSUE_PRICE_BOT))
PER_CALL_PRICE_BOT = Decimal(str(settings.BOTCHAIN_PER_CALL_PRICE_BOT))


def _w3() -> Web3:
    return Web3(Web3.HTTPProvider(settings.botchain_rpc_url))


def _bot_to_wei(amount_bot: Decimal) -> int:
    return int(amount_bot * Decimal(10) ** 18)


def verify_and_record_payment(
    tx_hash: str,
    *,
    purpose: str,
    min_amount_bot: Decimal,
    user_id: str,
    expected_sender: str | None = None,
) -> str:
    """Verify a native-BOT payment and record it as spent. Raises
    PaymentError on any failure. Returns the sender address on success,
    so callers that don't pass expected_sender (key issuance, MCP's
    generate_cad_part) can bind identity to whichever wallet actually
    paid.

    expected_sender: if given, the tx's sender must match (case-
    insensitive) or verification fails - see module docstring. Pass
    this whenever the caller already has an established identity (an
    API key's owner, a job's owner) that the payment must belong to.
    """
    tx_hash = tx_hash.lower()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise PaymentError(f"Malformed transaction hash: {tx_hash!r}")

    if not settings.TREASURY_ADDRESS:
        raise PaymentError("TREASURY_ADDRESS is not configured on this server.")

    # Reserve first, verify second - see module docstring.
    if not db.reserve_payment(tx_hash, purpose=purpose, user_id=user_id):
        raise PaymentError(f"Transaction {tx_hash} has already been used for a payment.")

    try:
        w3 = _w3()
        try:
            tx = w3.eth.get_transaction(tx_hash)
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            db.release_payment(tx_hash)
            raise PaymentError(
                f"Transaction {tx_hash} not found yet - wait for it to be mined and retry."
            )

        if receipt.status != 1:
            db.invalidate_payment(tx_hash)
            raise PaymentError(f"Transaction {tx_hash} reverted on-chain.")

        confirmations = w3.eth.block_number - receipt.blockNumber + 1
        if confirmations < settings.BOTCHAIN_CONFIRMATION_BLOCKS:
            db.release_payment(tx_hash)
            raise PaymentError(
                f"Transaction {tx_hash} has {confirmations} confirmation(s), "
                f"needs {settings.BOTCHAIN_CONFIRMATION_BLOCKS}. Retry shortly."
            )

        treasury = Web3.to_checksum_address(settings.TREASURY_ADDRESS)
        if Web3.to_checksum_address(tx["to"]) != treasury:
            db.invalidate_payment(tx_hash)
            raise PaymentError(f"Transaction {tx_hash} was not sent to the treasury address.")

        if tx["value"] < _bot_to_wei(min_amount_bot):
            db.invalidate_payment(tx_hash)
            raise PaymentError(
                f"Transaction {tx_hash} sent less than the required {min_amount_bot} BOT."
            )

        sender = tx["from"]
        if expected_sender is not None and sender.lower() != expected_sender.lower():
            db.invalidate_payment(tx_hash)
            raise PaymentError(
                f"Transaction {tx_hash} was not sent from the expected wallet - "
                "this payment belongs to a different account."
            )

    except PaymentError:
        raise
    except Exception as exc:  # noqa: BLE001 - RPC/web3 raises many exception types
        db.release_payment(tx_hash)
        logger.exception("payment verification failed unexpectedly")
        raise PaymentError(f"Payment verification failed: {exc}") from exc

    db.mark_payment_verified(tx_hash)
    logger.info("payment verified", extra={"purpose": purpose})
    return sender
