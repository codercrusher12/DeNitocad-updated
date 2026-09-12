"""
Agent-to-agent MCP server for BOT Chain callers - separate from a2mcp/
(OKX's listing on X Layer, gated via OKX's official Payment SDK).
Mounted at /mcp-bot on the same FastAPI app, not /mcp - different
payment rails, shouldn't share a mount.

Unlike a2mcp/'s X402Gate (HTTP-level 402 negotiation via OKX's SDK),
there's no equivalent protocol on BOT Chain yet - AgentPay is still
roadmap (see easycad's decision log). Payment here is simpler and more
manual: the caller pays botchain_pay.PER_CALL_PRICE_BOT (or
KEY_ISSUE_PRICE_BOT, N/A here - key issuance is HTTP-only, see
web_app.py) to the treasury address BEFORE calling a tool, then passes
the resulting tx_hash as a tool argument. Verified inside the tool
handler via botchain_pay - there's no negotiation step to intercept.

Two tools:
  - generate_cad_part: same "one real job this backend does" framing as
    a2mcp/'s generate_cad_part, but priced in native BOT. Only STL is
    produced (matches web_app.py's /generate - see cad_generator.py's
    _build_workplane split). No expected_sender check on the payment
    here - there's no prior identity for a first-time MCP caller to be
    checked against; the paying wallet IS the identity, same reasoning
    as key issuance in web_app.py.
  - export_format: pulls STEP/IGES/DXF/PDF for a job created by
    generate_cad_part, priced per call, ownership checked by wallet
    address (not an API key - MCP callers here never had one). Unlike
    generate_cad_part, this DOES pass expected_sender once the caller
    is known via the job's stored owner, closing the tx-hash-sniping
    gap a ShieldGuard audit surfaced for any call that already has an
    established identity to check against.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

import botchain_pay
import db
from cad_generator import CADGenerator
from config import settings
from exceptions import NitocadError, PaymentError

mcp = FastMCP("nitocad-botchain")
generator = CADGenerator()


@mcp.tool
async def generate_cad_part(description: str, tx_hash: str) -> dict:
    """Generate a CAD part from a natural-language description, paid
    per call in native BOT on BOT Chain. Caller must first send
    botchain_pay.PER_CALL_PRICE_BOT BOT to the treasury address, then
    pass the resulting transaction hash as tx_hash.

    Only STL is produced (drives a 3D preview) - call export_format
    afterward, paying again, for STEP/IGES/DXF/PDF.
    """
    try:
        caller = botchain_pay.verify_and_record_payment(
            tx_hash,
            purpose="generate",
            min_amount_bot=botchain_pay.PER_CALL_PRICE_BOT,
            user_id="pending",
        )
    except PaymentError as exc:
        return {"success": False, "error": exc.message}

    try:
        result = await asyncio.to_thread(
            generator.generate_from_text,
            description,
            user_id=caller,
            formats=["stl"],
        )
    except NitocadError as exc:
        return {"success": False, "error": exc.message}

    return result


@mcp.tool
async def export_format(job_id: str, fmt: str, tx_hash: str) -> dict:
    """Export a specific format (step/iges/dxf/pdf) for a job previously
    created by generate_cad_part, paid per call in BOT. The paying
    wallet must be the same one that created the job - a payment from a
    different wallet is rejected even if it's otherwise valid, since it
    would mean either wallet could pull files that belong to the other.
    """
    # Job lookup happens BEFORE payment verification here, on purpose -
    # it's what lets expected_sender be enforced at all. A cheap db read,
    # not gated on payment, is fine to do first.
    job = db.get_job(job_id)
    if job is None:
        return {"success": False, "error": "Job not found."}

    try:
        caller = botchain_pay.verify_and_record_payment(
            tx_hash,
            purpose="generate",
            min_amount_bot=botchain_pay.PER_CALL_PRICE_BOT,
            user_id=job["user_id"],
            expected_sender=job["user_id"],
        )
    except PaymentError as exc:
        return {"success": False, "error": exc.message}

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
