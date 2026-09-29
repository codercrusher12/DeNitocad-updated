"""
Calls DesignRegistry.sol's anchorDesign() on BOT Chain whenever a STEP
file is actually built (see cad_generator.py's export_format_for_job,
the only caller). Not wired into every export format - only STEP,
since it's the canonical solid deliverable; anchoring on every
IGES/DXF/PDF export of the same job would just re-describe the same
part_type+parameters+templateVersion again, and anchorDesign() reverts
on a duplicate jobId anyway (one anchor per job, not per export click).

Anchoring failure is NEVER allowed to break a paid export - the user
already spent a credit generating this job and is owed their file
regardless of whether the chain call succeeds. Every failure path here
is caught and logged, not raised.

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

import db
from config import settings
from logging_config import get_logger

logger = get_logger(__name__)

# BOT Chain is a Proof-of-Authority chain: its blocks carry an extraData
# field longer than the 32 bytes stock web3.py expects, which raises
# ExtraDataLengthError on any call that touches block-header data -
# including build_transaction()'s own fee estimation, below. See
# scripts/deploy_design_registry.py's identical note; that script
# injects this same middleware for the exact same build_transaction()
# call pattern on the exact same chain, but this module - the one that
# actually runs on every STEP export, not just once at deploy time -
# never did. Without it, anchor_design() fails on every call, gets
# swallowed by the bare except below (by design, so anchoring failures
# never break a paid export), and shows up nowhere but the logs.
try:
    from web3.middleware import ExtraDataToPOAMiddleware as _poa_middleware
except ImportError:
    from web3.middleware import geth_poa_middleware as _poa_middleware

REPO_ROOT = Path(__file__).resolve().parent
ABI_PATH = REPO_ROOT / "contracts" / "DesignRegistry.abi.json"

_EMBEDDED_ABI = [
    {
        "type": "function", "name": "anchorDesign", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "jobId", "type": "bytes32"},
            {"name": "partType", "type": "string"},
            {"name": "parametersHash", "type": "bytes32"},
            {"name": "parameters", "type": "string"},
            {"name": "templateVersion", "type": "string"},
            {"name": "outputHash", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "type": "function", "name": "getDesign", "stateMutability": "view",
        "inputs": [{"name": "jobId", "type": "bytes32"}],
        "outputs": [{
            "name": "", "type": "tuple",
            "components": [
                {"name": "partType", "type": "string"},
                {"name": "parametersHash", "type": "bytes32"},
                {"name": "templateVersion", "type": "string"},
                {"name": "outputHash", "type": "bytes32"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "submitter", "type": "address"},
            ],
        }],
    },
    {
        "type": "function", "name": "isAnchored", "stateMutability": "view",
        "inputs": [{"name": "jobId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function", "name": "designs", "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [
            {"name": "partType", "type": "string"},
            {"name": "parametersHash", "type": "bytes32"},
            {"name": "templateVersion", "type": "string"},
            {"name": "outputHash", "type": "bytes32"},
            {"name": "timestamp", "type": "uint256"},
            {"name": "submitter", "type": "address"},
        ],
    },
    {
        "type": "event", "name": "DesignAnchored", "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "jobId", "type": "bytes32"},
            {"indexed": False, "name": "partType", "type": "string"},
            {"indexed": False, "name": "parametersHash", "type": "bytes32"},
            {"indexed": False, "name": "parameters", "type": "string"},
            {"indexed": False, "name": "templateVersion", "type": "string"},
            {"indexed": False, "name": "outputHash", "type": "bytes32"},
            {"indexed": False, "name": "timestamp", "type": "uint256"},
            {"indexed": False, "name": "submitter", "type": "address"},
        ],
    },
]

_abi_cache = None


def _load_abi():
    """Embedded ABI is the default so a live contract works without any
    committed file. contracts/DesignRegistry.abi.json, if present, is an
    optional override."""
    global _abi_cache
    if _abi_cache is None:
        _abi_cache = _EMBEDDED_ABI
        if ABI_PATH.exists():
            try:
                _abi_cache = json.loads(ABI_PATH.read_text())
            except Exception:  # noqa: BLE001
                logger.exception("bad ABI override file, using embedded ABI")
    return _abi_cache


def anchoring_status() -> dict:
    """Which pieces are configured. Never exposes secret values."""
    addr = settings.DESIGN_REGISTRY_ADDRESS
    return {
        "registry_address": bool(addr),
        "registry_address_valid": bool(addr) and Web3.is_address(addr),
        "anchor_wallet_key": bool(settings.ANCHOR_WALLET_PRIVATE_KEY),
        "rpc_url": bool(settings.botchain_rpc_url),
        "environment": settings.BOTCHAIN_ENVIRONMENT,
    }


def log_anchoring_status() -> None:
    st = anchoring_status()
    missing = [k for k in ("registry_address", "anchor_wallet_key", "rpc_url") if not st[k]]
    if st["registry_address"] and not st["registry_address_valid"]:
        missing.append("registry_address_valid")
    if missing:
        logger.warning("anchoring DISABLED, missing/invalid: %s", ", ".join(missing), extra=st)
    else:
        logger.info("anchoring ENABLED on %s", st["environment"], extra=st)


def anchoring_available() -> bool:
    """True when registry address, anchor wallet key and RPC URL are all
    set. The ABI is embedded, so no file is required."""
    st = anchoring_status()
    return st["registry_address_valid"] and st["anchor_wallet_key"] and st["rpc_url"]


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
    if not anchoring_available():
        return None

    abi = _load_abi()

    try:
        w3 = Web3(Web3.HTTPProvider(settings.botchain_rpc_url))
        w3.middleware_onion.inject(_poa_middleware, layer=0)
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
        tx_hex = tx_hash.hex()
        if not tx_hex.startswith("0x"):
            tx_hex = "0x" + tx_hex
        try:
            db.set_job_anchor_tx(job_id, tx_hex)
        except Exception:  # noqa: BLE001 - never fail the export on a DB write
            logger.exception(
                "failed to persist anchor_tx on job", extra={"job_id": job_id}
            )
        logger.info(
            "design anchor submitted",
            extra={"job_id": job_id, "anchor_tx": tx_hex},
        )
        return tx_hex
    except Exception:  # noqa: BLE001 - anchoring must never break a paid export
        logger.exception(
            "design anchor failed, export still proceeds", extra={"job_id": job_id}
        )
        return None
