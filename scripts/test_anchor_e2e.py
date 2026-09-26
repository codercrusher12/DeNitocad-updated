"""
End-to-end validation for design_registry.py's PoA middleware fix.

Run this on Railway's shell (or anywhere with real network access to
BOT Chain), NOT in CI - it sends real transactions and costs real
testnet BOT. It exists to prove the specific chain that was broken
before the fix, not just that the module imports cleanly:

    export STEP -> anchor_design() -> PoA middleware -> build_transaction()
    -> sign -> broadcast -> receipt confirmed -> registry contains anchor

Two jobs are generated and anchored, not one - anchorDesign() reverts
on a duplicate jobId (see contracts/DesignRegistry.sol), so exporting
the same job's STEP twice can't exercise the nonce-handling path
design_registry.py's own docstring flags as a known, un-closed race
under concurrent anchoring. Two DIFFERENT jobs, back to back, is what
actually exercises "does get_transaction_count('pending') correctly
account for this wallet's own just-submitted, not-yet-mined tx."

Every check below reads the result back from the chain itself (via
RPC - get_transaction_receipt, then the deployed contract's own
isAnchored/getDesign), not from this script's own success/failure
assumption. Prints explorer links so you can also verify by eyeball,
independent of anything this process claims.

Prerequisites (same env this API already needs for anchoring to be
configured at all - see config.py):
    DESIGN_REGISTRY_ADDRESS   - from scripts/deploy_design_registry.py
    ANCHOR_WALLET_PRIVATE_KEY - funded with testnet BOT for gas
    BOTCHAIN_ENVIRONMENT=testnet (or whatever settings.botchain_rpc_url
                                   should resolve to)

Usage:
    python scripts/test_anchor_e2e.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3

import db
import design_registry
from cad_generator import CADGenerator
from config import settings

try:
    from web3.middleware import ExtraDataToPOAMiddleware as _poa_middleware
except ImportError:
    from web3.middleware import geth_poa_middleware as _poa_middleware


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def _run_one_export(generator: CADGenerator, description: str, tag: str) -> tuple[str, str]:
    """Generates one job through the real /generate code path, then
    exports STEP through the real export_format_for_job path - the
    same call web_app.py's GET /export/step/{job_id} makes, so this
    exercises the exact anchoring call site, not a stand-in."""
    print(f"\n--- job {tag}: generating '{description}' ---")
    result = generator.generate_from_text(description, use_deepseek=False, user_id="e2e-test-wallet", formats=["stl"])
    _check(f"job {tag}: generation succeeded", result.get("success"), result.get("error", ""))
    job_id = result["job_id"]
    print(f"job_id = {job_id}")

    file_path, part_type = generator.export_format_for_job(job_id, "step")
    _check(f"job {tag}: STEP file written to disk", file_path.exists(), str(file_path))
    return job_id, part_type


def main() -> None:
    print(f"BOTCHAIN_ENVIRONMENT = {settings.BOTCHAIN_ENVIRONMENT}")
    print(f"RPC                  = {settings.botchain_rpc_url}")
    print(f"DESIGN_REGISTRY      = {settings.DESIGN_REGISTRY_ADDRESS}")
    _check("DESIGN_REGISTRY_ADDRESS is set", bool(settings.DESIGN_REGISTRY_ADDRESS))
    _check("ANCHOR_WALLET_PRIVATE_KEY is set", bool(settings.ANCHOR_WALLET_PRIVATE_KEY))

    abi = design_registry._load_abi()
    _check("contracts/DesignRegistry.abi.json loads", abi is not None)

    w3 = Web3(Web3.HTTPProvider(settings.botchain_rpc_url))
    w3.middleware_onion.inject(_poa_middleware, layer=0)
    _check("RPC reachable", w3.is_connected())

    account = w3.eth.account.from_key(settings.ANCHOR_WALLET_PRIVATE_KEY)
    balance_bot = w3.eth.get_balance(account.address) / 10**18
    print(f"anchor wallet         = {account.address}")
    print(f"anchor wallet balance = {balance_bot} BOT")
    _check("anchor wallet has gas funds", balance_bot > 0)

    contract = w3.eth.contract(address=Web3.to_checksum_address(settings.DESIGN_REGISTRY_ADDRESS), abi=abi)

    db.init_db()
    generator = CADGenerator(output_dir=Path(settings.OUTPUT_DIR))

    # Two distinct jobs, exported back to back with no delay between
    # them - this is what actually exercises the 'pending' nonce path,
    # not two calls minutes apart that would each just read a fresh
    # confirmed nonce.
    job_a, part_a = _run_one_export(generator, "a 40mm cube bracket with two 5mm mounting holes", "A")
    job_b, part_b = _run_one_export(generator, "an L-bracket 60x40x5mm with a 6mm hole", "B")

    print("\n--- waiting for both anchor transactions to be mined ---")
    results = {}
    for tag, job_id, part_type in (("A", job_a, part_a), ("B", job_b, part_b)):
        job = db.get_job(job_id)
        parameters = __import__("json").loads(job["parameters"]) if job["parameters"] else {}
        out_path = generator.output_dir / f"{job_id}_step.step"
        tx_hash = design_registry.anchor_design(job_id, part_type, parameters, out_path)
        _check(f"job {tag}: anchor_design() returned a tx hash (not None)", tx_hash is not None)
        results[tag] = {"job_id": job_id, "tx_hash": tx_hash}

    for tag, r in results.items():
        receipt = w3.eth.wait_for_transaction_receipt(r["tx_hash"], timeout=120)
        _check(f"job {tag}: transaction mined", receipt is not None)
        _check(f"job {tag}: transaction succeeded on-chain (status == 1)", receipt.status == 1)
        r["nonce"] = w3.eth.get_transaction(r["tx_hash"])["nonce"]
        print(f"job {tag}: nonce = {r['nonce']}, block = {receipt.blockNumber}")
        print(f"job {tag}: explorer = {settings.botchain_explorer_url}/tx/{r['tx_hash']}")

    _check(
        "the two anchors used two different nonces (no collision)",
        results["A"]["nonce"] != results["B"]["nonce"],
        f"A={results['A']['nonce']} B={results['B']['nonce']}",
    )

    print("\n--- reading the registry back from chain state (not app logs) ---")
    for tag, r in results.items():
        job_id_bytes32 = Web3.keccak(text=r["job_id"])
        is_anchored = contract.functions.isAnchored(job_id_bytes32).call()
        _check(f"job {tag}: isAnchored(jobId) == true", is_anchored)
        design = contract.functions.getDesign(job_id_bytes32).call()
        # Design struct: (partType, parametersHash, templateVersion, outputHash, timestamp, submitter)
        _check(f"job {tag}: on-chain submitter == anchor wallet", design[5].lower() == account.address.lower())
        print(f"job {tag}: on-chain partType={design[0]!r} templateVersion={design[2]!r} timestamp={design[4]}")

    print("\nAll checks passed. Both anchors are confirmed on-chain, independent of this script's own claims -")
    print("cross-check the two explorer links above by eye before treating this as done.")


if __name__ == "__main__":
    main()
