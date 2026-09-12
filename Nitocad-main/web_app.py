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
from cad_generator import CADGenerator
from config import settings
from exceptions import GenerationError, PaymentError, UnsupportedFormatError, register_exception_handlers
from file_safety import safe_output_path
from logging_config import configure_logging, get_logger, set_request_id
from security import get_current_key

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
    # request/response schema and every route including /api/keys/generate.
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
    # description - unrelated to the X-API-Key header that authenticates
    # against this service. Two different keys, two different purposes.
    api_key: str | None = None
    model: str = "deepseek-v4-flash"
    # No longer honored - /generate now always builds STL only (the
    # preview). Kept on the model so old callers that still send it don't
    # get a 422 on an unrecognized field; the value is ignored. Every
    # other format is deferred to GET /export/{fmt}/{job_id}, paid and
    # built on demand - see easycad's decision log for why.
    formats: list[str] | None = None
    # Proof of the settings.BOTCHAIN_PER_CALL_PRICE_BOT native-BOT
    # payment to settings.TREASURY_ADDRESS on BOT Chain - see
    # botchain_pay.py. Required; this specific endpoint has no free
    # tier - see POST /preview for the free, STL-only, no-wallet route
    # people can try before paying anything.
    tx_hash: str

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank")
        return v


class PreviewRequest(BaseModel):
    """POST /preview - free, no wallet, no API key, STL-only. See that
    route's docstring for why it forces the fallback parser and why
    the resulting job isn't later upgradeable to a paid export."""
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


class ApiKeyResponse(BaseModel):
    api_key: str


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


class KeyGenerateRequest(BaseModel):
    # Proof of the settings.BOTCHAIN_KEY_ISSUE_PRICE_BOT native-BOT
    # payment to settings.TREASURY_ADDRESS - see botchain_pay.py.
    tx_hash: str


@app.post("/api/keys/generate", response_model=ApiKeyResponse)
@limiter.limit(settings.RATE_LIMIT_KEY_ISSUE)
async def generate_service_key(request: Request, body: KeyGenerateRequest):
    """
    Issues a new service API key for this backend (X-API-Key header on
    /generate), after verifying a one-time BOTCHAIN_KEY_ISSUE_PRICE_BOT
    payment to the treasury address on BOT Chain. The key's user_id is
    bound to the paying wallet address itself (returned by
    verify_and_record_payment), not a caller-supplied value - a key is
    tied to whoever actually paid for it, which is what later lets
    /generate and /export bind their own per-call payments back to the
    same wallet via expected_sender. Rate-limited per IP
    (RATE_LIMIT_KEY_ISSUE, default 5/hour).
    """
    try:
        sender = botchain_pay.verify_and_record_payment(
            body.tx_hash,
            purpose="key_issue",
            min_amount_bot=botchain_pay.KEY_ISSUE_PRICE_BOT,
            user_id="pending",
        )
    except PaymentError as exc:
        raise HTTPException(status_code=402, detail=exc.message) from exc
    raw_key = db.create_api_key(user_id=sender)
    logger.info("issued new api key", extra={"user_id": sender})
    return ApiKeyResponse(api_key=raw_key)


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
        "key_issue_price_bot": float(botchain_pay.KEY_ISSUE_PRICE_BOT),
        "per_call_price_bot": float(botchain_pay.PER_CALL_PRICE_BOT),
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
                    <strong>Available on download (built on demand, paid per format):</strong>
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
            let SERVICE_API_KEY = null; // this backend's own X-API-Key, not the DeepSeek key
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

            async function ensureServiceKey() {
                // Same localStorage-caching shape as before, but issuing a
                // key now costs a real payment - see /api/keys/generate.
                const cached = localStorage.getItem('nl_to_cad_service_key');
                if (cached) {
                    SERVICE_API_KEY = cached;
                    return;
                }
                const cfg = await getChainConfig();
                const txHash = await payBot(cfg.key_issue_price_bot);
                const resp = await fetch('/api/keys/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tx_hash: txHash }),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    throw new Error(data.detail || 'Key issuance failed');
                }
                SERVICE_API_KEY = data.api_key;
                localStorage.setItem('nl_to_cad_service_key', SERVICE_API_KEY);
            }

            async function downloadFormat(fmt, jobId) {
                // Can't use a plain <a href>/window.open here - the export
                // route requires X-API-Key as a header (see security.py's
                // get_current_key), and neither of those can set custom
                // headers on a GET. fetch() + blob is the correct way to
                // download an authenticated file, not a workaround.
                const btn = event.target;
                const originalText = btn.textContent;
                btn.disabled = true;
                btn.textContent = 'Paying...';
                try {
                    const cfg = await getChainConfig();
                    const txHash = await payBot(cfg.per_call_price_bot);
                    btn.textContent = 'Building...';
                    const resp = await fetch(`/export/${fmt}/${jobId}?tx_hash=${txHash}`, {
                        headers: { 'X-API-Key': SERVICE_API_KEY },
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
                
                resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Generating CAD...</p></div>';
                generateBtn.disabled = true;
                previewBtn.disabled = true;
                
                try {
                    if (!SERVICE_API_KEY) {
                        await ensureServiceKey();
                    }
                    const cfg = await getChainConfig();
                    resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Waiting for payment...</p></div>';
                    const txHash = await payBot(cfg.per_call_price_bot);
                    resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Generating CAD...</p></div>';
                    const response = await fetch('/generate', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-API-Key': SERVICE_API_KEY,
                        },
                        body: JSON.stringify({
                            description: description,
                            use_deepseek: useDeepSeek,
                            api_key: apiKey,
                            tx_hash: txHash,
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        let html = renderResultBase(data);
                        
                        // STL already exists on disk (it built the preview,
                        // and is served unauthenticated - see
                        // /download/stl/{filename}), so it's a direct link.
                        // Every other checked format is built - and paid
                        // for - only when its button is actually clicked;
                        // see downloadFormat().
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
                                html += `<button onclick="downloadFormat('${cb.value}', '${data.job_id}')">📥 Download ${label} (${chainConfig ? chainConfig.per_call_price_bot : '0.2'} BOT)</button>`;
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
                }
            }
            
            // type="module" scripts don't leak declarations onto window,
            // so the inline onclick="generate()" / onclick="setExample(...)"
            // handlers in the HTML above need these attached explicitly.
            window.generate = generate;
            window.previewFree = previewFree;
            window.setExample = setExample;

            // Initialize viewer on load
            window.addEventListener('load', () => {
                initViewer();
                ensureServiceKey();

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
    """Free STL-only preview - no wallet, no X-API-Key, no BOT payment.
    Lets someone evaluate NitoCAD before spending anything. Rate-limited
    hard per IP (RATE_LIMIT_PREVIEW, default 5/hour) since nothing else
    throttles this route the way a real BOT payment throttles /generate.

    Deliberately forces use_deepseek=False - the fallback regex parser,
    not DeepSeek. DeepSeek calls cost real money per call regardless of
    whether the caller pays BOT, and this route has no payment gate to
    recover that cost from; letting it call DeepSeek would make it a
    free way to burn this server's DeepSeek budget at scale.

    The resulting job is NOT later exportable via
    GET /export/{fmt}/{job_id} - its user_id is "anonymous", which will
    never match a real wallet-bound API key's user_id. This is
    deliberate, not a bug: making a preview "upgradeable" to a paid
    export would mean letting a paid API key claim an existing job by
    its job_id, and nothing currently stops an anonymous job_id from
    being seen and claimed by someone other than whoever generated it.
    Liking a preview means re-submitting the same description through
    the paid /generate flow - a fresh job, not unlocking this one."""
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
    key_info: Annotated[dict, Depends(get_current_key)],
):
    """Generate CAD from description. Requires X-API-Key (see
    POST /api/keys/generate) AND proof of a BOTCHAIN_PER_CALL_PRICE_BOT
    payment (tx_hash). Rate-limited per IP (RATE_LIMIT_GENERATE, default
    20/minute) - each call does real CadQuery/OCCT work.

    Only STL is built here (the preview) - STEP/IGES/DXF/PDF are
    deferred to GET /export/{fmt}/{job_id}, paid and built separately,
    only for formats actually requested. See easycad's decision log."""
    try:
        botchain_pay.verify_and_record_payment(
            body.tx_hash,
            purpose="generate",
            min_amount_bot=botchain_pay.PER_CALL_PRICE_BOT,
            user_id=key_info["user_id"],
            expected_sender=key_info["user_id"],
        )
    except PaymentError as exc:
        raise HTTPException(status_code=402, detail=exc.message) from exc

    result = generator.generate_from_text(
        body.description,
        use_deepseek=body.use_deepseek,
        api_key=body.api_key,
        model=body.model,
        user_id=key_info["user_id"],
        formats=["stl"],
    )
    return result


@app.get("/export/{fmt}/{job_id}")
@limiter.limit(settings.RATE_LIMIT_GENERATE)
async def export_on_demand(
    request: Request,
    fmt: str,
    job_id: str,
    tx_hash: str,
    key_info: Annotated[dict, Depends(get_current_key)],
):
    """Build (or reuse a same-instance cached build of) exactly one
    export format for an already-generated job, on demand, paid per
    call. expected_sender=key_info["user_id"] means the payment must
    come from the same wallet the calling API key is bound to - see
    botchain_pay.py's module docstring for why (a ShieldGuard audit
    finding: without this, any valid unused tx hash paying the treasury,
    including someone else's, could be spent against someone else's
    call). Rate-limited the same as /generate - this does real
    CadQuery/OCCT work, same as generation itself."""
    try:
        botchain_pay.verify_and_record_payment(
            tx_hash,
            purpose="generate",
            min_amount_bot=botchain_pay.PER_CALL_PRICE_BOT,
            user_id=key_info["user_id"],
            expected_sender=key_info["user_id"],
        )
    except PaymentError as exc:
        raise HTTPException(status_code=402, detail=exc.message) from exc

    job = db.get_job(job_id)
    if job is None or job["user_id"] != key_info["user_id"]:
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
async def list_jobs(key_info: Annotated[dict, Depends(get_current_key)]):
    """Audit history for the authenticated key - every job it has run."""
    return db.list_jobs(user_id=key_info["user_id"])


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, key_info: Annotated[dict, Depends(get_current_key)]):
    """
    Look up one job by id. Generation here is synchronous (1-3s, no
    Celery queue - see README), so this isn't a poll-for-completion
    endpoint like Stitchfren's /api/status/{task_id}. It exists so the
    mcp-gateway (or any agent) can re-fetch a completed job's download
    links later without re-running generation.
    """
    job = db.get_job(job_id)
    if job is None or job["user_id"] != key_info["user_id"]:
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
