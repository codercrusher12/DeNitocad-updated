"""
x402 payment gate for NitoCAD's MCP tool - ported from Stitchfren's
app/mcp/x402.py, which is the pattern OKX's A2MCP review actually approved.

WHY THIS FILE EXISTS INSTEAD OF backend equivalent: mcp-gateway/server.js
(the Node service) is NitoCAD's original A2MCP integration - a hand-rolled
REST route wrapped by the generic community @x402/evm npm package. Stitchfren
shipped an identical-shape Node gateway first and got rejected by OKX's
listing review for the stated reason "service isn't integrated with the
official OKX Payment SDK." Stitchfren's fix was this file: build the real
x402.http.middleware.fastapi.PaymentMiddlewareASGI from OKX's own Python SDK
(okxweb3-app-x402), mounted directly on the FastAPI backend, instead of a
separate Node service calling the generic TypeScript SDK. This file is that
same fix, ported to NitoCAD. mcp-gateway/ should be decommissioned once this
path is verified live - see its own file for the deprecation note.

UNVERIFIED, flagged rather than guessed at - same discipline the mcp-gateway
Node rewrite used for its own SDK-shape guesses:
  - PaymentMiddlewareASGI's constructor signature and RouteConfig/
    PaymentOption's exact field names are carried over from Stitchfren's
    working file, not re-derived from reading okxweb3-app-x402's installed
    source - I don't have network access in this environment to
    `pip install okxweb3-app-x402` and confirm them directly.
  - Whether a dollar-string price ("$0.50") resolves cleanly against OKX's
    own facilitator for eip155:196 the same way it did in Stitchfren's
    deployment is inferred from Stitchfren's working code and comments, not
    independently re-verified against a live call from here.
Do one real paid call against this in staging before trusting it in
production, and if PaymentMiddlewareASGI's constructor rejects any of these
kwargs, paste the TypeError back - it'll name the actual accepted signature.
"""

from __future__ import annotations

import os

from x402.http import (
    OKXAuthConfig,
    OKXFacilitatorClient,
    OKXFacilitatorConfig,
    PaymentOption,
)
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.server import x402ResourceServer

NETWORK = "eip155:196"  # X Layer, same chain the old Node gateway targeted

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://web3.okx.com")

# USD string, per OKX's own quickstart price format ("$0.1"-style) - the
# middleware converts this to atomic units itself via the scheme registered
# below. This is deliberately NOT the explicit TokenAmount object
# (amountInAtomicUnits/asset/eip712) the Node gateway needed - that was only
# required there because the generic community @x402/evm package has no
# default-asset entry for eip155:196. OKX's own SDK is expected to resolve
# X Layer pricing internally, same as it does in Stitchfren's deployment.
# Confirm with one real paid call in staging before trusting this.
PRICE_USD = os.getenv("NL_TO_CAD_PRICE_USD", "$0.50")

FACILITATOR_REQUIRED_ENV = [OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE]

# Explicit, opt-in-only escape hatch for local dev when facilitator creds
# aren't set. Defaults OFF. Never set this on the deploy OKX's listing
# points at - see build_paid_app()'s fail-closed behavior below for why.
ALLOW_UNPAID_MCP = os.getenv("NL_TO_CAD_ALLOW_UNPAID_MCP", "false").strip().lower() == "true"


def facilitator_is_configured() -> bool:
    return all(FACILITATOR_REQUIRED_ENV) and bool(PAY_TO_ADDRESS)


class PaymentConfigError(Exception):
    """Raised at paid-app build time if facilitator creds are missing and
    the explicit local-dev opt-out isn't set. Message is safe to log."""


_facilitator: OKXFacilitatorClient | None = None
_resource_server: x402ResourceServer | None = None


def _get_resource_server() -> x402ResourceServer:
    """
    Lazy singleton - import-time (tests, or before env vars are set)
    shouldn't crash just because nothing's called yet.
    """
    global _facilitator, _resource_server
    if _resource_server is None:
        _facilitator = OKXFacilitatorClient(
            OKXFacilitatorConfig(
                auth=OKXAuthConfig(
                    api_key=OKX_API_KEY or "",
                    secret_key=OKX_SECRET_KEY or "",
                    passphrase=OKX_PASSPHRASE or "",
                ),
                base_url=OKX_BASE_URL,
                sync_settle=True,
            )
        )
        _resource_server = x402ResourceServer(_facilitator)
        _resource_server.register(NETWORK, ExactEvmScheme())
    return _resource_server


def _payment_routes(resource_description: str) -> dict:
    """
    Registers both "POST /mcp" and "GET /mcp": X402Gate forwards two kinds
    of unpaid requests to paid_app - a priced tools/call (POST) and a
    sessionless GET (the SSE-stream-open probe OKX's x402 checker sends,
    per X402Gate's own GET-handling comment) - and both need a matching
    entry here or PaymentMiddlewareASGI has nothing to enforce and passes
    them straight through, same failure mode as the POST-only path bug
    this function used to have.

    NO trailing slash on these keys - confirmed against the installed
    okxweb3-app-x402 (0.1.1) source. x402HTTPResourceServer._normalize_path
    does path.rstrip("/") on every incoming request path before matching,
    but _parse_route_pattern builds the match regex straight from the route
    key with no equivalent stripping. A key of "POST /mcp/" compiles to
    ^\\/mcp\\/$, which the normalized request path "/mcp" (slash already
    stripped) can never match - so the route silently never matches and
    PaymentMiddlewareASGI passes every request straight through unguarded
    instead of returning 402. "POST /mcp" (no trailing slash) compiles to
    ^\\/mcp$, which does match. Verified via inspect.getsource() on
    x402.http.middleware.fastapi.x402HTTPResourceServer._normalize_path /
    _parse_route_pattern - not re-derived from memory.
    """
    option = PaymentOption(
        scheme="exact",
        price=PRICE_USD,
        network=NETWORK,
        pay_to=PAY_TO_ADDRESS,
        max_timeout_seconds=60,
    )
    return {
        "POST /mcp": RouteConfig(
            accepts=[option],
            description=resource_description,
            mime_type="application/json",
        ),
        "GET /mcp": RouteConfig(
            accepts=[option],
            description=resource_description,
            mime_type="application/json",
        ),
    }



def build_paid_app(inner_app, resource_description: str):
    """
    Wraps `inner_app` in the real PaymentMiddlewareASGI: routes from
    _payment_routes() ("POST /mcp/" and "GET /mcp/", see that function's
    docstring for why the path is the full unstripped one, not "/"),
    server=x402ResourceServer with ExactEvmScheme registered for eip155:196.

    Fails closed: if facilitator creds or PAY_TO_ADDRESS aren't set and the
    explicit local-dev opt-out (NL_TO_CAD_ALLOW_UNPAID_MCP=true) isn't
    either, raises PaymentConfigError rather than silently building an app
    that would deliver the paid result for free.
    """
    # Diagnostic only - prints once at startup so Railway's deploy log says
    # in plain text which branch this call took, instead of that having to
    # be inferred later from how a live request behaves. Safe to remove
    # once the 402-vs-passthrough question is settled; doesn't change
    # behavior.
    print(
        f"[a2mcp.x402] build_paid_app: facilitator_is_configured="
        f"{facilitator_is_configured()} ALLOW_UNPAID_MCP={ALLOW_UNPAID_MCP} "
        f"OKX_API_KEY_set={bool(OKX_API_KEY)} OKX_SECRET_KEY_set={bool(OKX_SECRET_KEY)} "
        f"OKX_PASSPHRASE_set={bool(OKX_PASSPHRASE)} PAY_TO_ADDRESS_set={bool(PAY_TO_ADDRESS)}"
    )

    if not facilitator_is_configured():
        if ALLOW_UNPAID_MCP:
            # Explicit opt-in only, for local dev with no OKX creds at
            # hand. Never set this on the deploy OKX's listing points at.
            print(
                "[a2mcp.x402] build_paid_app: returning UNWRAPPED inner_app "
                "(ALLOW_UNPAID_MCP=true) - no payment gate on this path."
            )
            return inner_app
        raise PaymentConfigError(
            "Payment processing is not configured on this deployment "
            "(missing OKX facilitator credentials or PAY_TO_ADDRESS) - "
            "refusing to build the paid MCP path without confirmed "
            "on-chain settlement capability."
        )

    server = _get_resource_server()
    routes = _payment_routes(resource_description)
    print(f"[a2mcp.x402] build_paid_app: returning PaymentMiddlewareASGI, routes={list(routes.keys())}")
    return PaymentMiddlewareASGI(inner_app, routes=routes, server=server)
