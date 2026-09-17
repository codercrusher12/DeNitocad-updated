"""
Agent-to-agent MCP server for BOT Chain callers - separate from a2mcp/
(OKX's listing on X Layer, gated via OKX's official Payment SDK).
Mounted at /mcp-bot on the same FastAPI app, not /mcp - different
payment rails, shouldn't share a mount.

Identity and payment here now match web_app.py's HTTP API exactly -
wallet sessions and prepaid credits, not per-call BOT payments. This
used to be the one part of the project still on the old per-call-
payment model (see the project's own migration notes: it was left
alone through the wallet-auth/credits rollout because it had no
API-key-equivalent identity to fall back on - the payment transaction
itself WAS the identity proof). That gap is closed now: a wallet
session is just as available to a headless agent as it is to a
browser, since "sign a message" is strictly simpler than "send a
transaction," and any caller here already holds a private key capable
of the latter.

What an agent does before calling either tool below, all plain HTTP
JSON (no MCP tool needed for this part - these endpoints have no
CORS/browser dependency, any HTTP client reaches them fine):
  1. POST /auth/nonce {wallet_address} -> {nonce, message}
  2. Sign `message` with the wallet's private key (eth_account's
     Account.sign_message + encode_defunct, or equivalent) - free, no
     gas, nothing on-chain.
  3. POST /auth/verify {wallet_address, nonce, signature} -> {session_id}
  4. POST /credits/purchase {tx_hash, tier} with
     Authorization: Bearer <session_id> - same three tiers as the web
     app (single/pack_1000/pack_10000), needs one real on-chain payment
     just like before, but now it buys a balance instead of paying per
     call.
Then pass that session_id into generate_cad_part/export_format below.
A session lasts WALLET_SESSION_TTL_SECONDS (7 days by default) - an
agent making many calls doesn't need to re-sign for each one.

Two tools:
  - generate_cad_part: spends 1 credit from the session's wallet
    balance (db.consume_credit) instead of requiring a fresh payment.
    Only STL is produced (matches web_app.py's /generate - see
    cad_generator.py's _build_workplane split).
  - export_format: pulls STEP/IGES/DXF/PDF for a job created by
    generate_cad_part - free, included in the credit already spent to
    create the job. Ownership is checked by wallet address (the
    session's, matched against the job's stored owner) - not billed
    again, same reasoning as GET /export/{fmt}/{job_id} in web_app.py.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

import db
import wallet_auth
from cad_generator import CADGenerator
from config import settings
from exceptions import NitocadError

mcp = FastMCP("nitocad-botchain")
generator = CADGenerator()


@mcp.tool
async def generate_cad_part(description: str, session_id: str) -> dict:
    """Generate a CAD part from a natural-language description.

    Requires a wallet session (see this module's docstring for how to
    get one - POST /auth/nonce then POST /auth/verify, both plain HTTP)
    and spends 1 credit from that wallet's balance. If the balance is
    0, buy more via POST /credits/purchase before calling this.

    Only STL is produced (drives a 3D preview) - call export_format
    afterward for STEP/IGES/DXF/PDF, included at no extra cost.
    """
    wallet = wallet_auth.resolve_session(session_id)
    if wallet is None:
        return {
            "success": False,
            "error": "Invalid or expired session_id. Sign in again via "
            "POST /auth/nonce then POST /auth/verify - see this server's "
            "module docstring for the full flow.",
        }

    if not db.consume_credit(wallet["wallet_address"]):
        balance = db.get_credit_balance(wallet["wallet_address"])
        return {
            "success": False,
            "error": f"Insufficient credits (balance: {balance}). Buy more via "
            "POST /credits/purchase with this session_id as the bearer token.",
        }

    try:
        result = await asyncio.to_thread(
            generator.generate_from_text,
            description,
            user_id=wallet["wallet_address"],
            formats=["stl"],
        )
    except NitocadError as exc:
        return {"success": False, "error": exc.message}

    return result


@mcp.tool
async def export_format(job_id: str, fmt: str, session_id: str) -> dict:
    """Export a specific format (step/iges/dxf/pdf) for a job previously
    created by generate_cad_part. Free - already covered by the credit
    spent when the job was generated. Requires the same wallet session
    that created the job; a different wallet's session gets "Job not
    found" rather than an ownership error, so a caller can't use this
    to probe which job IDs exist for someone else's wallet.
    """
    wallet = wallet_auth.resolve_session(session_id)
    if wallet is None:
        return {
            "success": False,
            "error": "Invalid or expired session_id. Sign in again via "
            "POST /auth/nonce then POST /auth/verify.",
        }

    job = db.get_job(job_id)
    if job is None or job["user_id"] != wallet["wallet_address"]:
        return {"success": False, "error": "Job not found."}

    try:
        file_path, part_type = await asyncio.to_thread(
            generator.export_format_for_job, job_id, fmt
        )
    except NitocadError as exc:
        return {"success": False, "error": exc.message}

    import storage
    url = storage.upload_export(str(file_path), fmt) if settings.r2_configured else None
    return {
        "success": True,
        "job_id": job_id,
        "format": fmt,
        "part_type": part_type,
        "download_url": url,
        "note": None if url else (
            "R2 not configured on this deploy - the file exists server-side "
            "but there's no reachable URL for a remote caller to fetch it."
        ),
    }


# fastmcp's http_app() returns a Starlette ASGI app speaking the MCP
# Streamable HTTP transport at the given path. Mounted at "/mcp-bot" in
# web_app.py.
mcp_app = mcp.http_app(path="/")
