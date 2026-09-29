"""
Enhanced web interface with 3D preview.
"""
import hashlib
import hmac
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import db
from a2mcp.server import mcp_app, mcp_app_gated
from a2mcp_botchain.server import mcp_app as mcp_app_botchain
import botchain_pay
import paystack_pay
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
    # Without this a cross-origin frontend (Vercel -> Railway) cannot read
    # these response headers via fetch(), so filenames / anchor info are
    # invisible to it even though the browser received them.
    expose_headers=[
        "Content-Disposition",
        "X-NitoCAD-Anchor-Tx",
        "X-NitoCAD-Explorer-Url",
    ],
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


class GoogleAuthRequest(BaseModel):
    id_token: str  # the credential Google Identity Services hands the browser on sign-in


@app.post("/auth/google")
@limiter.limit("20/minute")
async def auth_google(request: Request, body: GoogleAuthRequest):
    """The Google-identity counterpart to POST /auth/verify - one step,
    not two, since Google's own ID token already IS the proof (no
    nonce/challenge needed the way a wallet signature needs one).
    Returns the same {session_id, wallet_address, expires_at} shape
    plus "email", so the frontend's existing wallet-session storage/
    Authorization-header code works completely unchanged - see
    wallet_auth.verify_google_and_create_session's docstring."""
    return wallet_auth.verify_google_and_create_session(body.id_token)


@app.get("/config/auth")
async def config_auth():
    """Public, no wallet needed. GOOGLE_OAUTH_CLIENT_ID is not a secret
    (Google Identity Services needs it client-side to even render the
    Sign-In button) - exposed here so it's never hardcoded into the
    static frontend file, same reasoning as GET /config/chain and
    GET /subscribe/tiers above."""
    return {
        "google_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "google_signin_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
    }


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
    subscribed = db.is_subscription_entitled(wallet["wallet_address"])
    return {
        "wallet_address": wallet["wallet_address"],
        "balance": balance,
        "subscribed": subscribed,
        # Convenience flag so the frontend doesn't need to know
        # BULK_TIER_CREDIT_THRESHOLD itself to decide whether to show
        # the prompt-refinement chat entry point - it's also exposed
        # directly via GET /config/chain for anywhere that needs the
        # raw number (e.g. an upsell message before reaching it).
        # An active subscriber gets this regardless of purchased
        # balance - additive to the existing threshold, not a
        # replacement for it, so a high-balance pay-as-you-go wallet
        # keeps the access it already had.
        "prompt_refine_eligible": subscribed or balance >= settings.BULK_TIER_CREDIT_THRESHOLD,
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
    bonus_granted = db.claim_signup_bonus(wallet["wallet_address"], qualifying_payment_id=body.tx_hash)
    if bonus_granted:
        new_balance = db.get_credit_balance(wallet["wallet_address"])
    logger.info(
        "credits purchased",
        extra={
            "wallet_address": wallet["wallet_address"],
            "tier": body.tier,
            "credits_granted": credits_granted,
            "signup_bonus_granted": bonus_granted,
        },
    )
    return {
        "wallet_address": wallet["wallet_address"],
        "credits_granted": credits_granted,
        "balance": new_balance,
        "signup_bonus_granted": bonus_granted,
    }


class SubscribeCheckoutRequest(BaseModel):
    tier: str  # "starter" | "engineer" | "professional" - see config.py's settings.subscription_tiers
    email: str  # required by Paystack - a real address, never synthesized, see paystack_pay.py's module docstring
    callback_url: str  # where Paystack redirects the browser after payment


@app.get("/subscribe/tiers")
async def subscribe_tiers():
    """Public, no wallet needed - the frontend's pricing page reads this
    live instead of hardcoding caps/credits (see frontend/index.html's
    earlier pricing-copy drift bug, which is exactly the failure mode
    this avoids repeating for the subscription tiers)."""
    return {
        name: {k: v for k, v in cfg.items() if k not in ("stripe_price_id", "paystack_plan_code")}
        for name, cfg in settings.subscription_tiers.items()
    }


@app.post("/subscribe/checkout")
@limiter.limit(settings.RATE_LIMIT_GENERATE)
async def subscribe_checkout(
    request: Request,
    body: SubscribeCheckoutRequest,
    wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)],
):
    """Creates a Paystack transaction for a NEW subscription at body.tier
    and returns its authorization_url for the frontend to redirect to.
    body.email is REQUIRED and must be the user's real address - see
    paystack_pay.py's module docstring on why this app never synthesizes
    one: Paystack needs a working email for receipts/payment
    notifications, and it's the reconciliation trail back to this
    wallet (via db.get_subscription_by_paystack_customer_code) if
    support ever needs it. The wallet stays the actual identity
    throughout - see config.py's Paystack comment block."""
    if db.is_subscription_entitled(wallet["wallet_address"]):
        raise HTTPException(
            status_code=409,
            detail="This wallet already has an active subscription.",
        )
    try:
        checkout_url = paystack_pay.initialize_subscription(
            wallet["wallet_address"], body.tier, body.email, body.callback_url
        )
    except PaymentError as exc:
        raise HTTPException(status_code=402, detail=exc.message) from exc
    return {"checkout_url": checkout_url}


@app.get("/subscription/status")
async def subscription_status(wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)]):
    sub = db.get_subscription(wallet["wallet_address"])
    entitled = db.is_subscription_entitled(wallet["wallet_address"])
    tier_config = settings.subscription_tiers.get(sub["tier"]) if (sub and entitled) else None
    return {
        "wallet_address": wallet["wallet_address"],
        "subscribed": entitled,
        "tier": sub["tier"] if sub else None,
        "status": sub["status"] if sub else None,
        "subscription_credits": sub["subscription_credits"] if sub else 0,
        "current_period_end": sub["current_period_end"] if sub else None,
        # Only meaningful (and only computed) for an entitled
        # subscription - see db.charge_generation for how this cap
        # interacts with subscription_credits above (Starter: both
        # apply; Engineer/Professional: this is the only limit).
        "daily_generation_cap": tier_config["daily_generation_cap"] if tier_config else None,
        "generations_last_24h": db.count_generations_last_24h(wallet["wallet_address"]) if tier_config else None,
    }


@app.post("/webhooks/paystack")
async def webhooks_paystack(request: Request):
    """No wallet_auth here - this is called by Paystack's own servers,
    not a signed-in user. Authenticity comes entirely from the
    signature check inside paystack_pay.handle_webhook_event
    (HMAC-SHA512 against PAYSTACK_SECRET_KEY - Paystack uses the same
    secret key for this and for API calls, there's no separate webhook
    secret to configure, see paystack_pay.py's module docstring). The
    raw body bytes are required for that check to pass - do NOT parse
    this as JSON first and re-serialize it, Paystack signs the exact
    bytes it sent. Register this URL (https://<your-domain>/webhooks/paystack)
    under Paystack Dashboard -> Settings -> API Keys & Webhooks."""
    payload = await request.body()
    sig_header = request.headers.get("x-paystack-signature", "")
    try:
        result = paystack_pay.handle_webhook_event(payload, sig_header)
    except PaymentError as exc:
        # 400, not 402 - tells Paystack "retry me" (bad signature, or an
        # unlinked customer whose linking webhook hasn't arrived yet -
        # see paystack_pay._on_charge_success), not "payment failed."
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"received": True, "result": result}


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

              // ===== Mobile-safe downloads (same helper on every page) =====
              // Why: fetch()->blob-><a download>->revokeObjectURL() works on desktop but
              // fails on phones (iOS Safari ignores `download` on blob: URLs, and the
              // revoke cancels the save; after an await the tap gesture is gone, so
              // share/download can silently no-op). Fix: the API hands back a short-lived
              // signed URL served with Content-Disposition: attachment - a plain
              // navigation every mobile browser can handle - and a "Tap to save" bar
              // (a fresh, real tap) is shown as a backup on phones / in-app browsers.
              const NITO_IS_MOBILE = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) ||
                (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
              const NITO_IN_APP = /FBAN|FBAV|Instagram|Twitter|Telegram|MicroMessenger|TikTok|[;] wv[)]/i.test(navigator.userAgent);
              let _nitoBarTimer = null, _nitoBarRevoke = null;

              function nitoHideSaveBar(){
                const el = document.getElementById('nitoSaveBar');
                if (el) el.remove();
                clearTimeout(_nitoBarTimer);
                if (_nitoBarRevoke) { const u = _nitoBarRevoke; _nitoBarRevoke = null; setTimeout(() => URL.revokeObjectURL(u), 60000); }
              }

              function nitoShowSaveBar(url, filename, isBlob){
                nitoHideSaveBar();
                const bar = document.createElement('div');
                bar.id = 'nitoSaveBar';
                bar.style.cssText = 'position:fixed;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom,0px));z-index:99999;background:#111827;color:#fff;border-radius:14px;padding:12px 14px;display:flex;align-items:center;gap:10px;box-shadow:0 8px 30px rgba(0,0,0,.35);font:14px/1.3 system-ui,-apple-system,sans-serif;';
                const txt = document.createElement('div');
                txt.style.cssText = 'flex:1;min-width:0;word-break:break-all;';
                txt.textContent = NITO_IN_APP
                  ? 'Not saving? Open this page in Safari/Chrome. ' + filename
                  : (isBlob ? 'Ready: ' : 'Download started. Not saving? ') + filename;
                const a = document.createElement('a');
                a.href = url;
                a.textContent = 'Tap to save';
                a.setAttribute('download', filename);
                a.rel = 'noopener';
                a.style.cssText = 'background:#13A57D;color:#fff;padding:9px 14px;border-radius:999px;font-weight:600;text-decoration:none;white-space:nowrap;';
                a.addEventListener('click', () => setTimeout(nitoHideSaveBar, 1500));
                const x = document.createElement('button');
                x.type = 'button';
                x.textContent = '\u2715';
                x.setAttribute('aria-label', 'Dismiss');
                x.style.cssText = 'background:none;border:0;color:#9ca3af;font-size:18px;padding:4px 6px;';
                x.addEventListener('click', nitoHideSaveBar);
                bar.append(txt, a, x);
                document.body.appendChild(bar);
                if (isBlob) _nitoBarRevoke = url;
                _nitoBarTimer = setTimeout(nitoHideSaveBar, isBlob ? 300000 : 25000);
              }

              // Preferred path: the API returned a signed, short-lived URL that responds
              // with Content-Disposition: attachment. Navigating to it downloads the file
              // without leaving the page, on desktop and mobile alike.
              function nitoDeliver(url, filename){
                const a = document.createElement('a');
                a.href = url;
                a.rel = 'noopener';
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                setTimeout(() => a.remove(), 1000);
                if (NITO_IS_MOBILE || NITO_IN_APP) nitoShowSaveBar(url, filename, false);
              }

              // Fallback for files we only have as a Blob (e.g. an owned STL fetched by
              // filename). Mobile: Web Share (files) -> else "Tap to save" bar with a
              // fresh gesture. Desktop: classic <a download> with a DELAYED revoke.
              async function nitoBlobDownload(blob, filename){
                if (NITO_IS_MOBILE && navigator.canShare && navigator.share) {
                  try {
                    const file = new File([blob], filename, { type: blob.type || 'application/octet-stream' });
                    if (navigator.canShare({ files: [file] })) {
                      await navigator.share({ files: [file], title: filename });
                      return;
                    }
                  } catch (e) {
                    if (e && e.name === 'AbortError') return;   // user closed the share sheet
                    // gesture expired / type not shareable -> fall through to the save bar
                  }
                }
                const url = URL.createObjectURL(blob);
                if (NITO_IS_MOBILE || NITO_IN_APP) { nitoShowSaveBar(url, filename, true); return; }
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                setTimeout(() => URL.revokeObjectURL(url), 60000);
              }
              // ===== end mobile-safe downloads =====

            async function downloadFormat(fmt, jobId) {
                // The export route needs an Authorization header, which a
                // plain <a href> can't send - so: authenticated POST builds
                // the file and returns a short-lived signed URL, then we
                // navigate to it (Content-Disposition: attachment). That
                // is what makes downloads work on phone browsers; the old
                // fetch()->blob->revokeObjectURL() path only worked on
                // desktop. See POST /export/{fmt}/{job_id}/link.
                //
                // No payment here - the job's original /generate payment
                // already covers every export format pulled from it.
                const session = getWalletSession();
                if (!session) { alert('Your wallet session expired - reconnect and regenerate to download.'); return; }
                const btn = event.target;
                const originalText = btn.textContent;
                btn.disabled = true;
                btn.textContent = 'Building...';
                try {
                    const resp = await fetch(`/export/${fmt}/${jobId}/link`, {
                        method: 'POST',
                        headers: { 'Authorization': 'Bearer ' + session.session_id },
                    });
                    if (!resp.ok) {
                        const err = await resp.json().catch(() => ({}));
                        throw new Error(err.detail || `Export failed (${resp.status})`);
                    }
                    const info = await resp.json();
                    nitoDeliver(info.url, info.filename || `${jobId}.${fmt}`);
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
            
            async function loadOwnedStl(url) {
                // /download/{fmt}/{filename} is now ownership-gated for
                // everything except anonymous /preview output (see
                // db.find_job_owner and web_app.py's _resolve_owned_download).
                // A bare STLLoader.load(url) or <a href> can't attach the
                // Authorization header a paid job's file now needs - same
                // reason downloadFormat() below already has to fetch()+blob
                // instead of a direct link. The free-preview call site keeps
                // calling loadSTL(url) directly below, since those files stay
                // public and need no session at all.
                const session = getWalletSession();
                const headers = session ? { 'Authorization': 'Bearer ' + session.session_id } : {};
                const resp = await fetch(url, { headers });
                if (!resp.ok) {
                    throw new Error(`Could not load ${url} (${resp.status})`);
                }
                return resp.blob();
            }

            async function downloadStl(filename) {
                try {
                    const blob = await loadOwnedStl(`/download/stl/${filename}`);
                    await nitoBlobDownload(blob, filename);
                } catch (err) {
                    alert('Download failed: ' + err.message);
                }
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
                        loadSTL(data.job_id ? `/download/job/${data.job_id}/stl` : `/download/stl/${data.stl_file.split('/').pop()}`);
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
                        html += data.job_id
                            ? `<button onclick="downloadFormat('stl','${data.job_id}')">📥 Download STL (mesh)</button>`
                            : `<button onclick="downloadStl('${stlFilename}')">📥 Download STL (mesh)</button>`;
                        document.querySelectorAll('.fmt-checkbox').forEach(cb => {
                            if (cb.checked) {
                                const label = formatLabels[cb.value];
                                html += `<button onclick="downloadFormat('${cb.value}', '${data.job_id}')">📥 Download ${label} (included)</button>`;
                            }
                        });
                        html += '</div>';
                        
                        resultDiv.innerHTML = html;
                        
                        // Load STL in viewer - paid job, needs the wallet
                        // session's Authorization header now (loadOwnedStl),
                        // unlike previewFree()'s call above which stays a
                        // bare loadSTL() since anonymous preview files are
                        // still public.
                        loadOwnedStl(data.job_id ? `/download/job/${data.job_id}/stl` : `/download/stl/${data.stl_file.split('/').pop()}`)
                            .then(blob => loadSTL(URL.createObjectURL(blob)))
                            .catch(err => { resultDiv.innerHTML += `<div class="error"><strong>✗ Viewer failed:</strong> ${err.message}</div>`; });
                        
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
    and POST /auth/verify) and spends exactly 1 generation, paid for by
    whichever mechanism applies first (db.charge_generation): an
    active subscription's daily allowance or credit pool, falling back
    to purchased credits. A subscriber who exceeds their daily cap (or,
    for Starter, their monthly pool) falls back to spending their own
    purchased credits rather than being hard-blocked until the
    window/period resets - a deliberate choice, not an oversight: it
    means a paying subscriber's work never just stops mid-day for lack
    of an allowance if they're willing to spend a credit, at the cost
    of the daily cap not being a hard ceiling for wallets that also
    hold purchased credits. No API key, no per-call payment here
    anymore - top up credits via POST /credits/purchase, or subscribe
    via POST /subscribe/checkout. This is the only cost for the whole
    job - see GET /export/{fmt}/{job_id}'s docstring. Rate-limited per
    IP (RATE_LIMIT_GENERATE, default 20/minute) - each call does real
    CadQuery/OCCT work.

    Only STL is built here (the preview) - STEP/IGES/DXF/PDF are
    deferred to GET /export/{fmt}/{job_id}, built on demand, only for
    formats actually requested, but not billed again."""
    charged_pool = db.charge_generation(wallet["wallet_address"])
    if charged_pool is None:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient credits (balance: {db.get_credit_balance(wallet['wallet_address'])}). "
                "Buy more via POST /credits/purchase, or subscribe via POST /subscribe/checkout."
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


_EXPORT_MEDIA_TYPES = {
    "step": "application/step", "stl": "model/stl", "iges": "model/iges",
    "dxf": "image/vnd.dxf", "pdf": "application/pdf",
}

# Phones (iOS Safari especially) save attachments reliably when the response
# is a plain navigation with Content-Disposition: attachment, and struggle
# with fetch()->blob-><a download>. But the export route needs an
# Authorization header, which a navigation can't send. So the frontend calls
# POST /export/{fmt}/{job_id}/link (authenticated) and gets back a short-lived
# HMAC-signed URL for GET /export/{fmt}/{job_id}/file, which it simply opens.
_DL_LINK_TTL_SECONDS = 300
# Set DOWNLOAD_LINK_SECRET in the environment if you run more than one
# process/replica (or want links to survive a restart); otherwise a random
# per-process key is used, which is fine for a single Railway instance since
# links only live 5 minutes.
_DL_LINK_SECRET = (
    (getattr(settings, "DOWNLOAD_LINK_SECRET", None) or "").encode()
    or secrets.token_bytes(32)
)


def _dl_signature(job_id: str, fmt: str, owner: str, exp: int) -> str:
    msg = f"{job_id}|{fmt}|{owner}|{exp}".encode()
    return hmac.new(_DL_LINK_SECRET, msg, hashlib.sha256).hexdigest()


def _make_dl_token(job_id: str, fmt: str, owner: str) -> str:
    exp = int(time.time()) + _DL_LINK_TTL_SECONDS
    return f"{exp}.{_dl_signature(job_id, fmt, owner, exp)}"


def _verify_dl_token(job_id: str, fmt: str, owner: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sig, _dl_signature(job_id, fmt, owner, exp))


def _anchor_info(job: dict) -> tuple[str | None, str | None]:
    anchor_tx = job.get("anchor_tx")
    if not anchor_tx:
        return None, None
    if not str(anchor_tx).startswith("0x"):
        anchor_tx = "0x" + str(anchor_tx)
    explorer = settings.botchain_explorer_url.rstrip("/")
    return str(anchor_tx), f"{explorer}/tx/{anchor_tx}"


def _build_export(fmt: str, job_id: str, owner_wallet: str | None):
    """Shared by every export route: ownership check (skipped when
    owner_wallet is None because a signed token already proved it), build
    one format on demand, return (file_path, job_after)."""
    job = db.get_job(job_id)
    if job is None or (owner_wallet is not None and job["user_id"] != owner_wallet):
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        file_path, _part_type = generator.export_format_for_job(job_id, fmt)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except GenerationError as exc:
        raise HTTPException(status_code=500, detail=exc.message) from exc
    # Re-read so a freshly written anchor_tx (STEP path only) is visible.
    return file_path, (db.get_job(job_id) or job)


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
    gate this. (This used to re-charge per format on top of the
    generation charge - that was the reported "double charge" bug.)

    Kept for API/agent callers that can send an Authorization header.
    Browsers - especially phones - should use POST .../link instead."""
    file_path, job_after = _build_export(fmt, job_id, wallet["wallet_address"])
    headers = {}
    anchor_tx, explorer_url = _anchor_info(job_after)
    if anchor_tx:
        headers["X-NitoCAD-Anchor-Tx"] = anchor_tx
        headers["X-NitoCAD-Explorer-Url"] = explorer_url
    return FileResponse(
        file_path,
        media_type=_EXPORT_MEDIA_TYPES.get(fmt, "application/octet-stream"),
        filename=file_path.name,  # => Content-Disposition: attachment
        headers=headers,
    )


@app.post("/export/{fmt}/{job_id}/link")
@limiter.limit(settings.RATE_LIMIT_GENERATE)
async def export_link(
    request: Request,
    fmt: str,
    job_id: str,
    wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)],
):
    """Authenticated: build the export, then return a short-lived signed
    URL the browser can simply navigate to (no Authorization header
    needed, so it works as a plain download on iOS Safari / Android
    Chrome). Ownership is checked here; the token binds job + format +
    owner and expires after _DL_LINK_TTL_SECONDS."""
    file_path, job_after = _build_export(fmt, job_id, wallet["wallet_address"])
    token = _make_dl_token(job_id, fmt, wallet["wallet_address"])
    anchor_tx, explorer_url = _anchor_info(job_after)
    return {
        "url": f"/export/{fmt}/{job_id}/file?t={token}",
        "filename": file_path.name,
        "expires_in": _DL_LINK_TTL_SECONDS,
        "anchor_tx": anchor_tx,
        "explorer_url": explorer_url,
    }


@app.get("/export/{fmt}/{job_id}/file")
@limiter.limit(settings.RATE_LIMIT_GENERATE)
async def export_file(request: Request, fmt: str, job_id: str, t: str = ""):
    """Serve a file for a valid signed token from POST .../link. Always
    Content-Disposition: attachment, generic binary type so mobile
    browsers download instead of trying to render STEP/DXF inline."""
    job = db.get_job(job_id)
    # 404 (not 401/403) for every failure, same as the other export routes,
    # so this can't be used to probe which job ids exist.
    if job is None or fmt not in _EXPORT_MEDIA_TYPES or not _verify_dl_token(
        job_id, fmt, job["user_id"], t
    ):
        raise HTTPException(status_code=404, detail="Link expired or invalid")
    file_path, _ = _build_export(fmt, job_id, None)
    media = "application/pdf" if fmt == "pdf" else "application/octet-stream"
    return FileResponse(
        file_path,
        media_type=media,
        filename=file_path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/jobs/{job_id}/provenance")
async def job_provenance(
    job_id: str,
    wallet: Annotated[dict, Depends(wallet_auth.get_current_wallet)],
):
    """Customer-facing on-chain proof for one job.

    Returns the DesignRegistry anchor transaction (if STEP was exported
    and anchoring succeeded), plus a direct explorer URL and the
    parameters that were hashed on-chain. Wallet/email session must own
    the job - same gate as GET /api/jobs/{job_id}.
    """
    job = db.get_job(job_id)
    if job is None or job["user_id"] != wallet["wallet_address"]:
        raise HTTPException(status_code=404, detail="Job not found")

    anchor_tx = job.get("anchor_tx")
    explorer = settings.botchain_explorer_url.rstrip("/")
    explorer_tx = None
    if anchor_tx:
        if not str(anchor_tx).startswith("0x"):
            anchor_tx = "0x" + str(anchor_tx)
        explorer_tx = f"{explorer}/tx/{anchor_tx}"

    params = job.get("parameters")
    if isinstance(params, str):
        try:
            import json as _json
            params = _json.loads(params)
        except Exception:
            pass

    return {
        "job_id": job_id,
        "part_type": job.get("part_type"),
        "parameters": params,
        "anchored": bool(anchor_tx),
        "anchor_tx": anchor_tx,
        "explorer_tx_url": explorer_tx,
        "registry_address": settings.DESIGN_REGISTRY_ADDRESS,
        "explorer_registry_url": (
            f"{explorer}/address/{settings.DESIGN_REGISTRY_ADDRESS}"
            if settings.DESIGN_REGISTRY_ADDRESS
            else None
        ),
        "chain": {
            "name": (
                "BOT Chain Testnet"
                if settings.BOTCHAIN_ENVIRONMENT == "testnet"
                else "BOT Chain"
            ),
            "chain_id_hex": settings.botchain_chain_id_hex,
            "explorer_url": explorer,
        },
        "note": (
            "On-chain provenance is written when you export STEP for a paid job. "
            "The record includes part type, parameter hash, template version, "
            "and keccak256 of the STEP file. Full parameters are in the event log."
            if anchor_tx
            else (
                "Not anchored yet. Generate a paid job, then export STEP. "
                "Free STL preview never writes on-chain."
            )
        ),
    }


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

def _stream_from_url(url: str, media_type: str, filename: str) -> StreamingResponse:
    """Proxy a remote object (typically an R2 presigned URL) through this
    API so the browser never has to talk to R2 directly.

    R2 buckets shared with other projects often lack a CORS rule for this
    frontend origin. THREE.STLLoader fetches via XHR, which is subject to
    CORS, so a direct load of the R2 URL fails even though a plain <a href>
    download of the same URL works. Streaming through this backend (which
    already allows CORS_ORIGINS=*) fixes the 3D preview without requiring
    bucket-level CORS changes.
    """
    import httpx

    try:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
        upstream = client.send(
            client.build_request("GET", url), stream=True
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Upstream fetch failed: {exc}"
        ) from exc

    if upstream.status_code != 200:
        upstream.close()
        client.close()
        raise HTTPException(
            status_code=404,
            detail=f"Remote file unavailable (HTTP {upstream.status_code})",
        )

    def iter_bytes():
        try:
            for chunk in upstream.iter_bytes():
                yield chunk
        finally:
            upstream.close()
            client.close()

    return StreamingResponse(
        iter_bytes(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
        },
    )


def _resolve_owned_download(
    fmt: str, filename: str, wallet: dict | None, media_type: str
):
    """Shared body for the five GET /download/{fmt}/{filename} routes.

    These used to be open to anyone who had the URL (see git history
    on file_safety.py - that module only ever guaranteed the path
    can't escape output_dir, it never checked WHO the file belongs to).
    For a job's own paid CAD artifacts that's a real gap: a leaked
    link (browser history, a referrer header, a screen share) handed
    out permanent, un-revocable access with no session needed at all.

    /preview output is the deliberate exception, not an oversight: it
    has no wallet to check ownership against in the first place (see
    web_app.py's preview_cad - user_id is the literal string
    "anonymous"), and it's free, rate-limited, and regenerable, so
    there's nothing sensitive to protect there. Every other job's
    files now require the requesting wallet to match db.find_job_owner
    - same 404-not-403 pattern GET /export/{fmt}/{job_id} already uses
    below, so this can't be used to distinguish "wrong owner" from
    "doesn't exist" either.

    When the local file is gone (Railway ephemeral disk after restart,
    or multi-replica where the generating instance is not the one
    serving the download) but the job still has a durable R2 URL, we
    proxy that URL so the 3D viewer keeps working.
    """
    file_path = safe_output_path(generator.output_dir, filename)
    owner = db.find_job_owner(filename)

    if file_path.exists():
        if owner is not None and owner != "anonymous":
            if wallet is None or owner != wallet["wallet_address"]:
                raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(file_path, media_type=media_type, filename=file_path.name)

    # Local miss → try R2 via the job record. find_job_by_local_filename
    # matches the local stem against jobs.parameters / stored URLs.
    remote_url = db.find_remote_url_for_filename(filename, fmt)
    if remote_url and remote_url.startswith("http"):
        if owner is not None and owner != "anonymous":
            if wallet is None or owner != wallet["wallet_address"]:
                raise HTTPException(status_code=404, detail="File not found")
        return _stream_from_url(remote_url, media_type, filename)

    raise HTTPException(status_code=404, detail="File not found")


@app.get("/download/job/{job_id}/{fmt}")
async def download_job_format(
    job_id: str,
    fmt: str,
    wallet: Annotated[dict | None, Depends(wallet_auth.get_current_wallet_optional)],
):
    """Serve a job's CAD artifact by job_id.

    Prefer local disk; fall back to proxying the durable R2 URL stored on
    the job row. Used by the 3D preview viewer so it never has to fetch
    R2 cross-origin (R2 CORS is not configured for this frontend).
    """
    media_types = {
        "step": "application/step",
        "stl": "model/stl",
        "iges": "model/iges",
        "dxf": "image/vnd.dxf",
        "pdf": "application/pdf",
    }
    if fmt not in media_types:
        raise HTTPException(status_code=422, detail=f"Unsupported format: {fmt}")

    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    owner = job.get("user_id") or "anonymous"
    if owner != "anonymous":
        if wallet is None or owner != wallet["wallet_address"]:
            raise HTTPException(status_code=404, detail="Job not found")

    # Prefer durable R2 URL on the job row (local disk is ephemeral on Railway).
    url_col = f"{fmt}_url"
    remote_url = job.get(url_col)

    if remote_url:
        if remote_url.startswith("/download/"):
            # Local-only fallback path stored when R2 wasn't configured
            filename = remote_url.rstrip("/").split("/")[-1]
            file_path = safe_output_path(generator.output_dir, filename)
            if file_path.exists():
                return FileResponse(
                    file_path, media_type=media_types[fmt], filename=file_path.name
                )
            raise HTTPException(status_code=404, detail="File not found")
        if remote_url.startswith("http"):
            filename = remote_url.split("?")[0].rstrip("/").split("/")[-1] or f"{job_id}.{fmt}"
            return _stream_from_url(remote_url, media_types[fmt], filename)

    raise HTTPException(status_code=404, detail="File not found")


@app.get("/download/step/{filename}")
async def download_step(
    filename: str,
    wallet: Annotated[dict | None, Depends(wallet_auth.get_current_wallet_optional)],
):
    """Download STEP file. See _resolve_owned_download's docstring for
    the path-safety (file_safety.safe_output_path) and ownership rules."""
    return _resolve_owned_download("step", filename, wallet, "application/step")

@app.get("/download/stl/{filename}")
async def download_stl(
    filename: str,
    wallet: Annotated[dict | None, Depends(wallet_auth.get_current_wallet_optional)],
):
    """Download STL file. See _resolve_owned_download's docstring."""
    return _resolve_owned_download("stl", filename, wallet, "model/stl")


@app.get("/download/iges/{filename}")
async def download_iges(
    filename: str,
    wallet: Annotated[dict | None, Depends(wallet_auth.get_current_wallet_optional)],
):
    """Download IGES file. See _resolve_owned_download's docstring."""
    return _resolve_owned_download("iges", filename, wallet, "model/iges")


@app.get("/download/dxf/{filename}")
async def download_dxf(
    filename: str,
    wallet: Annotated[dict | None, Depends(wallet_auth.get_current_wallet_optional)],
):
    """Download multi-view orthographic DXF file (front/top/side). See
    _resolve_owned_download's docstring."""
    return _resolve_owned_download("dxf", filename, wallet, "image/vnd.dxf")


@app.get("/download/pdf/{filename}")
async def download_pdf(
    filename: str,
    wallet: Annotated[dict | None, Depends(wallet_auth.get_current_wallet_optional)],
):
    """Download 1:1 scale vector PDF technical drawing. See
    _resolve_owned_download's docstring."""
    return _resolve_owned_download("pdf", filename, wallet, "application/pdf")


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
