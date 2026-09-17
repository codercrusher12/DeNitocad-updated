"""
Enhanced web interface with 3D preview.
"""
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import db
from a2mcp.server import mcp_app, mcp_app_gated
from a2mcp_botchain.server import mcp_app as mcp_app_botchain
import botchain_pay
import prompt_refine
import wallet_auth
from cad_generator import CADGenerator
from config import settings
from eth_utils import is_address
from exceptions import GenerationError, PaymentError, UnsupportedFormatError, register_exception_handlers
from file_safety import safe_output_path
from logging_config import configure_logging, get_logger, set_request_id

configure_logging(level=settings.LOG_LEVEL, fmt=settings.LOG_FORMAT)
logger = get_logger(__name__)

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info(
        "starting nitocad api",
        extra={"environment": settings.ENVIRONMENT, "r2_configured": settings.r2_configured},
    )
    # fastmcp's Streamable HTTP transport needs its own session manager
    # running for the lifetime of the app (this is what makes /mcp actually
    # answer instead of 404) - the plain @app.on_event("startup") hook this
    # replaced doesn't provide that; a lifespan context manager is required.
    # Two mounted fastmcp apps now (/mcp for OKX/X Layer, /mcp-bot for BOT
    # Chain) - both lifespans need to be active for the app's lifetime, so
    # they're nested rather than picking one.
    async with mcp_app.lifespan(app):
        async with mcp_app_botchain.lifespan(app):
            yield
    logger.info("shutting down nitocad api")


app = FastAPI(
    title="Natural Language to CAD",
    version="2.0.0",
    lifespan=lifespan,
    # Hide interactive docs in production by default - flip DOCS_ENABLED
    # (via ENVIRONMENT) if you want them public; they leak the full
    # request/response schema and every route including /auth/* and
    # /credits/*.
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
register_exception_handlers(app)

# Real MCP protocol server (initialize / tools/list / tools/call), x402
# payment-gated per-call for the one priced tool. See a2mcp/server.py.
# Supersedes mcp-gateway/ (the Node service) - see that file's deprecation
# note.
app.mount("/mcp", mcp_app_gated)

# BOT Chain agent-to-agent mount - separate payment rail from the OKX/X
# Layer listing above (native BOT, manually verified via botchain_pay.py,
# not OKX's Payment SDK), so it gets its own mount rather than sharing
# /mcp. No gate wrapper here - payment is verified inside each tool
# handler itself (see a2mcp_botchain/server.py's module docstring for why
# that's the right shape for a pay-then-prove-it flow instead of an
# HTTP-402-negotiation flow).
app.mount("/mcp-bot", mcp_app_botchain)

# The static demo frontend deploys separately (Vercel/Netlify, no build
# step - see README) and calls this API cross-origin, same split as
# Stitchfren's frontend/backend/mcp-gateway architecture. Driven by
# CORS_ORIGINS (config.py) - set it to your real frontend domain(s) before
# going live; the "*" default is fine for local testing only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """Assigns a short correlation id to every request (echoed back as
    X-Request-ID and attached to every log line emitted while handling
    it, via logging_config's contextvar) and logs one structured access
    line per request with status + timing - the closest thing to
    uvicorn's own access log, but structured and going through the same
    logger/formatter as everything else instead of a separate stream."""
    request_id = set_request_id(request.headers.get("x-request-id"))
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "unhandled error on %s %s", request.method, request.url.path,
            extra={"request_id": request_id},
        )
        raise
    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "%s %s -> %s (%.1fms)",
        request.method, request.url.path, response.status_code, duration_ms,
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration_ms, 1),
        },
    )
    return response


generator = CADGenerator()

class GenerateRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=4000)
    # None = auto (server decides based on key availability - see
    # deepseek_parser.parse_description). True/False force one path
    # explicitly. The public demo frontend sends neither, relying on auto.
    use_deepseek: bool | None = None
    # NOTE: this is the caller's own DeepSeek key, used to parse the
    # description - unrelated to the Authorization: Bearer <session_id>
    # header that authenticates the wallet session against this
    # service. Two different keys, two different purposes.
    api_key: str | None = None
    model: str = "deepseek-v4-flash"
    # No longer honored - /generate now always builds STL only (the
    # preview). Kept on the model so old callers that still send it don't
    # get a 422 on an unrecognized field; the value is ignored. Every
    # other format is deferred to GET /export/{fmt}/{job_id}, built on
    # demand but not separately billed - see that endpoint's docstring.
    formats: list[str] | None = None
    # tx_hash removed here - /generate no longer takes a payment
    # directly. It now spends 1 credit (db.consume_credit) from the
    # signed-in wallet's balance instead - see POST /credits/purchase
    # to top that balance up, and wallet_auth.py for how the wallet
    # itself gets identified (a session, not an API key).

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank")
        return v


class PreviewRequest(BaseModel):
    """POST /preview - free, no wallet, no signed-in session, STL-only.
    See that route's docstring for why it forces the fallback parser and
    why the resulting job isn't later upgradeable to a paid export."""
    description: str = Field(..., min_length=1, max_length=4000)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank")
        return v


class ValidationInfo(BaseModel):
    warnings: list[str] = []
    errors: list[str] = []
    corrections: dict[str, Any] = {}


class GenerateResponse(BaseModel):
    success: bool
    job_id: str | None = None
    step_file: str | None = None
    stl_file: str | None = None
    iges_file: str | None = None
    dxf_file: str | None = None
    pdf_file: str | None = None
    step_url: str | None = None
    stl_url: str | None = None
    iges_url: str | None = None
    dxf_url: str | None = None
    pdf_url: str | None = None
    parameters: dict[str, Any] | None = None
    validation: ValidationInfo | None = None
    error: str | None = None
    error_type: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


# NOTE: the old POST /api/keys/generate endpoint (and its
# ApiKeyResponse/KeyGenerateRequest models) lived here - issuing an
# API key for a one-time BOTCHAIN_KEY_ISSUE_PRICE_BOT payment, cached
# in one browser via localStorage. Retired: wallet_auth.py's free,
# signature-based sign-in replaced it (see that module's docstring),
# and /generate + /export + /api/jobs* now all authenticate via
# wallet_auth.get_current_wallet instead of security.get_current_key.
# db.py's api_keys table and create_api_key/validate_api_key are gone
# too - see db.py's own module docstring. Going forward only, per the
# project's own migration decision: no conversion path for old keys.


# --------------------------------------------------------- wallet auth ----
# Sign-In-With-Wallet: the free, signature-based login that replaced
# the API-key-bought-with-a-payment model noted above. See
# wallet_auth.py's module docstring for the full flow.

class NonceRequest(BaseModel):
    wallet_address: str


class VerifyRequest(BaseModel):
    wallet_address: str
    nonce: str
    signature: str


@app.post("/auth/nonce")
@limiter.limit("20/minute")
async def auth_nonce(request: Request, body: NonceRequest):
    """Step 1 of wallet sign-in. Unauthenticated and free (no payment,
    no gas), so it's the one wallet-auth endpoint that could be
    hammered to grow the nonces table if left unlimited - rate-limited
    generously but not unlimited."""
    if not is_address(body.wallet_address):
        raise HTTPException(status_code=422, detail="Not a valid wallet address")
    return wallet_auth.issue_nonce(body.wallet_address)


@app.post("/auth/verify")
@limiter.limit("20/minute")
async def auth_verify(request: Request, body: VerifyRequest):
    """Step 2 of wallet sign-in. Returns a session_id - send it back on
    every subsequent request as 'Authorization: Bearer <session_id>'."""
    return wallet_auth.verify_and_create_session(body.wallet_address, body.nonce, body.signature)


@app.get("/auth/me")
async def auth_me(wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)]):
    """Lets the frontend check 'am I still signed in' and show the
    connected address, without that check itself costing anything or
    touching the chain."""
    return {"wallet_address": wallet["wallet_address"]}


@app.post("/auth/logout")
async def auth_logout(wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)]):
    """Explicit sign-out - deletes the session row immediately, rather
    than leaving the frontend to just forget the session_id and wait
    for it to expire on its own."""
    db.delete_session(wallet["session_id"])
    return {"success": True}


# ------------------------------------------------------------ credits ----
# Prepaid generation balance per wallet - see db.py's own comment on
# wallet_credits for why pay-as-you-go and bulk packs are one mechanism
# underneath. Requires a wallet session (not an API key) - this is the
# new /generate's identity path, see that endpoint below.

def _credit_tiers() -> dict[str, tuple]:
    """tier name -> (price_bot, credits_granted). A function, not a
    module-level constant, so it always reads the current
    botchain_pay.* values (tests / different environments can patch
    those) rather than freezing them at import time."""
    return {
        "single": (botchain_pay.CREDIT_PRICE_BOT, 1),
        "pack_1000": (botchain_pay.PACK_1000_PRICE_BOT, settings.BOTCHAIN_PACK_1000_CREDITS),
        "pack_10000": (botchain_pay.PACK_10000_PRICE_BOT, settings.BOTCHAIN_PACK_10000_CREDITS),
    }


class CreditsPurchaseRequest(BaseModel):
    tx_hash: str
    tier: str  # "single" | "pack_1000" | "pack_10000" - validated below,
    # not with Literal[...], so an unrecognized tier gets this endpoint's
    # own clear 422 message rather than FastAPI's generic enum error.


@app.get("/credits/balance")
async def credits_balance(wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)]):
    balance = db.get_credit_balance(wallet["wallet_address"])
    return {
        "wallet_address": wallet["wallet_address"],
        "balance": balance,
        # Convenience flag so the frontend doesn't need to know
        # BULK_TIER_CREDIT_THRESHOLD itself to decide whether to show
        # the prompt-refinement chat entry point - it's also exposed
        # directly via GET /config/chain for anywhere that needs the
        # raw number (e.g. an upsell message before reaching it).
        "prompt_refine_eligible": balance >= settings.BULK_TIER_CREDIT_THRESHOLD,
    }


@app.post("/credits/purchase")
@limiter.limit(settings.RATE_LIMIT_GENERATE)
async def credits_purchase(
    request: Request,
    body: CreditsPurchaseRequest,
    wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)],
):
    """Verifies a payment at the declared tier's exact price and credits
    the signed-in wallet accordingly. expected_sender is the signed-in
    wallet, not left open - otherwise anyone could grab someone else's
    qualifying tx_hash off-chain (chain data is public) and credit
    their own account with it instead. See botchain_pay.py's module
    docstring for the same reasoning applied to /generate and
    /export."""
    tiers = _credit_tiers()
    if body.tier not in tiers:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown tier {body.tier!r} - must be one of {sorted(tiers)}.",
        )
    price_bot, credits_granted = tiers[body.tier]

    try:
        botchain_pay.verify_and_record_payment(
            body.tx_hash,
            purpose=f"credits_{body.tier}",
            min_amount_bot=price_bot,
            user_id=wallet["wallet_address"],
            expected_sender=wallet["wallet_address"],
        )
    except PaymentError as exc:
        raise HTTPException(status_code=402, detail=exc.message) from exc

    new_balance = db.add_credits(wallet["wallet_address"], credits_granted)
    logger.info(
        "credits purchased",
        extra={"wallet_address": wallet["wallet_address"], "tier": body.tier, "credits_granted": credits_granted},
    )
    return {"wallet_address": wallet["wallet_address"], "credits_granted": credits_granted, "balance": new_balance}


# ------------------------------------------------------ prompt refinement ----
# A perk for bulk-tier wallets, not a separate purchase - see
# prompt_refine.py's module docstring and config.py's comment on
# BULK_TIER_CREDIT_THRESHOLD. Never touches the CAD engine.

class PromptRefineMessage(BaseModel):
    role: str  # "user" | "assistant" - validated below, not with
    # Literal[...], so a bad value gets this endpoint's own clear 422
    # rather than FastAPI's generic enum error.
    content: str

    @field_validator("role")
    @classmethod
    def role_must_be_user_or_assistant(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError('role must be "user" or "assistant"')
        return v

    @field_validator("content")
    @classmethod
    def content_within_length_limit(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content cannot be blank")
        if len(v) > settings.PROMPT_REFINE_MAX_MESSAGE_CHARS:
            raise ValueError(
                f"content exceeds {settings.PROMPT_REFINE_MAX_MESSAGE_CHARS} characters"
            )
        return v


class PromptRefineRequest(BaseModel):
    messages: list[PromptRefineMessage]

    @field_validator("messages")
    @classmethod
    def messages_within_history_limit(cls, v: list) -> list:
        if not v:
            raise ValueError("messages cannot be empty")
        if len(v) > settings.PROMPT_REFINE_MAX_HISTORY_MESSAGES:
            raise ValueError(
                f"conversation exceeds {settings.PROMPT_REFINE_MAX_HISTORY_MESSAGES} messages - "
                "start a new refinement session"
            )
        if v[-1].role != "user":
            raise ValueError("the last message must be from the user - nothing to reply to otherwise")
        return v


@app.post("/prompt-refine")
@limiter.limit(settings.RATE_LIMIT_PROMPT_REFINE)
async def prompt_refine_chat(
    request: Request,
    body: PromptRefineRequest,
    wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)],
):
    """Chat-style prompt refinement - DeepSeek only, never calls the CAD
    engine (see prompt_refine.py's module docstring). Gated on the
    signed-in wallet's CURRENT credit balance being at or above
    BULK_TIER_CREDIT_THRESHOLD, not a permanent "ever bought a pack"
    flag - access turns off the moment the balance drops below it, on
    the same lookup /generate already does for spending a credit.
    Costs nothing to call (no credit spent, no BOT payment) - rate-
    limited on its own (RATE_LIMIT_PROMPT_REFINE) instead, since a real
    DeepSeek call still costs real tokens with no payment attached to
    absorb abuse the way /generate's credit spend naturally does."""
    balance = db.get_credit_balance(wallet["wallet_address"])
    if balance < settings.BULK_TIER_CREDIT_THRESHOLD:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Prompt refinement is available to wallets holding at least "
                f"{settings.BULK_TIER_CREDIT_THRESHOLD} credits (you have {balance}). "
                "Buy a credit pack via POST /credits/purchase to unlock it."
            ),
        )
    if not settings.DEEPSEEK_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Prompt refinement isn't configured on this server right now.",
        )

    try:
        reply = prompt_refine.refine_prompt(
            [{"role": m.role, "content": m.content} for m in body.messages],
            api_key=settings.DEEPSEEK_API_KEY,
            model=settings.PROMPT_REFINE_MODEL,
        )
    except prompt_refine.PromptRefineError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

    return {"reply": reply}


@app.get("/config/chain")
async def get_chain_config():
    """Single source of truth for the frontend's wallet connection -
    avoids hardcoding TREASURY_ADDRESS/chain params twice (once in
    config.py, once in the demo HTML). settings.BOTCHAIN_ENVIRONMENT
    picks testnet vs mainnet for both this endpoint and botchain_pay's
    own verifier - one switch, can't drift apart."""
    return {
        "treasury_address": settings.TREASURY_ADDRESS,
        "chain_id_hex": settings.botchain_chain_id_hex,
        "chain_name": "BOT Chain Testnet" if settings.BOTCHAIN_ENVIRONMENT == "testnet" else "BOT Chain",
        "rpc_url": settings.botchain_rpc_url,
        "explorer_url": settings.botchain_explorer_url,
        "currency_symbol": "BOT",
        # Credits pricing - see POST /credits/purchase and config.py's
        # own comment on why the two packs share a per-credit rate.
        # (key_issue_price_bot / per_call_price_bot used to live here -
        # removed along with the API-key system they priced; nothing
        # charges those amounts anymore, see botchain_pay.py.)
        "credit_price_bot": float(botchain_pay.CREDIT_PRICE_BOT),
        "pack_1000_price_bot": float(botchain_pay.PACK_1000_PRICE_BOT),
        "pack_1000_credits": settings.BOTCHAIN_PACK_1000_CREDITS,
        "pack_10000_price_bot": float(botchain_pay.PACK_10000_PRICE_BOT),
        "pack_10000_credits": settings.BOTCHAIN_PACK_10000_CREDITS,
        "bulk_tier_credit_threshold": settings.BULK_TIER_CREDIT_THRESHOLD,
    }

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the web UI with 3D preview."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Natural Language to CAD</title>
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; 
                max-width: 1200px; 
                margin: 0 auto; 
                padding: 20px;
                background: #f5f5f5;
            }
            .container {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
            }
            .panel {
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            h1 { 
                color: #333;
                grid-column: 1 / -1;
            }
            textarea { 
                width: 100%; 
                height: 120px; 
                margin: 10px 0; 
                padding: 12px; 
                border: 1px solid #ddd;
                border-radius: 4px;
                font-family: inherit;
                font-size: 14px;
            }
            button { 
                padding: 12px 24px; 
                background: #007bff; 
                color: white; 
                border: none; 
                cursor: pointer;
                border-radius: 4px;
                font-size: 14px;
                font-weight: 500;
            }
            button:hover { background: #0056b3; }
            button:disabled { background: #ccc; cursor: not-allowed; }
            .result { margin-top: 20px; }
            .error { 
                color: #dc3545; 
                background: #f8d7da;
                padding: 12px;
                border-radius: 4px;
                border-left: 4px solid #dc3545;
            }
            .success { 
                color: #155724;
                background: #d4edda;
                padding: 12px;
                border-radius: 4px;
                border-left: 4px solid #28a745;
            }
            .warning {
                color: #856404;
                background: #fff3cd;
                padding: 8px;
                border-radius: 4px;
                margin: 8px 0;
                border-left: 4px solid #ffc107;
            }
            pre { 
                background: #f8f9fa; 
                padding: 12px; 
                border-radius: 4px; 
                overflow-x: auto;
                font-size: 12px;
                border: 1px solid #dee2e6;
            }
            .viewer {
                width: 100%;
                height: 500px;
                background: #e9ecef;
                border-radius: 4px;
                border: 1px solid #dee2e6;
                position: relative;
            }
            .viewer canvas {
                width: 100%;
                height: 100%;
            }
            .download-links {
                margin-top: 16px;
            }
            .download-links a {
                display: inline-block;
                margin-right: 16px;
                padding: 8px 16px;
                background: #28a745;
                color: white;
                text-decoration: none;
                border-radius: 4px;
            }
            .download-links a:hover {
                background: #218838;
            }
            .param-grid {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 8px;
                margin: 12px 0;
            }
            .param-item {
                background: #f8f9fa;
                padding: 8px;
                border-radius: 4px;
                font-size: 13px;
            }
            .param-item strong {
                color: #495057;
            }
            .loading {
                text-align: center;
                padding: 40px;
                color: #6c757d;
            }
            .spinner {
                border: 3px solid #f3f3f3;
                border-top: 3px solid #007bff;
                border-radius: 50%;
                width: 40px;
                height: 40px;
                animation: spin 1s linear infinite;
                margin: 0 auto 16px;
            }
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            .examples {
                margin-top: 16px;
            }
            .example-btn {
                display: inline-block;
                margin: 4px;
                padding: 6px 12px;
                background: #e9ecef;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                cursor: pointer;
                font-size: 12px;
            }
            .example-btn:hover {
                background: #dee2e6;
            }
        </style>
        <!--
          build/three.js and build/three.min.js (the old global UMD
          build) were deprecated at r150 and removed entirely at r161,
          same with the legacy non-module examples/js/* loaders. This
          uses an import map + ES modules instead, per
          https://threejs.org/docs/index.html#manual/en/introduction/Installation
        -->
        <script type="importmap">
        {
            "imports": {
                "three": "https://unpkg.com/three@0.180.0/build/three.module.js",
                "three/addons/": "https://unpkg.com/three@0.180.0/examples/jsm/"
            }
        }
        </script>
    </head>
    <body>
        <h1>🔧 Natural Language to Parametric CAD</h1>
        <div style="text-align:center;margin-bottom:20px;">
            <button id="walletBtn" onclick="handleWalletButtonClick()" style="background:#2d3748;color:#fff;border:none;padding:8px 20px;border-radius:6px;cursor:pointer;font-size:0.9rem;">Connect Wallet</button>
        </div>
        
        <div class="container">
            <div class="panel">
                <h2>Describe Your Part</h2>
                <textarea id="description" placeholder="e.g., Mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick"></textarea>
                
                <div class="examples">
                    <strong>Examples:</strong><br>
                    <span class="example-btn" onclick="setExample('mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick')">Motor Mount</span>
                    <span class="example-btn" onclick="setExample('L-bracket 50mm wide, 60mm tall, 40mm deep, 3mm thick, 2 holes per leg, 2mm fillets')">L-Bracket</span>
                    <span class="example-btn" onclick="setExample('flat plate 100x80mm, 5mm thick, 4x3 hole pattern, 3mm corner fillets')">Flat Plate</span>
                    <span class="example-btn" onclick="setExample('shaft 10mm diameter, 50mm long, 0.5mm chamfer')">Shaft</span>
                    <span class="example-btn" onclick="setExample('gear with 20 teeth, module 2, 10mm thick, 5mm bore')">Gear</span>
                    <span class="example-btn" onclick="setExample('box enclosure 100x80x50mm, 3mm walls, with lid')">Box</span>
                    <span class="example-btn" onclick="setExample('bearing 10mm inner, 20mm outer, 5mm wide')">Bearing</span>
                    <span class="example-btn" onclick="setExample('pulley 40mm outer, 10mm belt width, 5mm bore, 15mm thick')">Pulley</span>
                </div>
                
                <br>
                <label>
                    <input type="checkbox" id="useDeepSeek"> Use DeepSeek API (requires API key)
                </label>
                <input type="text" id="apiKey" placeholder="DeepSeek API Key" style="width: 250px; margin-left: 10px; padding: 8px; border: 1px solid #ddd; border-radius: 4px;">
                <br><br>
                <div id="formatOptions">
                    <strong>Available on download (included with your generation, no extra charge):</strong>
                    <label style="margin-left: 10px;"><input type="checkbox" class="fmt-checkbox" value="step" checked> STEP (3D solid)</label>
                    <label style="margin-left: 10px;"><input type="checkbox" class="fmt-checkbox" value="iges"> IGES (3D solid)</label>
                    <label style="margin-left: 10px;"><input type="checkbox" class="fmt-checkbox" value="dxf"> DXF (2D cut layers)</label>
                    <label style="margin-left: 10px;"><input type="checkbox" class="fmt-checkbox" value="pdf"> PDF (1:1 drawing)</label>
                    <!-- STL is always generated (it drives the 3D preview below), so it isn't
                         offered as a toggle - unchecking it would silently break the viewer. -->
                </div>
                <br>
                <button onclick="previewFree()" id="previewBtn" style="background:#6c757d;">Preview (Free, STL only)</button>
                <button onclick="generate()" id="generateBtn">Generate + Pay in BOT</button>
                <p id="costNote" style="font-size:0.85rem;color:#6c757d;margin-top:6px;"></p>
                <div style="margin-top:6px;">
                    <button onclick="handleBuyCreditsClick('single')" style="background:#e9ecef;color:#212529;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;font-size:0.8rem;margin-right:6px;">Buy 1 credit</button>
                    <button onclick="handleBuyCreditsClick('pack_1000')" style="background:#e9ecef;color:#212529;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;font-size:0.8rem;margin-right:6px;">Buy 1000 credits</button>
                    <button onclick="handleBuyCreditsClick('pack_10000')" style="background:#e9ecef;color:#212529;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;font-size:0.8rem;">Buy 10000 credits</button>
                </div>
                <p style="font-size:0.85rem;color:#6c757d;margin-top:6px;">Preview is free and unlimited detail-wise, but STL only and not exportable later - it's a fresh, separate job. Once you're happy with the description, use "Generate + Pay in BOT" for a version you can export to STEP/IGES/DXF/PDF.</p>
            </div>
            
            <div class="panel">
                <h2>3D Preview</h2>
                <div class="viewer" id="viewer">
                    <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: #6c757d;">
                        Generate a part to see 3D preview
                    </div>
                </div>
            </div>
        </div>
        
        <div class="panel" style="margin-top: 20px;">
            <h2>Results</h2>
            <div id="result" class="result"></div>
        </div>
        
        <script type="module">
            import * as THREE from "three";
            import { OrbitControls } from "three/addons/controls/OrbitControls.js";
            import { STLLoader } from "three/addons/loaders/STLLoader.js";

            let scene, camera, renderer, controls, currentMesh;
            let chainConfig = null;

            async function getChainConfig() {
                if (chainConfig) return chainConfig;
                const res = await fetch('/config/chain');
                chainConfig = await res.json();
                return chainConfig;
            }

            async function connectWallet() {
                if (!window.ethereum) {
                    throw new Error('No wallet found. Install MetaMask or a compatible wallet.');
                }
                const cfg = await getChainConfig();
                const [account] = await window.ethereum.request({ method: 'eth_requestAccounts' });

                try {
                    await window.ethereum.request({
                        method: 'wallet_switchEthereumChain',
                        params: [{ chainId: cfg.chain_id_hex }],
                    });
                } catch (switchError) {
                    // 4902 = chain not added to the wallet yet
                    if (switchError.code === 4902) {
                        await window.ethereum.request({
                            method: 'wallet_addEthereumChain',
                            params: [{
                                chainId: cfg.chain_id_hex,
                                chainName: cfg.chain_name,
                                nativeCurrency: { name: 'BOT', symbol: cfg.currency_symbol, decimals: 18 },
                                rpcUrls: [cfg.rpc_url],
                                blockExplorerUrls: [cfg.explorer_url],
                            }],
                        });
                    } else {
                        throw switchError;
                    }
                }
                return account;
            }

            // ------------------------------------------------- wallet sign-in ----
            // Sign-In-With-Wallet: connect + sign a free message (no gas,
            // nothing on-chain) to get a session, replacing the old "pay
            // 5 BOT for an API key cached in this browser" identity model.
            // Same-origin page, so no API base to key the cache by - one
            // flat localStorage key is enough, unlike frontend/index.html's
            // per-base version. See wallet_auth.py.
            function getWalletSession() {
                const raw = localStorage.getItem('nl_to_cad_wallet_session');
                if (!raw) return null;
                try {
                    const session = JSON.parse(raw);
                    if (new Date(session.expires_at) <= new Date()) {
                        localStorage.removeItem('nl_to_cad_wallet_session');
                        return null;
                    }
                    return session;
                } catch (err) {
                    return null;
                }
            }

            function setWalletSession(session) {
                localStorage.setItem('nl_to_cad_wallet_session', JSON.stringify(session));
            }

            function clearWalletSession() {
                localStorage.removeItem('nl_to_cad_wallet_session');
            }

            function updateWalletUI() {
                const session = getWalletSession();
                const btn = document.getElementById('walletBtn');
                if (!btn) return;
                btn.textContent = session
                    ? session.wallet_address.slice(0, 6) + '…' + session.wallet_address.slice(-4)
                    : 'Connect Wallet';
            }

            async function signInWithWallet() {
                const account = await connectWallet();
                const nonceResp = await fetch('/auth/nonce', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ wallet_address: account }),
                });
                const nonceData = await nonceResp.json();
                if (!nonceResp.ok) throw new Error(nonceData.detail || 'Could not start sign-in.');

                const signature = await window.ethereum.request({
                    method: 'personal_sign',
                    params: [nonceData.message, account],
                });

                const verifyResp = await fetch('/auth/verify', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ wallet_address: account, nonce: nonceData.nonce, signature }),
                });
                const session = await verifyResp.json();
                if (!verifyResp.ok) throw new Error(session.detail || 'Sign-in failed.');

                setWalletSession(session);
                updateWalletUI();
                return session;
            }

            async function signOutOfWallet() {
                const session = getWalletSession();
                clearWalletSession();
                updateWalletUI();
                if (session) {
                    try {
                        await fetch('/auth/logout', {
                            method: 'POST',
                            headers: { 'Authorization': 'Bearer ' + session.session_id },
                        });
                    } catch (err) { /* already signed out client-side regardless */ }
                }
            }

            async function handleWalletButtonClick() {
                const existing = getWalletSession();
                if (existing) {
                    if (window.confirm(`Connected as ${existing.wallet_address}. Disconnect?`)) {
                        await signOutOfWallet();
                    }
                    return;
                }
                try {
                    await signInWithWallet();
                } catch (err) {
                    alert('Wallet sign-in failed: ' + err.message);
                }
            }

            async function payBot(amountBot) {
                const cfg = await getChainConfig();
                const account = await connectWallet();
                const valueWei = BigInt(Math.round(amountBot * 1e18));

                const txHash = await window.ethereum.request({
                    method: 'eth_sendTransaction',
                    params: [{
                        from: account,
                        to: cfg.treasury_address,
                        value: '0x' + valueWei.toString(16),
                    }],
                });
                // txHash is returned the moment the user confirms in-wallet,
                // not once mined - the backend's own confirmation check
                // (botchain_pay.py) is what actually gates on it being real.
                return txHash;
            }

            // Replaces the old hasServiceKey/SERVICE_API_KEY/
            // ensureServiceKey model entirely - see the wallet sign-in
            // block above for identity, and db.py's wallet_credits
            // table for what "credits" means server-side.
            async function fetchCreditsBalance() {
                const session = getWalletSession();
                if (!session) return null;
                const resp = await fetch('/credits/balance', {
                    headers: { 'Authorization': 'Bearer ' + session.session_id },
                });
                if (!resp.ok) return null;
                const data = await resp.json();
                return data.balance;
            }

            // Keeps the visible cost note honest at every point: shown
            // before generating, reflects whichever state actually
            // applies right now - not signed in, signed in with
            // credits, or signed in with none.
            async function updateCostNote() {
                const el = document.getElementById('costNote');
                if (!el) return;
                try {
                    const cfg = await getChainConfig();
                    const session = getWalletSession();
                    if (!session) {
                        el.textContent = `Connect your wallet to generate. Each generation spends 1 credit (${cfg.credit_price_bot} BOT to buy one, or ${cfg.pack_1000_price_bot} BOT for ${cfg.pack_1000_credits}, ${cfg.pack_10000_price_bot} BOT for ${cfg.pack_10000_credits}).`;
                        return;
                    }
                    const balance = await fetchCreditsBalance();
                    if (balance === null) { el.textContent = ''; return; }
                    el.textContent = balance > 0
                        ? `You have ${balance} credit${balance === 1 ? '' : 's'}. Generating spends 1 - exports of that job are free after.`
                        : `You have 0 credits. Buy 1 for ${cfg.credit_price_bot} BOT, or a pack (${cfg.pack_1000_price_bot} BOT for ${cfg.pack_1000_credits}, ${cfg.pack_10000_price_bot} BOT for ${cfg.pack_10000_credits}) below.`;
                } catch (err) {
                    el.textContent = '';
                }
            }

            // Buys credits at one of the three tiers - pays on-chain,
            // then registers that payment against the signed-in
            // wallet's balance. Same call for all three; only the tier
            // and price differ.
            async function buyCredits(tier, priceBot) {
                const session = getWalletSession();
                if (!session) throw new Error('Connect your wallet first.');
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = `<div class="loading"><div class="spinner"></div><p>Waiting for payment: ${priceBot} BOT for credits...</p></div>`;
                const txHash = await payBot(priceBot);
                resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Confirming purchase...</p></div>';
                const resp = await fetch('/credits/purchase', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + session.session_id,
                    },
                    body: JSON.stringify({ tx_hash: txHash, tier }),
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Credit purchase failed.');
                updateCostNote();
                resultDiv.innerHTML = `<div class="loading"><p>Purchase complete - balance: ${data.balance} credits.</p></div>`;
                return data.balance;
            }

            async function handleBuyCreditsClick(tier) {
                try {
                    if (!getWalletSession()) await signInWithWallet();
                    const cfg = await getChainConfig();
                    const priceBot = tier === 'single' ? cfg.credit_price_bot
                        : tier === 'pack_1000' ? cfg.pack_1000_price_bot
                        : cfg.pack_10000_price_bot;
                    await buyCredits(tier, priceBot);
                } catch (err) {
                    alert('Purchase failed: ' + err.message);
                }
            }

            async function downloadFormat(fmt, jobId) {
                // Can't use a plain <a href>/window.open here - the export
                // route requires an Authorization: Bearer <session_id>
                // header (see wallet_auth.get_current_wallet), and neither
                // of those can set custom headers on a GET. fetch() + blob
                // is the correct way to download an authenticated file,
                // not a workaround.
                //
                // No payment here - the job's original /generate payment
                // already covers every export format pulled from it. See
                // /export/{fmt}/{job_id}'s docstring in web_app.py.
                const session = getWalletSession();
                if (!session) { alert('Your wallet session expired - reconnect and regenerate to download.'); return; }
                const btn = event.target;
                const originalText = btn.textContent;
                btn.disabled = true;
                btn.textContent = 'Building...';
                try {
                    const resp = await fetch(`/export/${fmt}/${jobId}`, {
                        headers: { 'Authorization': 'Bearer ' + session.session_id },
                    });
                    if (!resp.ok) {
                        const err = await resp.json().catch(() => ({}));
                        throw new Error(err.detail || `Export failed (${resp.status})`);
                    }
                    const blob = await resp.blob();
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    const cd = resp.headers.get('content-disposition') || '';
                    const match = cd.match(/filename="?([^"]+)"?/);
                    a.download = match ? match[1] : `${jobId}.${fmt}`;
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    URL.revokeObjectURL(url);
                } catch (err) {
                    alert('Download failed: ' + err.message);
                } finally {
                    btn.disabled = false;
                    btn.textContent = originalText;
                }
            }
            window.downloadFormat = downloadFormat;

            function initViewer() {
                const viewerDiv = document.getElementById('viewer');
                const width = viewerDiv.clientWidth;
                const height = viewerDiv.clientHeight;
                
                scene = new THREE.Scene();
                scene.background = new THREE.Color(0xe9ecef);
                
                camera = new THREE.PerspectiveCamera(75, width / height, 0.1, 1000);
                camera.position.set(50, 50, 50);
                
                renderer = new THREE.WebGLRenderer({ antialias: true });
                renderer.setSize(width, height);
                viewerDiv.innerHTML = '';
                viewerDiv.appendChild(renderer.domElement);
                
                controls = new OrbitControls(camera, renderer.domElement);
                controls.enableDamping = true;
                
                // Add lights
                const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
                scene.add(ambientLight);
                
                const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
                directionalLight.position.set(50, 50, 50);
                scene.add(directionalLight);
                
                // Add grid
                const gridHelper = new THREE.GridHelper(100, 10);
                scene.add(gridHelper);
                
                animate();
            }
            
            function animate() {
                requestAnimationFrame(animate);
                controls.update();
                renderer.render(scene, camera);
            }
            
            function loadSTL(url) {
                const loader = new STLLoader();
                loader.load(url, function(geometry) {
                    if (currentMesh) {
                        scene.remove(currentMesh);
                    }
                    
                    const material = new THREE.MeshPhongMaterial({
                        color: 0x007bff,
                        specular: 0x111111,
                        shininess: 200
                    });
                    
                    currentMesh = new THREE.Mesh(geometry, material);
                    
                    // Center and scale the mesh
                    geometry.computeBoundingBox();
                    geometry.center();
                    
                    const bbox = geometry.boundingBox;
                    const maxDim = Math.max(
                        bbox.max.x - bbox.min.x,
                        bbox.max.y - bbox.min.y,
                        bbox.max.z - bbox.min.z
                    );
                    
                    const scale = 80 / maxDim;
                    currentMesh.scale.set(scale, scale, scale);
                    
                    scene.add(currentMesh);
                    
                    // Reset camera
                    camera.position.set(50, 50, 50);
                    controls.target.set(0, 0, 0);
                    controls.update();
                });
            }
            
            function setExample(text) {
                document.getElementById('description').value = text;
            }
            
            function renderResultBase(data) {
                let html = '<div class="success">✓ Generated successfully!</div>';

                if (data.validation && data.validation.warnings.length > 0) {
                    html += '<div class="warning"><strong>Warnings:</strong><ul>';
                    data.validation.warnings.forEach(w => {
                        html += `<li>${w}</li>`;
                    });
                    html += '</ul></div>';
                }

                if (data.validation && Object.keys(data.validation.corrections).length > 0) {
                    html += '<div class="warning"><strong>Auto-corrections applied:</strong><ul>';
                    for (let [param, correction] of Object.entries(data.validation.corrections)) {
                        html += `<li>${param}: ${correction.old} → ${correction.new}</li>`;
                    }
                    html += '</ul></div>';
                }

                html += `<h3>Part Type: ${data.parameters.part_type}</h3>`;
                html += '<div class="param-grid">';
                for (let [key, value] of Object.entries(data.parameters.parameters)) {
                    if (key !== 'operations') {
                        html += `<div class="param-item"><strong>${key}:</strong> ${value}</div>`;
                    }
                }
                html += '</div>';

                if (data.parameters.material) {
                    html += `<p><strong>Material:</strong> ${data.parameters.material}</p>`;
                }
                return html;
            }

            async function previewFree() {
                const description = document.getElementById('description').value;
                if (!description.trim()) {
                    alert('Please enter a description');
                    return;
                }

                const resultDiv = document.getElementById('result');
                const previewBtn = document.getElementById('previewBtn');
                const generateBtn = document.getElementById('generateBtn');

                resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Generating free preview...</p></div>';
                previewBtn.disabled = true;
                generateBtn.disabled = true;

                try {
                    // No wallet, no API key, no payment - see POST /preview's
                    // own docstring for why (rate-limited by IP instead).
                    const response = await fetch('/preview', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ description: description })
                    });
                    const data = await response.json();

                    if (data.success) {
                        let html = renderResultBase(data);
                        html += '<div class="warning"><strong>This is a free STL-only preview.</strong> It isn\'t exportable to STEP/IGES/DXF/PDF later - use "Generate + Pay in BOT" above with the same description for a version you can export.</div>';
                        resultDiv.innerHTML = html;
                        loadSTL(`/download/stl/${data.stl_file.split('/').pop()}`);
                    } else {
                        resultDiv.innerHTML = `<div class="error"><strong>✗ Error:</strong> ${data.error}</div>`;
                    }
                } catch (error) {
                    resultDiv.innerHTML = `<div class="error"><strong>✗ Request failed:</strong> ${error.message}</div>`;
                } finally {
                    previewBtn.disabled = false;
                    generateBtn.disabled = false;
                }
            }

            async function generate() {
                const description = document.getElementById('description').value;
                const useDeepSeek = document.getElementById('useDeepSeek').checked;
                const apiKey = document.getElementById('apiKey').value;
                // formats checkboxes no longer control what /generate builds
                // (always STL-only now) - they control which download
                // buttons render after generation succeeds, see below.

                if (!description.trim()) {
                    alert('Please enter a description');
                    return;
                }

                const resultDiv = document.getElementById('result');
                const generateBtn = document.getElementById('generateBtn');
                const previewBtn = document.getElementById('previewBtn');

                let cfg;
                try {
                    cfg = await getChainConfig();
                } catch (err) {
                    resultDiv.innerHTML = `<div class="error"><strong>✗ Could not reach pricing info:</strong> ${err.message}</div>`;
                    return;
                }

                generateBtn.disabled = true;
                previewBtn.disabled = true;

                try {
                    // Step 1: identity. No API key anymore - a wallet
                    // session, signed for free, is the only thing
                    // /generate needs. See wallet_auth.py.
                    let session = getWalletSession();
                    if (!session) {
                        resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Connecting wallet...</p></div>';
                        session = await signInWithWallet();
                    }

                    // Step 2: credits. /generate spends exactly 1 - if
                    // the balance is 0, this is the one moment a payment
                    // might be needed, disclosed and confirmed before
                    // the wallet opens, not bundled silently in.
                    resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Checking credits...</p></div>';
                    let balance = await fetchCreditsBalance();
                    if (balance === 0) {
                        const buy = window.confirm(
                            `You have 0 credits. Buy 1 for ${cfg.credit_price_bot} BOT to generate this part?\n\n` +
                            `(For repeated use, ${cfg.pack_1000_price_bot} BOT gets you ${cfg.pack_1000_credits} credits, ` +
                            `${cfg.pack_10000_price_bot} BOT gets you ${cfg.pack_10000_credits} - see the credit buttons below.)`
                        );
                        if (!buy) {
                            resultDiv.innerHTML = '<p>Cancelled - no payment made.</p>';
                            generateBtn.disabled = false;
                            previewBtn.disabled = false;
                            return;
                        }
                        balance = await buyCredits('single', cfg.credit_price_bot);
                    }

                    // Step 3: generate. No tx_hash, no per-call wallet
                    // prompt here - the credit spend is server-side and
                    // atomic (db.consume_credit).
                    resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Generating CAD...</p></div>';
                    let response = await fetch('/generate', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'Authorization': 'Bearer ' + session.session_id,
                        },
                        body: JSON.stringify({
                            description: description,
                            use_deepseek: useDeepSeek,
                            api_key: apiKey,
                        })
                    });

                    if (response.status === 401) {
                        // Session expired/revoked server-side - free to
                        // fix, just sign in again, no payment involved.
                        clearWalletSession();
                        updateWalletUI();
                        throw new Error('Your wallet session expired - click Generate again to reconnect.');
                    }

                    const data = await response.json();
                    
                    if (data.success) {
                        let html = renderResultBase(data);
                        
                        // STL already exists on disk (it built the preview,
                        // and is served unauthenticated - see
                        // /download/stl/{filename}), so it's a direct link.
                        // Every other checked format is built on demand
                        // when its button is clicked, but not separately
                        // billed - this job's one credit already covers
                        // every format pulled from it; see downloadFormat()
                        // and /export/{fmt}/{job_id}.
                        const formatLabels = {
                            step: 'STEP (3D solid)',
                            iges: 'IGES (3D solid)',
                            dxf: 'DXF (2D cut layers)',
                            pdf: 'PDF (1:1 drawing)',
                        };
                        html += '<div class="download-links">';
                        const stlFilename = data.stl_file.split('/').pop();
                        html += `<a href="/download/stl/${stlFilename}" target="_blank">📥 Download STL (mesh)</a>`;
                        document.querySelectorAll('.fmt-checkbox').forEach(cb => {
                            if (cb.checked) {
                                const label = formatLabels[cb.value];
                                html += `<button onclick="downloadFormat('${cb.value}', '${data.job_id}')">📥 Download ${label} (included)</button>`;
                            }
                        });
                        html += '</div>';
                        
                        resultDiv.innerHTML = html;
                        
                        // Load STL in viewer
                        loadSTL(`/download/stl/${data.stl_file.split('/').pop()}`);
                        
                    } else {
                        resultDiv.innerHTML = `<div class="error"><strong>✗ Error:</strong> ${data.error}</div>`;
                    }
                } catch (error) {
                    resultDiv.innerHTML = `<div class="error"><strong>✗ Request failed:</strong> ${error.message}</div>`;
                } finally {
                    generateBtn.disabled = false;
                    previewBtn.disabled = false;
                    updateCostNote();
                }
            }
            
            // type="module" scripts don't leak declarations onto window,
            // so the inline onclick="generate()" / onclick="setExample(...)"
            // handlers in the HTML above need these attached explicitly.
            // (handleWalletButtonClick was missing this in an earlier pass -
            // its Connect Wallet button would have thrown "not defined" on
            // click, since inline onclick runs in global scope, not this
            // module's scope. Caught here before shipping, not after.)
            window.generate = generate;
            window.previewFree = previewFree;
            window.setExample = setExample;
            window.handleWalletButtonClick = handleWalletButtonClick;
            window.handleBuyCreditsClick = handleBuyCreditsClick;

            // Initialize viewer on load
            window.addEventListener('load', () => {
                initViewer();
                // NOTE: this used to call ensureServiceKey() here too,
                // which meant simply loading this page - no click, no
                // typed description - would try to auto-connect a
                // wallet and immediately request a real 5 BOT payment
                // if the visitor had no cached key yet. Wallet-connect-
                // then-charge on page load with zero user action is
                // also the exact behavioral signature wallet security
                // tools (e.g. MetaMask's Blockaid integration) look for
                // to flag drainer/scam sites - not a risk worth taking
                // on a legitimate page just to warm the cache a few
                // seconds early. Key issuance now only ever happens
                // from an explicit Generate click, same as the price
                // itself only ever being disclosed at that point.
                updateCostNote();
                // Safe to check here, unlike the old ensureServiceKey() -
                // this only reads a cached session and updates the button
                // label, no wallet popup, no request of any kind unless
                // the person clicks Connect Wallet themselves.
                updateWalletUI();

                // Handle window resize
                window.addEventListener('resize', () => {
                    const viewerDiv = document.getElementById('viewer');
                    const width = viewerDiv.clientWidth;
                    const height = viewerDiv.clientHeight;
                    camera.aspect = width / height;
                    camera.updateProjectionMatrix();
                    renderer.setSize(width, height);
                });
            });
        </script>
    </body>
    </html>
    """
    return html

@app.post("/preview", response_model=GenerateResponse)
@limiter.limit(settings.RATE_LIMIT_PREVIEW)
async def preview_cad(request: Request, body: PreviewRequest):
    """Free STL-only preview - no wallet, no signed-in session, no BOT
    payment. Lets someone evaluate NitoCAD before spending anything.
    Rate-limited hard per IP (RATE_LIMIT_PREVIEW, default 5/hour) since
    nothing else
    throttles this route the way a real BOT payment throttles /generate.

    Deliberately forces use_deepseek=False - the fallback regex parser,
    not DeepSeek. DeepSeek calls cost real money per call regardless of
    whether the caller pays BOT, and this route has no payment gate to
    recover that cost from; letting it call DeepSeek would make it a
    free way to burn this server's DeepSeek budget at scale.

    The resulting job is NOT later exportable via
    GET /export/{fmt}/{job_id} - its user_id is "anonymous", which will
    never match a real wallet address. This is deliberate, not a bug:
    making a preview "upgradeable" to a paid export would mean letting
    a signed-in wallet claim an existing job by its job_id, and nothing
    currently stops an anonymous job_id from being seen and claimed by
    someone other than whoever generated it. Liking a preview means
    re-submitting the same description through the paid /generate flow
    - a fresh job, not unlocking this one."""
    result = generator.generate_from_text(
        body.description,
        use_deepseek=False,
        user_id="anonymous",
        formats=["stl"],
    )
    return result


@app.post("/generate", response_model=GenerateResponse)
@limiter.limit(settings.RATE_LIMIT_GENERATE)
async def generate_cad(
    request: Request,
    body: GenerateRequest,
    wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)],
):
    """Generate CAD from description. Requires a signed-in wallet
    session (Authorization: Bearer <session_id> - see wallet_auth.py
    and POST /auth/verify) and spends exactly 1 credit from that
    wallet's balance (db.consume_credit). No API key, no per-call
    payment here anymore - top up credits via POST /credits/purchase.
    This is the only cost for the whole job - see
    GET /export/{fmt}/{job_id}'s docstring. Rate-limited per IP
    (RATE_LIMIT_GENERATE, default 20/minute) - each call does real
    CadQuery/OCCT work.

    Only STL is built here (the preview) - STEP/IGES/DXF/PDF are
    deferred to GET /export/{fmt}/{job_id}, built on demand, only for
    formats actually requested, but not billed again."""
    if not db.consume_credit(wallet["wallet_address"]):
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient credits (balance: {db.get_credit_balance(wallet['wallet_address'])}). "
                "Buy more via POST /credits/purchase."
            ),
        )

    result = generator.generate_from_text(
        body.description,
        use_deepseek=body.use_deepseek,
        api_key=body.api_key,
        model=body.model,
        user_id=wallet["wallet_address"],
        formats=["stl"],
    )
    return result


@app.get("/export/{fmt}/{job_id}")
@limiter.limit(settings.RATE_LIMIT_GENERATE)
async def export_on_demand(
    request: Request,
    fmt: str,
    job_id: str,
    wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)],
):
    """Build (or reuse a same-instance cached build of) exactly one
    export format for an already-generated job, on demand.

    No separate charge here, on purpose: the 1 credit spent at
    POST /generate time already covers this job in full, including
    every export format pulled from it - a job isn't billed per
    format, it's billed once, at generation. Ownership is enough to
    gate this: `job["user_id"] != wallet["wallet_address"]` below
    already proves the caller is the same wallet that spent the credit
    to create the job, so charging again would just be double-billing
    work already paid for once. (This used to require its own tx_hash
    and re-charge BOTCHAIN_PER_CALL_PRICE_BOT per format - that was the
    bug reported as "charged 0.2 BOT per format on top of the 0.2 BOT
    generation charge, for one job." Fixed here, not by discounting
    anything.)"""
    job = db.get_job(job_id)
    if job is None or job["user_id"] != wallet["wallet_address"]:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        file_path, _part_type = generator.export_format_for_job(job_id, fmt)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except GenerationError as exc:
        raise HTTPException(status_code=500, detail=exc.message) from exc

    media_types = {
        "step": "application/step", "stl": "model/stl", "iges": "model/iges",
        "dxf": "image/vnd.dxf", "pdf": "application/pdf",
    }
    return FileResponse(file_path, media_type=media_types[fmt], filename=file_path.name)


@app.get("/api/jobs")
async def list_jobs(wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)]):
    """Audit history for the signed-in wallet - every job it has run.
    This is also the backend a future profile page reads from; nothing
    more to build here for that beyond the frontend itself."""
    return db.list_jobs(user_id=wallet["wallet_address"])


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)]):
    """
    Look up one job by id. Generation here is synchronous (1-3s, no
    Celery queue - see README), so this isn't a poll-for-completion
    endpoint like Stitchfren's /api/status/{task_id}. It exists so the
    mcp-gateway (or any agent) can re-fetch a completed job's download
    links later without re-running generation.
    """
    job = db.get_job(job_id)
    if job is None or job["user_id"] != wallet["wallet_address"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/download/step/{filename}")
async def download_step(filename: str):
    """Download STEP file. `filename` is resolved through
    file_safety.safe_output_path so a path-traversal attempt (e.g.
    `..%2F..%2Fetc%2Fpasswd`) gets a clean 400 instead of walking outside
    the output directory."""
    file_path = safe_output_path(generator.output_dir, filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="application/step", filename=file_path.name)

@app.get("/download/stl/{filename}")
async def download_stl(filename: str):
    """Download STL file. See download_step's docstring re: path safety."""
    file_path = safe_output_path(generator.output_dir, filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="model/stl", filename=file_path.name)


@app.get("/download/iges/{filename}")
async def download_iges(filename: str):
    """Download IGES file. See download_step's docstring re: path safety."""
    file_path = safe_output_path(generator.output_dir, filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="model/iges", filename=file_path.name)


@app.get("/download/dxf/{filename}")
async def download_dxf(filename: str):
    """Download multi-view orthographic DXF file (front/top/side). See
    download_step's docstring re: path safety."""
    file_path = safe_output_path(generator.output_dir, filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="image/vnd.dxf", filename=file_path.name)


@app.get("/download/pdf/{filename}")
async def download_pdf(filename: str):
    """Download 1:1 scale vector PDF technical drawing. See
    download_step's docstring re: path safety."""
    file_path = safe_output_path(generator.output_dir, filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="application/pdf", filename=file_path.name)


@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
async def healthz():
    """Liveness probe - answers as soon as the process is up, with no
    dependency checks. Used by Railway/Docker HEALTHCHECK and load
    balancers to decide "is this instance alive at all"."""
    return HealthResponse(status="ok", version=app.version, environment=settings.ENVIRONMENT)


@app.get("/readyz", response_model=ReadinessResponse, tags=["ops"])
async def readyz():
    """Readiness probe - actually exercises the dependencies this service
    needs to serve real traffic (currently: the sqlite connection).
    Returns 200 with each check's result even on partial failure, so a
    caller can see *which* dependency is down rather than just "not
    ready"; orchestrators that want a hard fail on any false value can
    check the JSON body themselves."""
    checks = {"database": db.check_connection()}
    status_code = 200 if all(checks.values()) else 503
    return JSONResponse(
        status_code=status_code,
        content=ReadinessResponse(status="ready" if all(checks.values()) else "degraded", checks=checks).model_dump(),
    )


if __name__ == "__main__":
    logger.info("starting server at http://%s:%s", settings.HOST, settings.PORT)
    uvicorn.run(
        "web_app:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=not settings.is_production,
    )
