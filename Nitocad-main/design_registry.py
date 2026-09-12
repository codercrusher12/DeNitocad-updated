"""
Calls DesignRegistry.sol's anchorDesign() on BOT Chain whenever a STEP
file is actually built (see cad_generator.py's export_format_for_job,
the only caller). Not wired into every export format - only STEP,
since it's the canonical solid deliverable; anchoring on every
IGES/DXF/PDF export of the same job would just re-describe the same
part_type+parameters+templateVersion again, and anchorDesign() reverts
on a duplicate jobId anyway (one anchor per job, not per export click).

Anchoring failure is NEVER allowed to break a paid export - the user
already paid PER_CALL_PRICE_BOT and is owed their file regardless of
whether the chain call succeeds. Every failure path here is caught and
logged, not raised.

Uses settings.ANCHOR_WALLET_PRIVATE_KEY - a wallet the SERVER controls
and signs with, separate from any user's wallet and separate from
TREASURY_ADDRESS (which only ever receives, never signs). This wallet
needs its own small BOT balance to pay gas - see
scripts/deploy_design_registry.py's docstring for funding it.

Nonce handling: uses the 'pending' block for get_transaction_count,
which accounts for this wallet's own already-submitted-but-unconfirmed
transactions. This reduces, but does not fully eliminate, a race if
two exports anchor concurrently from two different request-handling
threads/processes at nearly the same instant - both could read the
same "next" nonce before either lands. Low risk at current call volume
(each anchor sits behind a real BOT payment, so this isn't a
high-frequency path), but if anchor volume grows, this needs a real
queue/lock around nonce assignment, not just 'pending'. Flagging this
now rather than presenting 'pending' as a complete fix.
"""

from __future__ import annotations

import json
from pathlib import Path

from web3 import Web3

from config import settings
from logging_config import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
ABI_PATH = REPO_ROOT / "contracts" / "DesignRegistry.abi.json"

_abi_cache = None


def _load_abi():
    global _abi_cache
    if _abi_cache is None:
        if not ABI_PATH.exists():
            return None
        _abi_cache = json.loads(ABI_PATH.read_text())
    return _abi_cache


def _raw_tx_bytes(signed_tx) -> bytes:
    """See scripts/deploy_design_registry.py's identical shim - web3.py
    renamed this attribute between major versions and requirements.txt
    pins "web3" with no version."""
    if hasattr(signed_tx, "raw_transaction"):
        return signed_tx.raw_transaction
    return signed_tx.rawTransaction


def anchor_design(
    job_id: str, part_type: str, parameters: dict, output_path: Path
) -> str | None:
    """Best-effort: anchors provenance for one job's STEP export.
    Returns the anchor tx hash on success, None on any failure or if
    anchoring isn't configured (DESIGN_REGISTRY_ADDRESS /
    ANCHOR_WALLET_PRIVATE_KEY unset, or the ABI hasn't been generated
    yet by scripts/deploy_design_registry.py). Never raises - see
    module docstring."""
    if not settings.DESIGN_REGISTRY_ADDRESS or not settings.ANCHOR_WALLET_PRIVATE_KEY:
        return None

    abi = _load_abi()
    if abi is None:
        logger.warning(
            "DESIGN_REGISTRY_ADDRESS is set but contracts/DesignRegistry.abi.json "
            "is missing - run scripts/deploy_design_registry.py and commit its "
            "output first."
        )
        return None

    try:
        w3 = Web3(Web3.HTTPProvider(settings.botchain_rpc_url))
        account = w3.eth.account.from_key(settings.ANCHOR_WALLET_PRIVATE_KEY)
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.DESIGN_REGISTRY_ADDRESS), abi=abi
        )

        canonical_params = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        parameters_hash = Web3.keccak(text=canonical_params)
        output_hash = Web3.keccak(output_path.read_bytes())
        job_id_bytes32 = Web3.keccak(text=job_id)

        tx = contract.functions.anchorDesign(
            job_id_bytes32,
            part_type,
            parameters_hash,
            canonical_params,
            settings.template_version,
            output_hash,
        ).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": w3.eth.chain_id,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(_raw_tx_bytes(signed))
        # Fire-and-forget: NOT waiting for a receipt here. This runs
        # inside export_format_for_job, in the same request that's
        # already been paid for and is about to hand back a file -
        # blocking that response on block confirmation would make
        # every STEP download wait on finality for no benefit to the
        # person downloading. The tx hash is logged; confirming it
        # actually landed is a separate, later concern, not this
        # request's problem to solve.
        logger.info(
            "design anchor submitted",
            extra={"job_id": job_id, "anchor_tx": tx_hash.hex()},
        )
        return tx_hash.hex()
    except Exception:  # noqa: BLE001 - anchoring must never break a paid export
        logger.exception(
            "design anchor failed, export still proceeds", extra={"job_id": job_id}
        )
        return None
