"""
Real MCP protocol server for NitoCAD, mounted at /mcp on the same FastAPI
app as the existing REST API (web_app.py). Ported from Stitchfren's
app/mcp/server.py - see a2mcp/x402.py's module docstring for why this
replaces mcp-gateway/'s Node "REST route wrapped by a generic x402 package"
approach: that's the shape OKX's A2MCP review rejected on Stitchfren, for
not being integrated with OKX's official Payment SDK.

Requires the `fastmcp` package (added to requirements.txt).

Design decisions, ported from Stitchfren, and one real difference:

- One tool: generate_cad_part. Matches the README's "one real job this
  backend does" framing (parse + generate + validate + STEP/STL export, see
  cad_generator.py's generate_from_text) - no separate parse-only or
  validate-only tools.
- Unlike Stitchfren's run_pattern_job, CADGenerator.generate_from_text is
  synchronous and does blocking file I/O (STEP/STL export, R2 upload) -
  Stitchfren's version was async only because of an optional LLM-narrative
  await. Run it via asyncio.to_thread() below so a single slow generation
  can't block the event loop out from under other requests (including the
  free MCP session-bootstrap methods this same process serves).
- No free preview tool. Stitchfren has draft_and_nest_pattern_preview
  because nesting math is worth showing before charging for the DXF. CAD
  generation here doesn't have an equivalent free-to-compute-but-
  worth-withholding artifact - the whole job is the STEP file. FREE_TOOL_NAMES
  is left empty rather than invented; add an entry here (and a matching
  tool above) if that changes.
- Payment gating happens in X402Gate below, NOT inside the tool function.
  initialize/notifications/initialized/ping always stay free (session
  bootstrap), and tools/list is free once a session exists (i.e. after a
  free initialize) - so a real client can complete the handshake and
  discover the tool without paying. A bare/sessionless tools/list POST (an
  automated x402 prober, not a real MCP client) is still priced - see
  X402Gate/_is_free for the exact rule and why, ported verbatim from
  Stitchfren since it's what OKX's own checker was observed to require
  there (402 on GET, a generic POST body, and a bare tools/list POST, not a
  protocol-level 400).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastmcp import FastMCP

from a2mcp.x402 import build_paid_app
from cad_generator import CADGenerator

mcp = FastMCP("NitoCAD")
generator = CADGenerator()


def _build_message(result: dict[str, Any]) -> str:
    """
    Plain-language "message" field, ported from mcp-gateway/server.js's
    buildMessage() so the new Python MCP tool gives an agent the same
    front-and-center summary the Node gateway did, without depending on it.
    """
    if not result.get("success"):
        return f"Could not generate this part: {result.get('error') or 'unknown error'}."

    params = result.get("parameters") or {}
    part_type = str(params.get("part_type") or "part").replace("_", " ")
    parts = [f"Your {part_type} is ready as a STEP file."]

    validation = result.get("validation") or {}
    warnings = validation.get("warnings") or []
    corrections = validation.get("corrections") or {}
    if corrections:
        parts.append(f"{len(corrections)} parameter(s) were auto-corrected to keep the geometry valid.")
    if warnings:
        parts.append(" ".join(warnings))
    return " ".join(parts)


def _build_tool_response(result: dict[str, Any]) -> dict[str, Any]:
    """Same field shape mcp-gateway/server.js's buildAgentResponse used, so
    any existing consumer built against that JSON shape keeps working."""
    params = result.get("parameters") or {}
    return {
        "ok": bool(result.get("success")),
        "job_id": result.get("job_id"),
        "message": _build_message(result),
        "download_url": result.get("step_url"),
        "stl_url": result.get("stl_url"),
        "part_type": params.get("part_type"),
        "parameters": params.get("parameters"),
        "material": params.get("material"),
        "validation": result.get("validation"),
        "error": result.get("error"),
    }


@mcp.tool
async def generate_cad_part(
    description: str,
    use_deepseek: bool | None = None,
    api_key: str | None = None,
    model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """
    Convert a plain-English mechanical part description into a
    manufacturable .STEP CAD file (with .STL for preview), using
    CadQuery/OpenCASCADE. Returns a download link plus the parsed
    parameters and any validation warnings/auto-corrections. Price: 0.50
    USDT per call.

    description: e.g. "L-bracket 50mm wide, 60mm tall, 40mm deep, 3mm
    thick, 2 holes per leg". See README for the full list of supported
    part types (23 templates: brackets, plates, shafts, gears, bearings,
    pulleys, enclosures, and more).

    use_deepseek: force DeepSeek LLM parsing (true) or force regex parsing
    (false); omit to let the server auto-detect based on key availability.

    api_key: your own DeepSeek API key, only needed if you want LLM
    parsing without relying on this server's own key.

    model: "deepseek-v4-flash" (default) or "deepseek-v4-pro".
    """
    result = await asyncio.to_thread(
        generator.generate_from_text,
        description,
        use_deepseek=use_deepseek,
        api_key=api_key,
        model=model,
        user_id="mcp-a2mcp",
    )
    return _build_tool_response(result)


# fastmcp's http_app() returns a Starlette ASGI app speaking the MCP
# Streamable HTTP transport at the given path. Mounted at "/mcp" in
# web_app.py, so the full route ends up being POST/GET /mcp/.
mcp_app = mcp.http_app(path="/")


# Methods that must stay reachable with NO payment, because they're what a
# client needs just to bootstrap/maintain an MCP session - not because
# they're "the free part of the product." tools/list is deliberately NOT in
# this set (see _is_free's docstring).
FREE_METHODS = {"initialize", "notifications/initialized", "ping"}

# No free tools in NitoCAD's single-tool surface - see this module's
# docstring for why a preview tool wasn't invented to fill this set.
FREE_TOOL_NAMES: set[str] = set()


def _validate_tool_call(payload: dict[str, Any]) -> str | None:
    """
    Structural + required-argument validation for a tools/call payload,
    run in X402Gate BEFORE payment routing. OKX's A2MCP review rejected
    NitoCAD for validating parameters only after the buyer was already
    charged (see the 402-then-400-with-payment-header pattern in the
    Railway logs: PaymentMiddlewareASGI settled payment, then fastmcp/
    generate_cad_part discovered the request was malformed). Anything
    this function would reject must never reach paid_app - X402Gate
    checks this first and returns its own 400 with no charge attempted.

    Only checks what's needed to guarantee the paid tool call can
    actually run: does the envelope look like JSON-RPC, does the tool
    name exist, and is the one required argument (description) present
    and non-empty. Deliberately does NOT duplicate CADGenerator's
    geometry-level validation (hole/fillet auto-correction etc.) - that
    class of "validation" legitimately can't happen before parsing the
    description, and OKX's complaint was about basic request validity,
    not geometry correctness.

    Returns an error message string if invalid, None if the call should
    proceed to payment routing.
    """
    if payload.get("jsonrpc") != "2.0":
        return "Invalid Request: missing or wrong 'jsonrpc' version, expected \"2.0\"."
    if "id" not in payload:
        return "Invalid Request: missing 'id'."

    params = payload.get("params")
    if not isinstance(params, dict):
        return "Invalid params: 'params' must be an object."

    name = params.get("name")
    if name != "generate_cad_part":
        return f"Unknown tool: {name!r}. Only 'generate_cad_part' is available."

    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return "Invalid params: 'arguments' must be an object."

    description = arguments.get("description")
    if not isinstance(description, str) or not description.strip():
        return "Invalid params: 'arguments.description' is required and must be a non-empty string."

    return None


def _is_free(body: bytes, headers: dict[str, str]) -> bool:
    """
    Default-DENY: only explicit session-bootstrap plumbing (FREE_METHODS)
    or an in-session tools/list counts as free. Everything else, including
    a bare/sessionless tools/list and non-JSON-RPC probe bodies, is "not
    free" and gets routed to the PaymentMiddlewareASGI-wrapped app instead
    of fastmcp directly - matching OKX's own x402-check prober, which
    doesn't do a full MCP handshake before probing for pricing and expects
    ANY unauthenticated hit on a route=x402 resource to come back as a 402
    with accepts[], not a protocol-level 400.
    """
    try:
        payload = json.loads(body)
    except Exception:
        return False  # unparseable / non-JSON-RPC probe body -> priced path
    if not isinstance(payload, dict):
        return False

    method = payload.get("method")
    if method in FREE_METHODS:
        return True
    if method == "tools/list" and "mcp-session-id" in headers:
        return True
    if method == "tools/call":
        params = payload.get("params") or {}
        if params.get("name") in FREE_TOOL_NAMES:
            return True
    return False


class AcceptFixer:
    """fastmcp's Streamable HTTP transport answers HTTP 406 ("Client must
    accept both application/json and text/event-stream") to any request
    whose Accept header lacks `text/event-stream`. Some clients and
    automated checkers (including OKX's x402 endpoint validator) don't send
    that header, so they get a 406 and the endpoint reads as "invalid" even
    though the MCP server is healthy and a real paid call works fine.

    This thin ASGI wrapper rewrites the Accept header to include
    `text/event-stream` before fastmcp sees the request, so those probes
    get a genuine MCP response (200) instead of a 406."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            new_headers = []
            had_accept = False
            for k, v in scope.get("headers", []):
                if k.lower() == b"accept":
                    had_accept = True
                    new_headers.append((k, b"application/json, text/event-stream"))
                else:
                    new_headers.append((k, v))
            if not had_accept:
                new_headers.append((b"accept", b"application/json, text/event-stream"))
            scope = dict(scope)
            scope["headers"] = new_headers
        await self.app(scope, receive, send)


class X402Gate:
    """
    Thin ASGI dispatcher in front of two inner apps: `free_app` (fastmcp,
    Accept-header-fixed, no payment gating) and `paid_app` (the same thing
    wrapped in a real PaymentMiddlewareASGI - see x402.py's build_paid_app).
    Buffers the POST body (ASGI bodies can only be read once) so it can
    inspect the JSON-RPC method before deciding which app to forward to,
    then replays the body to whichever app it picks, unchanged.
    """

    def __init__(self, free_app, paid_app):
        self.free_app = free_app
        self.paid_app = paid_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.free_app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        if scope["method"] == "GET":
            # A GET carrying mcp-session-id is the SSE stream for a session
            # that was already bootstrapped via a (free) initialize POST -
            # let it through untouched. A bare GET with no session-id is
            # exactly the kind of stateless probe OKX's x402-check tool
            # sends; route it to paid_app so PaymentMiddlewareASGI answers
            # with its own 402 challenge.
            if "mcp-session-id" not in headers:
                await self.paid_app(scope, receive, send)
            else:
                await self.free_app(scope, receive, send)
            return

        if scope["method"] != "POST":
            await self.free_app(scope, receive, send)
            return

        body_chunks = []
        more_body = True
        while more_body:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(body_chunks)

        # Some automated checkers (incl. OKX's x402 endpoint validator) POST
        # an `initialize` with empty/missing `params`, which fastmcp rejects
        # with JSON-RPC -32602 and surfaces to the checker as HTTP 400.
        # Default the params for a bare initialize so the handshake
        # completes and the endpoint reads as valid. A real MCP client
        # sends full params and is unaffected.
        parsed: Any = None
        try:
            parsed = json.loads(body)
            if (
                isinstance(parsed, dict)
                and parsed.get("method") == "initialize"
                and not parsed.get("params")
            ):
                parsed["params"] = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "okx-check", "version": "1.0"},
                }
                body = json.dumps(parsed).encode()
        except Exception:
            parsed = None

        # Reject a malformed/incomplete tools/call BEFORE any payment
        # routing decision - see _validate_tool_call's docstring. This must
        # run ahead of the free/paid split: an invalid call is invalid
        # whether or not it would have been priced, and must never reach
        # paid_app regardless.
        if isinstance(parsed, dict) and parsed.get("method") == "tools/call":
            validation_error = _validate_tool_call(parsed)
            if validation_error is not None:
                print(f"[a2mcp.server] X402Gate: rejecting pre-payment, no charge attempted: {validation_error}")
                error_body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": parsed.get("id"),
                    "error": {"code": -32602, "message": validation_error},
                }).encode()
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": error_body})
                return

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        target = self.paid_app if not _is_free(body, headers) else self.free_app

        # Diagnostic only - logs exactly what X402Gate hands to whichever
        # app it picks, and what status code came back, so it's directly
        # observable whether paid_app (PaymentMiddlewareASGI) is actually
        # intercepting an unpaid request with its own 402, or silently
        # passing it through to fastmcp underneath. Safe to remove once
        # that's settled; doesn't change routing or response behavior.
        target_name = "paid_app" if target is self.paid_app else "free_app"
        print(
            f"[a2mcp.server] X402Gate dispatch: method={scope['method']} "
            f"path={scope.get('path')!r} raw_path={scope.get('raw_path')!r} "
            f"target={target_name} has_payment_header="
            f"{'payment-signature' in headers or 'x-payment' in headers}"
        )

        status_holder = {}

        async def logging_send(message):
            if message.get("type") == "http.response.start":
                status_holder["status"] = message.get("status")
            await send(message)

        await target(scope, replay_receive, logging_send)
        print(f"[a2mcp.server] X402Gate dispatch: target={target_name} responded status={status_holder.get('status')}")


# AcceptFixer runs first on both paths so fastmcp never 406s on headers.
# free_app skips payment gating entirely; paid_app is the same fastmcp app
# wrapped in a real PaymentMiddlewareASGI, built in x402.py, which is what
# OKX's listing check looks for. X402Gate decides which of the two a given
# request goes to (see its docstring for why that decision can't move into
# the SDK).
_free_app = AcceptFixer(mcp_app)
_paid_app = build_paid_app(
    AcceptFixer(mcp_app),
    resource_description=(
        "Convert a plain-English mechanical part description into a "
        "manufacturable .STEP CAD file (with .STL for preview), using "
        "CadQuery/OpenCASCADE. Returns a download link plus the parsed "
        "parameters and any validation warnings/auto-corrections."
    ),
)
mcp_app_gated = X402Gate(_free_app, _paid_app)
