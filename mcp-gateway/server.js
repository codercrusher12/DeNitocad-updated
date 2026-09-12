/**
 * DEPRECATED — superseded by a2mcp/ (Python, mounted at /mcp on the
 * FastAPI backend directly). This Node gateway is the same
 * "REST route wrapped by the generic community @x402/evm package" shape
 * that got Stitchfren's identical gateway rejected by OKX's A2MCP review
 * ("service isn't integrated with the official OKX Payment SDK") — see
 * a2mcp/x402.py's module docstring for the full story. Keeping this file
 * in place for reference / rollback only. Do not point a new OKX listing
 * at this service; decommission the Railway deployment once a2mcp/ is
 * verified live.
 *
 * nl-to-cad MCP Gateway
 * ---------------------
 * A2MCP (pay-per-call) front door for the OKX AI Marketplace.
 *
 * Thin Node/Express service in front of the existing FastAPI backend
 * (web_app.py) - nothing there is touched by this file. Exposes the one
 * real job this backend does - parse + generate + validate + export - as
 * a single x402-gated HTTP route, using OKX's x402 facilitator on X Layer
 * (eip155:196), same as Stitchfren's gateway.
 *
 * DIFFERENCE FROM STITCHFREN'S GATEWAY, ON PURPOSE:
 * Stitchfren's job is multi-step (draft, nest via true NFP placement,
 * DXF export) and runs long enough to need Celery + a submit/poll split
 * (POST /api/pattern, then poll GET /api/status/:taskId). CAD generation
 * here is a single synchronous FastAPI call that returns in 1-3 seconds
 * (see README's Performance section) - there's no queue on the Python
 * side to poll against. So this gateway calls /generate once and returns
 * the result directly; no polling loop, no "pending" branch. Building an
 * async poll here would be matching Stitchfren's shape without matching
 * its reason for existing.
 *
 * WHAT YOU STILL NEED TO DO BEFORE THIS GOES LIVE:
 *  1. `npm install` - @x402/core, @x402/evm, and @x402/express versions
 *     below (2.3.0 / 2.9.0 / 2.3.0) were confirmed against npm directly
 *     on 2026-07-31, not carried over as unverified placeholders.
 *     Re-check before installing if you're reading this much later.
 *  2. Get OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE by registering as
 *     an Agent Service Provider (ASP) on OKX AI, A2MCP mode - that
 *     registration happens on OKX's side, not in this code.
 *  3. Set PAY_TO_ADDRESS to the wallet that should receive settlement.
 *  4. Set NL_TO_CAD_API_BASE to your deployed FastAPI URL, and
 *     NL_TO_CAD_SERVICE_KEY to a key generated via that backend's
 *     POST /api/keys/generate (a *service* key the gateway uses
 *     server-to-server - not the browser-demo key the frontend mints
 *     for itself via the same endpoint).
 *  5. Confirm the exact OKX facilitator auth-header contract with one
 *     real paid call in staging before trusting this in production -
 *     signOkx() below is carried over from Stitchfren's gateway, which
 *     itself flagged this as unverified against a live facilitator call.
 *  6. Tune PRICE below - it's a placeholder.
 */

import express from "express";
import crypto from "crypto";

const NETWORK = "eip155:196"; // X Layer — same chain OKX AI settles A2MCP on
const FACILITATOR_URL = "https://web3.okx.com/api/v6/pay/x402";

const REQUIRED_ENV = ["OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "PAY_TO_ADDRESS"];

function paymentIsConfigured() {
  return REQUIRED_ENV.every((k) => !!process.env[k]);
}

// Placeholder pricing for the one real skill this backend offers end to
// end (parse + generate + validate + STEP/STL export - see
// cad_generator.py's generate_from_text, which always runs these
// together in one synchronous call). Priced in USDT atomic units (6
// decimals) directly on the route below, not as a dollar-string - see
// the note in buildPaymentMiddleware() for why.
export const PRICE_USDT_ATOMIC = process.env.NL_TO_CAD_AMOUNT || "500000"; // $0.50

// Same OKX Onchain OS x402 API contract Stitchfren's gateway uses
// (web3.okx.com/onchainos/dev-docs/payments/api-http-onetime). Prehash
// formula (timestamp+method+requestPath+body, HMAC-SHA256, base64) and
// the four auth headers match exactly.
function signOkx(method, requestPath, body = "") {
  const timestamp = new Date().toISOString();
  const prehash = `${timestamp}${method.toUpperCase()}${requestPath}${body}`;
  const sign = crypto.createHmac("sha256", process.env.OKX_SECRET_KEY).update(prehash).digest("base64");
  const headers = {
    "OK-ACCESS-KEY": process.env.OKX_API_KEY,
    "OK-ACCESS-SIGN": sign,
    "OK-ACCESS-TIMESTAMP": timestamp,
    "OK-ACCESS-PASSPHRASE": process.env.OKX_PASSPHRASE,
  };
  if (method.toUpperCase() === "POST") {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

// OKX wraps every REST response (including the x402 facilitator) in
// {"code":"0","msg":"...","data":{...}} - @x402/core's facilitator
// client expects the unwrapped payload. Scoped narrowly to
// facilitator-URL calls only, same as Stitchfren's gateway.
function installFacilitatorEnvelopeUnwrap() {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input?.url;
    const response = await originalFetch(input, init);
    if (!url || !url.startsWith(FACILITATOR_URL)) return response;

    let body;
    try {
      body = await response.clone().json();
    } catch {
      return response;
    }
    if (body && typeof body === "object" && "code" in body && "data" in body) {
      return new Response(JSON.stringify(body.data), {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    }
    return response;
  };
}

async function buildPaymentMiddleware() {
  if (!paymentIsConfigured()) return null;

  const { paymentMiddleware, x402ResourceServer } = await import("@x402/express");
  const { ExactEvmScheme } = await import("@x402/evm/exact/server");
  const { HTTPFacilitatorClient } = await import("@x402/core/server");

  installFacilitatorEnvelopeUnwrap();

  const facilitatorClient = new HTTPFacilitatorClient({
    url: FACILITATOR_URL,
    createAuthHeaders: async () => ({
      verify: signOkx("POST", "/api/v6/pay/x402/verify"),
      settle: signOkx("POST", "/api/v6/pay/x402/settle"),
      supported: signOkx("GET", "/api/v6/pay/x402/supported"),
    }),
  });

  const resourceServer = new x402ResourceServer(facilitatorClient);
  resourceServer.register(NETWORK, new ExactEvmScheme());

  const routes = {
    "POST /mcp/generate-cad": {
      accepts: [{
        scheme: "exact",
        network: NETWORK,
        payTo: process.env.PAY_TO_ADDRESS,
        // eip155:196 (X Layer) has no entry in x402's default-asset table,
        // so a dollar-string price ("$0.5") throws "No default asset
        // configured for network eip155:196" (confirmed live, 2026-08-07).
        // Fix per docs.x402.org/core-concepts/network-and-token-support:
        // `price` must be a TokenAmount object — amountInAtomicUnits +
        // asset address + eip712{name,version} — not a dollar-string, and
        // not sibling amount/asset keys (that was the first, wrong, fix
        // attempt here: parsePrice only ever reads `price`, so anything
        // outside it is silently ignored and price comes through
        // undefined). TODO: confirm `version` against this token's own
        // version() read function on X Layer's block explorer before
        // relying on this in production — "1" is Tether's common default
        // but hasn't been verified on-chain for this specific deployment,
        // and a wrong EIP-712 version fails signature verification rather
        // than crashing loudly like the two bugs above did.
        price: {
          amountInAtomicUnits: PRICE_USDT_ATOMIC, // 6 decimals → $0.50 USDT
          asset: "0x1e4a5963abfd975d8c9021ce480b42188849d41d", // USDT on X Layer, confirmed via OKLink
          eip712: { name: "Tether USD", version: "1" },
        },
      }],
      description:
        "Convert a plain-English mechanical part description into a manufacturable .STEP CAD file (with .STL for preview), using CadQuery/OpenCASCADE. Returns download links plus the parsed parameters and any validation warnings/auto-corrections.",
      mimeType: "application/json",
    },
  };

  return paymentMiddleware(routes, resourceServer);
}

// ---------- Backend bridge ----------

const API_BASE = process.env.NL_TO_CAD_API_BASE; // e.g. https://your-app.up.railway.app
const SERVICE_KEY = process.env.NL_TO_CAD_SERVICE_KEY;

async function generateCad(payload) {
  const res = await fetch(`${API_BASE}/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": SERVICE_KEY },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Backend rejected the job (${res.status}): ${await res.text()}`);
  return res.json(); // the full generate_from_text() result, synchronous
}

async function fetchJob(jobId) {
  const res = await fetch(`${API_BASE}/api/jobs/${jobId}`, {
    headers: { "X-API-Key": SERVICE_KEY },
  });
  if (!res.ok) throw new Error(`Job lookup failed (${res.status})`);
  return res.json();
}

// ---------- Agent-facing response shaping ----------
//
// An agent reading this in an OKX terminal shouldn't have to parse a raw
// validation object to find out what happened. Every response gets a
// short plain-language "message" plus the STEP download link, both front
// and center - same principle as Stitchfren's gateway, built here from
// the parsed part type and any warnings/corrections since this backend
// doesn't have an LLM-narrative field the way Stitchfren's cutting sheet
// does.
function buildMessage(result) {
  if (!result.success) {
    return `Could not generate this part: ${result.error || "unknown error"}.`;
  }
  const params = result.parameters || {};
  const partType = (params.part_type || "part").replace(/_/g, " ");
  const parts = [`Your ${partType} is ready as a STEP file.`];

  const warnings = (result.validation && result.validation.warnings) || [];
  const corrections = (result.validation && result.validation.corrections) || {};
  if (Object.keys(corrections).length) {
    parts.push(`${Object.keys(corrections).length} parameter(s) were auto-corrected to keep the geometry valid.`);
  }
  if (warnings.length) {
    parts.push(warnings.join(" "));
  }
  return parts.join(" ");
}

function buildAgentResponse(result) {
  return {
    ok: !!result.success,
    job_id: result.job_id || null,
    message: buildMessage(result),
    download_url: result.step_url || null,
    stl_url: result.stl_url || null,
    part_type: result.parameters ? result.parameters.part_type : null,
    parameters: result.parameters ? result.parameters.parameters : null,
    material: result.parameters ? result.parameters.material : null,
    validation: result.validation || null,
    error: result.error || null,
  };
}

const app = express();
app.use(express.json());

app.get("/health", (_req, res) => {
  res.json({
    status: "ok",
    payment_configured: paymentIsConfigured(),
    backend_configured: !!(API_BASE && SERVICE_KEY),
  });
});

// Free — lets an agent re-fetch a job it already paid for without paying
// again. No polling semantics needed (generation is synchronous), this
// is purely "look this up again later."
app.get("/mcp/jobs/:jobId", async (req, res) => {
  if (!API_BASE || !SERVICE_KEY) {
    return res.status(500).json({ error: "Gateway misconfigured: NL_TO_CAD_API_BASE / NL_TO_CAD_SERVICE_KEY not set." });
  }
  try {
    const job = await fetchJob(req.params.jobId);
    res.json({
      ok: true,
      job_id: job.id,
      message: job.success ? `${(job.part_type || "part").replace(/_/g, " ")} — generated ${job.created_at}.` : `Failed: ${job.error}`,
      download_url: job.step_url,
      stl_url: job.stl_url,
      part_type: job.part_type,
    });
  } catch (err) {
    res.status(404).json({ ok: false, error: err.message });
  }
});

const paymentMiddleware = await buildPaymentMiddleware();
if (!paymentMiddleware) {
  console.warn(
    "[nl-to-cad-mcp-gateway] OKX payment env vars not set — /mcp/generate-cad is running WITHOUT payment gating. Do not point OKX's A2MCP listing at this instance until OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, and PAY_TO_ADDRESS are configured."
  );
} else {
  app.use(paymentMiddleware);
}

app.post("/mcp/generate-cad", async (req, res) => {
  if (!API_BASE || !SERVICE_KEY) {
    return res.status(500).json({ error: "Gateway misconfigured: NL_TO_CAD_API_BASE / NL_TO_CAD_SERVICE_KEY not set." });
  }
  try {
    // req.body is forwarded as-is to /generate: { description, use_deepseek, api_key, model }
    const result = await generateCad(req.body);
    const status = result.success ? 200 : 422;
    res.status(status).json(buildAgentResponse(result));
  } catch (err) {
    res.status(502).json({ ok: false, error: err.message });
  }
});

const PORT = process.env.PORT || 8403;
app.listen(PORT, () => console.log(`nl-to-cad-mcp-gateway listening on :${PORT}`));
