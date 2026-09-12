"""
One-off script: compiles and deploys contracts/DesignRegistry.sol to
BOT Chain, then writes the ABI to contracts/DesignRegistry.abi.json for
design_registry.py to load at runtime.

Run this ONCE per chain (once for testnet, again later for mainnet).
It is not part of the running API - it's meant to be run manually from
Railway's shell (or any machine with network access to BOT Chain and
the Solidity compiler), not deployed as a route or triggered
automatically.

Usage (from the repo root, on Railway's shell or similar):
    pip install -r scripts/requirements-deploy.txt --break-system-packages
    export DEPLOYER_PRIVATE_KEY=0x...   # the wallet paying gas for THIS deployment
    export BOTCHAIN_RPC_URL=https://rpc.bohr.life   # or your mainnet RPC when ready
    python scripts/deploy_design_registry.py

DEPLOYER_PRIVATE_KEY here is used once, to deploy. It does NOT need to
be the same wallet as ANCHOR_WALLET_PRIVATE_KEY (the one config.py's
settings.ANCHOR_WALLET_PRIVATE_KEY holds for ongoing anchorDesign()
calls afterward) - though using the same wallet for both is fine and
simpler if you'd rather manage one key instead of two. Either way, this
script only reads DEPLOYER_PRIVATE_KEY from the environment, never
writes it anywhere, and never touches TREASURY_ADDRESS.

After this script prints a contract address, set it as
DESIGN_REGISTRY_ADDRESS in Railway's environment variables for the main
API service - that's a separate manual step, this script does not do
it for you (it doesn't have Railway API access, and shouldn't).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from solcx import compile_source, install_solc, set_solc_version
from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "DesignRegistry.sol"
ABI_OUTPUT_PATH = REPO_ROOT / "contracts" / "DesignRegistry.abi.json"

SOLC_VERSION = "0.8.19"  # matches the pragma in DesignRegistry.sol


def _raw_tx_bytes(signed_tx) -> bytes:
    """web3.py renamed SignedTransaction.rawTransaction to
    raw_transaction between major versions. requirements.txt pins
    "web3" with no version, so which one lands here isn't guaranteed -
    check both rather than assume and fail confusingly on the wrong
    one."""
    if hasattr(signed_tx, "raw_transaction"):
        return signed_tx.raw_transaction
    return signed_tx.rawTransaction


def main() -> None:
    private_key = os.environ.get("DEPLOYER_PRIVATE_KEY")
    if not private_key:
        sys.exit(
            "DEPLOYER_PRIVATE_KEY is not set. Export it first - this is the "
            "wallet that pays gas for the deployment transaction, needs a "
            "small BOT balance on the target chain."
        )
    rpc_url = os.environ.get("BOTCHAIN_RPC_URL")
    if not rpc_url:
        sys.exit(
            "BOTCHAIN_RPC_URL is not set. For testnet: https://rpc.bohr.life"
        )

    print(f"Installing solc {SOLC_VERSION} (one-time download if not cached)...")
    install_solc(SOLC_VERSION)
    set_solc_version(SOLC_VERSION)

    print(f"Compiling {CONTRACT_PATH}...")
    source = CONTRACT_PATH.read_text()
    compiled = compile_source(
        source,
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
    )
    # compile_source keys results as "<stdin>:ContractName"
    contract_id, contract_interface = next(iter(compiled.items()))
    abi = contract_interface["abi"]
    bytecode = contract_interface["bin"]

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        sys.exit(f"Could not connect to {rpc_url} - check the RPC URL and network.")

    account = w3.eth.account.from_key(private_key)
    print(f"Deploying from {account.address} on chain id {w3.eth.chain_id}...")

    balance = w3.eth.get_balance(account.address)
    if balance == 0:
        sys.exit(
            f"{account.address} has zero balance on this chain - fund it "
            f"with testnet BOT (faucet.botchain.ai/en/basic) before deploying."
        )

    DesignRegistry = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = DesignRegistry.constructor().build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": w3.eth.chain_id,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(_raw_tx_bytes(signed))
    print(f"Deployment tx sent: {tx_hash.hex()} - waiting for it to be mined...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        sys.exit(f"Deployment transaction reverted. Receipt: {receipt}")

    print(f"\nDesignRegistry deployed at: {receipt.contractAddress}")

    ABI_OUTPUT_PATH.write_text(json.dumps(abi, indent=2))
    print(f"ABI written to {ABI_OUTPUT_PATH} - commit this file to the repo.")

    print(
        f"\nNext step (manual): set DESIGN_REGISTRY_ADDRESS="
        f"{receipt.contractAddress} in Railway's environment variables "
        f"for the main API service, then redeploy it."
    )


if __name__ == "__main__":
    main()
