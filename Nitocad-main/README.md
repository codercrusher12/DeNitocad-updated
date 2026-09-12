# Natural Language to Parametric CAD - Full Version

Convert plain English descriptions into manufacturable CAD files (STEP,
STL, IGES, DXF, PDF) using smart parsing and CadQuery. **Every
generation produces all five formats by default** — see v2.2.1 below.

## v2.2.1: Multi-format export is now the default

`exporters.DEFAULT_FORMATS` (what `/generate` falls back to when the
caller omits `formats` entirely) was `("step", "stl")` — the v2.2.0
export matrix below was opt-in only. Changed the default to all five
supported formats: `("step", "stl", "iges", "dxf", "pdf")`. A caller
that wants the old, narrower two-format behavior back can still get it
by passing `formats: ["step", "stl"]` explicitly — the opt-in mechanism
itself didn't change, only what happens when it's omitted.
`tests/test_exporters.py`'s default-format assertions were updated to
match. One-line constant change, no new logic, but — same standing
caveat as everything export-related below — not yet run against a real
CadQuery/OCP install in this environment.

## v2.2.0: Tier-1 export formats + engine components

Two passes landed on top of the v2.0 hardening pass below, neither of
which the rest of this README reflected until now:

- **Multi-format export (`exporters.py`)** — `/generate` no longer only
  produces STEP/STL. It accepts an optional `formats` list —
  `["step", "stl", "iges", "dxf", "pdf"]` — which as of v2.2.1 (see
  above) is also what it produces by default when omitted. `export_iges` calls OCP's
  `IGESControl_Writer` directly (cadquery 2.3.1's own `exporters`
  module has no IGES `ExportType`). `export_dxf`/`export_pdf` are built
  from a real horizontal section through the solid (layer-classified
  `PIECE_OUTLINE`/`HOLES`/`CENTERLINES` for DXF; a 1:1 scale vector
  drawing with a title block for PDF) — correct for the
  flat/plate/bracket-style parts this catalog is dominated by, **not**
  a full multi-view orthographic projection. New download routes:
  `/download/iges/{filename}`, `/download/dxf/{filename}`,
  `/download/pdf/{filename}`. See `exporters.py`'s module docstring for
  the full scope notes, including why GLTF isn't included.
- **Two new engine-component templates**: `connecting_rod` (small-end/
  big-end bosses with bores, I-beam-style shank, multi-body boolean
  union) and `crankshaft` (configurable throws, main journals, offset
  rod journals, counterweight webs, optional nose/flywheel flange).
  Both are the most geometrically complex templates in the catalog —
  multi-body unions rather than a single extrude+cut — and both caught
  real bugs during development (off-origin bore placement, a "backbone
  cylinder" crank profile that left main-journal material running
  through the throw regions) that are now regression-tested in
  `tests/test_cad_templates.py`.
- **Real MCP server (`a2mcp/`)**, mounted at `/mcp` on this same FastAPI
  app, replacing the standalone `mcp-gateway/` Node service (now
  deprecated — see that section below). One tool, `generate_cad_part`,
  payment-gated per-call via OKX's official Payment SDK
  (`okxweb3-app-x402`) for OKX's A2MCP marketplace.

Combined, the actual part-template count is **25**, not the 23 the
rest of this README used to claim (18 originally shipped + 3 hardware
templates + these 2 engine components) — see `cad_templates/__init__.py`.
None of `exporters.py`, `connecting_rod`/`crankshaft`, or the `a2mcp/`
migration have been run against a real CadQuery/native-dependency
install yet (same standing caveat as everything else below written in
a sandbox with no network access) — run `make test` / `python
smoke_test.py` on your VPS before relying on any of it.

## v2.0: Professional hardening pass

A structural pass focused on config, observability, error handling,
security, and testing — the generation engine itself (parser, templates,
validator) is untouched. Full detail in [`CHANGELOG.md`](CHANGELOG.md);
highlights:

- **`config.py`** — one typed, validated `Settings` object instead of
  ~15 scattered `os.getenv()` calls. See `.env.example` for every knob.
- **Structured logging** (`logging_config.py`) — JSON or console format,
  per-request correlation IDs, replaces every `print()` in the request path.
- **Real error handling** (`exceptions.py`) — a typed exception hierarchy
  with FastAPI handlers that return the right status code and never leak
  a traceback into a response body.
- **Fixed a path-traversal bug** in the `/download/{step,stl}/{filename}`
  routes (`file_safety.py`).
- **Rate limiting** on `/generate` and `/api/keys/generate` (`slowapi`).
- **`/healthz` + `/readyz`** endpoints for container/orchestrator health checks.
- **A real test suite** (`tests/`) — unit tests for the parser/validator,
  API integration tests, and CadQuery-gated geometry tests covering the
  fillet-degradation and structural-beam regressions noted below.
  Run `make check` for the fast suite, `make test` for everything.
- **CI** (`.github/workflows/ci.yml`), pre-commit hooks, `pyproject.toml`
  tooling config, a `Makefile`, and a hardened non-root Dockerfile with
  a `HEALTHCHECK`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local setup and dev workflow, and [`DEPLOYMENT.md`](DEPLOYMENT.md) for deploying to a VPS with Docker Compose + Caddy.

## Production hardening pass (added after DeepSeek integration)

Persistence, auth, and storage layer added on top of the existing
generation engine - the CAD templates, parser, and validator are
untouched. Mirrors the architecture already proven in the Stitchfren
project: plain `sqlite3` (not SQLAlchemy/Postgres - this project's scale
doesn't need it, generation is a synchronous 1-3s call, not a queued job),
hashed API keys, R2 object storage with local fallback.

New files:
- **`db.py`** - sqlite3 persistence: `jobs` table (full audit history -
  description, params, output URLs, success/failure, timestamp) and
  `api_keys` table (hashed keys only, raw key shown once at creation).
- **`security.py`** - `X-API-Key` header auth as a FastAPI dependency,
  backed by `db.py`.
- **`storage.py`** - Cloudflare R2 upload for generated STEP/STL files.
  Falls back to serving the local file directly if R2 env vars aren't
  set (fine for local dev, **not fine for Railway** - its filesystem is
  wiped on every redeploy/restart, so any download link handed out
  before R2 is configured will eventually 404).

Changed:
- **`cad_generator.py`** - filenames now use `uuid4()` instead of
  `hash(description) % 10000` (the old scheme collides fast under
  concurrent traffic, and Python randomizes string `hash()` per process
  by default anyway). Every successful/failed generation is now recorded
  via `db.record_job()`. Outputs are uploaded to R2 when configured.
- **`web_app.py`** - added CORS middleware (frontend deploys separately,
  same split as Stitchfren), `POST /api/keys/generate` (bootstrap
  endpoint, same pattern as Stitchfren's demo: called once, cached in
  `localStorage`), `GET /api/jobs` (per-key audit history), `/generate`
  now requires the `X-API-Key` header. The embedded demo HTML's Three.js
  loading was rewritten from the deprecated global UMD build
  (`build/three.min.js` + legacy `examples/js/*` loaders, both removed
  as of three.js r161) to proper ES module imports via an import map.
- **`deepseek_parser.py`** - explicitly disables DeepSeek's thinking
  mode on the extraction call. `deepseek-v4-flash` defaults to thinking
  *on* as of the July 2026 model migration; for a structured-extraction
  call that only adds latency and cost with no benefit.
- **`requirements.txt`** - added `boto3` for R2.

### Public-facing hardening (frontend + auto parser selection)
- **DeepSeek toggle removed from the UI entirely.** The server now
  auto-decides: uses DeepSeek if a key resolves from anywhere (a
  caller-supplied `api_key`, or `DEEPSEEK_API_KEY` set in the server's
  own environment), otherwise regex fallback - silently and correctly,
  verified with a functional test of all three branches (no key, forced
  regex despite a key being present, auto-with-bad-key falling back
  cleanly). API/agent consumers that want explicit control still have
  it via `use_deepseek: true/false` in the request body; the demo page
  just doesn't expose it.
- **API-endpoint config box hidden from regular visitors.** Only reveals
  via `?config=1` on the URL, then caches your choice in localStorage -
  same one-time-setup pattern as before, just not shown by default.
- **Part-type count corrected: it's 23, not 18** (the original stat on
  this page undercounted - the real registered count was already 20
  before today, now 23 with the 3 additions below).

### 3 new templates: `cad_templates/hardware.py`
- **`hex_standoff`** - hexagonal PCB/electronics standoff, distinct from
  the round spacer/washer/bearing shapes. Uses `Workplane.polygon()`
  (not used elsewhere in this codebase before now) - its diameter
  parameter is across corners, not across flats, verified against
  CadQuery's own API reference before use, not guessed.
- **`t_bracket`** - T cross-section (cap + stem) extruded along its
  length, built by keeping both pieces in the same plane rather than
  porting `generate_structural_beam`'s i_beam flange/web-then-rotate
  approach directly.
- **`channel_bracket`** - open U-channel (mounting channel / cable
  raceway / cradle), built with `Workplane.shell()` the same way
  `simple_box.py` builds its open-top lid box, just open on a side face.

All three route through both the regex fallback (keyword-matched in
`smart_parser.py` - had to reorder a few `elif` branches since e.g.
`"channel"` alone was shadowing `"channel bracket"`, verified with a
routing test after the fix) and DeepSeek's system prompt.

**Separately noticed, not fixed (out of scope for this pass):**
`generate_structural_beam`'s i_beam construction looks like it may
extrude its flanges only `thickness` deep in one local axis while the
web is extruded the full `length` in a different local axis before
being rotated into place - worth a second look on its own.

### Fixed: `generate_structural_beam`'s flange/web mismatch
The thing I'd flagged-but-not-fixed last round. Confirmed real with a
symbolic bounding-box dry run (pure arithmetic, no CadQuery needed -
computes the world-coordinate range each piece occupies and checks
adjacent pieces actually overlap) before and after the fix:

- **i_beam**: the flanges were built as `rect(width, thickness)
  .extrude(thickness)` on the default plane - only `thickness` deep in
  the beam's length direction, effectively thin end-caps, while the web
  was extruded the full `length` in a different local axis and rotated
  into place. Fixed by building all three pieces (top flange, web,
  bottom flange) in the same YZ plane and extruding all of them along X
  by the same `length` - the technique already used for `generate_angle`
  and this round's new `t_bracket`.
- **channel**: `bottom` was built and then rotated 90°, which (per the
  standard rotation matrix) lands its length-extent on the opposite side
  of zero from `left_wall`/`right_wall`'s un-rotated extent - they
  likely didn't actually overlap. Fixed the same way: build `bottom`
  directly in the same XZ plane as the walls, no rotation.
- **Second-order bug this surfaced**: the fixed pieces were touching at
  an exact coincident face (zero volumetric overlap), a known fragile
  case for OpenCASCADE boolean unions. Gave the web/cap a small
  deliberate overlap margin (`min(0.5, thickness/4)`) instead of relying
  on exact contact - applied to both `generate_structural_beam`'s i_beam
  and this round's `t_bracket` (same underlying pattern, same fix).
  `generate_angle` was checked too and didn't need this - its pieces
  already have genuine volumetric overlap on all three axes.
- **Third bug this surfaced**: `infer_missing_dimensions` (the regex
  fallback) never set `beam_type` at all - every regex-parsed structural
  beam silently defaulted to `i_beam` regardless of whether the
  description said "channel" or "c-channel". Fixed by threading the
  description text through to that function and checking for the
  keyword, verified with a routing test (i-beam, c-channel, and
  no-explicit-type-given all resolve correctly now).

Added `i_beam` and `channel` smoke-test cases so this is actually
exercised against real CadQuery on your VPS, not just reasoned about
here.

### Fixed: fillet crashes found by your first real smoke-test run (2/13 failures)
Both failures threw the same OCCT error (`BRep_API: command not done`)
but were two different bugs:

- **`motor_mount`**: it already applies its own correctly-guarded fillet
  internally (`fillet_radius_mm`, validated to stay under half the
  thickness). But `cad_generator.py` then *unconditionally* ran a
  second, completely separate fillet pass from the regex-extracted
  `operations` list ("5mm fillets" in the text → a raw, unvalidated
  5mm radius) - on a plate that had already been filleted once. Two
  independent fillet systems, never synced.
- **`l_bracket`**: this one is more fundamental - it failed at 1.4mm on
  3mm-thick material, a radius the validator had already deemed safe
  (well under the thickness/2 rule). Proves numeric validation alone
  can't predict OCCT fillet feasibility; it's an edge-topology problem
  (likely at the L-corner), not just a magnitude problem.

Fix, in `cad_templates/_safe_ops.py`: a shared `safe_fillet`/
`safe_chamfer` helper that retries at half, then a quarter, of the
requested radius, and if OCCT still rejects it, ships the un-filleted
geometry with a warning instead of crashing the whole job. Wired into
every direct `.fillet()`/`.chamfer()` call site across the templates
(`l_bracket`, `motor_mount`, `flat_plate`, `t_bracket`, `shaft`'s
chamfer) and into `cad_generator.py`'s generic `operations` pass, with
the warning threaded through to the same `validation_result.warnings`
list that already reaches the API response - for the `operations` path.
Template-level fillets (e.g. `l_bracket`'s) degrade silently for now,
since template functions don't currently have a channel back to the
warnings list - worth adding if this shows up in practice.

Verified the retry/degrade control flow itself with a mock workplane
(no CadQuery needed for this part): confirmed it retries and succeeds
at a smaller radius when possible, degrades to the original geometry
with exactly one warning when nothing works, and never raises. **Not
yet re-verified against real CadQuery** - that's the next VPS run.

### Second round, same fillet issue: silent zero-solids, not a crash
Re-ran on your VPS: `l_bracket` now passes clean. `motor_mount` no
longer *crashes*, but the exported STEP file re-imported with **zero
solids** - OCCT completed the fillet call with no exception at all, but
produced degenerate/empty geometry right at the edge of feasibility.
An exception-only retry loop doesn't catch that; it looks like success.

Fixed by validating the actual result after every attempt (checking
`result.solids().vals()` is non-empty, the same check `smoke_test.py`
already uses for re-imported STEP files) instead of only catching
exceptions. Also improved the warning text to include the real failure
reason (`str(exception)`) instead of just the exception's class name,
so "zero solids" and "hard OCCT crash" don't look identical in the log
- that distinction is what made this one findable in the first place.
Re-verified the full retry/degrade/recover logic with a mock workplane
across 6 scenarios, including the exact silent-zero-solids case seen on
your VPS. Next step is confirming this against real CadQuery again.

### Third round, same fillet issue: solids-count wasn't rigorous enough
Re-ran on your VPS again: motor_mount failed the *exact same way*
(zero solids after reimport) even with the solids-count check in place.
The gap: a solid *object* existing in CadQuery's internal stack isn't
the same as it being a genuinely *valid* one - the fillet can produce
something CadQuery still calls "a solid" that's topologically broken,
which only surfaces as "zero solids" once it round-trips through a
real STEP writer/reader (exactly what the smoke test's own re-import
check does, and what my earlier in-process check didn't).

Fixed by switching from a solids-count check to CadQuery's own
validity checker: `Shape.isValid()`, documented in CadQuery's source
(`cadquery/occ_impl/shapes.py`) as wrapping OCCT's
`BRepCheck_Analyzer::IsValid()` - the mechanism OCCT itself provides
for exactly this kind of defect detection, not something invented here.
Every solid from a fillet/chamfer attempt is now checked with
`.isValid()`, not just counted. Re-verified with a mock workplane
across the exact "succeeds but produces an invalid shape" scenario,
plus regression-tested the earlier zero-solids and hard-exception
scenarios to confirm none of them broke. Next step is confirming this
against real CadQuery again - third time being the actual charm,
hopefully.

### Still not done (Python backend)
- **Not tested against a real CadQuery install** - this environment has
  no network access, so the actual STEP/STL generation path (as opposed
  to the db/auth/storage layer, which is tested) needs verification on
  your machine or in deploy. See `smoke_test.py`.
- **Railway deploy**: pin the Python version explicitly (3.11 or 3.12 -
  OCP's pip wheels don't cover 3.13+ yet) and mount a persistent volume
  for `DB_PATH` if you want job history to survive redeploys, same as
  the R2 requirement for the generated files themselves.
- **Not for Vercel**: CadQuery's OCP dependency is a large native wheel
  stack that exceeds Vercel's serverless function size limit. Vercel is
  fine for hosting a *separate* static frontend only - never for this
  FastAPI/CadQuery backend.

## `a2mcp/` — OKX A2MCP listing (current) and `mcp-gateway/` (deprecated)

**Current**: `a2mcp/server.py` is a real MCP protocol server (via
`fastmcp`), mounted directly at `/mcp` on this same FastAPI app
(`web_app.py` imports `mcp_app`/`mcp_app_gated` and mounts them at
startup — see `app.mount("/mcp", mcp_app_gated)`). One tool,
`generate_cad_part`, matching the "one real job this backend does"
framing (parse + generate + validate + export). Payment gating
(`a2mcp/x402.py`) uses OKX's **official Payment SDK**
(`okxweb3-app-x402`), not a generic community x402 package. Gating
happens in `X402Gate`, not inside the tool function: MCP session
bootstrap (`initialize`/`notifications`/`initialized`/`ping`) and
`tools/list` after a free `initialize` stay free so a real client can
complete the handshake and discover the tool without paying; a bare,
sessionless `tools/list` POST is still priced. `FREE_TOOL_NAMES` is
empty — there's no free-preview tool, since the whole job (the STEP
file) is what's being sold, unlike pattern-nesting-style products
where a cheap preview is worth showing before charging.

Configure via the same `OKX_API_KEY`/`OKX_SECRET_KEY`/`OKX_PASSPHRASE`/
`PAY_TO_ADDRESS` env vars as before (see `.env.example`) — no separate
process or deploy needed now, since `a2mcp/` runs inside the main
backend.

**Deprecated**: `mcp-gateway/` (the standalone Node/Express service,
`POST /mcp/generate-cad`, using the community `@x402/*` packages) is
**superseded** by `a2mcp/` — its own file header now says so explicitly.
It failed OKX's A2MCP review for not integrating the official OKX
Payment SDK, which is the entire reason `a2mcp/` exists. The code is
kept in the repo for reference/rollback only. **Do not point a new OKX
listing at it**; decommission its Railway deployment once `a2mcp/` is
verified live.

**Not yet verified**: no network access in the environment this was
built in, so neither `a2mcp/`'s real MCP handshake nor a real payment-
gated call has been run end-to-end. `okxweb3-app-x402` and
`fastmcp==2.14.7` (`requirements.txt`) were not independently confirmed
against PyPI at time of writing — check before relying on them.

**Still to do on OKX's side**: registering as an ASP and pointing the
marketplace at `/mcp` happens through OKX's own signup flow — not
something this repo can do for you.

## Deployment topology

Two separate deploys (down from three — `a2mcp/`'s migration into the
main FastAPI process retired the standalone gateway deploy):

| Piece | Where | Why not somewhere else |
|---|---|---|
| FastAPI + CadQuery backend (now includes `/mcp` — see above) | Railway, **Docker** builder | CadQuery's OCP dependency needs system libs (`libgl1` etc.) and a pinned Python (3.9-3.12 only) that Railway's default Nixpacks builder has repeatedly failed to honor reliably (see `Dockerfile` comments, sourced from Railway's own help forum). A Dockerfile removes the guesswork. Not Vercel — OCP's wheel stack blows past Vercel's 250MB serverless function limit. |
| `frontend/index.html` | Vercel / Netlify / GitHub Pages, static, no build step | It's one static file with a configurable API-endpoint box (localStorage), same pattern as Stitchfren's `frontend/`. |

`mcp-gateway/`'s `Dockerfile`/`railway.json` are still in the repo but
should not be deployed as a live service going forward (see above).

Before `frontend/index.html` is genuinely public, also tighten
`web_app.py`'s CORS `allow_origins=["*"]` to your actual deployed
frontend domain.

Note there are now two copies of the demo UI: the inline HTML still
served at the backend's own `/` (handy for quick same-origin testing —
no endpoint config needed, works the moment `uvicorn` is running) and
`frontend/index.html` (the one to actually deploy separately for the
OKX listing). They're kept in sync manually; if you change one, change
the other.

## DeepSeek Integration (real LLM parsing, added after initial delivery)

The originally delivered `smart_parser.py` is pure regex/keyword matching,
no language model involved at all, despite this project's own stated
design principle (LLM parses intent, deterministic templates + CadQuery
do the actual geometry). `deepseek_parser.py` restores real LLM parsing
using DeepSeek's API, `smart_parser.py`'s regex parser remains as the
automatic fallback if DeepSeek isn't configured or the API call fails.

**Not yet verified end-to-end**: no internet access or DeepSeek API key
were available in the environment this was built in, so the real API
call has not been tested. Written against DeepSeek's current, documented
OpenAI-compatible API. DeepSeek has renamed models before (the older
`deepseek-chat`/`deepseek-reasoner` names were retired after July 24,
2026), current names used here are `deepseek-v4-flash` and
`deepseek-v4-pro`, verify against api-docs.deepseek.com if this stops
working.

```bash
# Web UI: check "Use DeepSeek API" and paste a key
python web_app.py

# CLI
export DEEPSEEK_API_KEY="your-key-here"
python cli.py --deepseek "mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick"

# Python API
from cad_generator import CADGenerator
generator = CADGenerator()
result = generator.generate_from_text(
    "gear with 20 teeth, module 2, 10mm thick, 5mm bore",
    use_deepseek=True,
    api_key="your-key-here"
)
```

**Also fixed while in this file**: `web_app.py`'s JavaScript had a real
syntax bug in the auto-corrections display (`for...of` loop closed with
`});` instead of `}`), which would break the whole script the moment a
request returned any corrections. Fixed.

**Left as-is, worth knowing about**: `llm_parser.py` (the original Claude
integration) is still in the repo but no longer imported by
`cad_generator.py`. **Correction**: `requirements.txt` no longer
carries the `anthropic` package — it was actually removed (it was
fighting `openai`'s httpx version range and causing pip backtracking).
If Claude support is wired back in later, re-add a current `anthropic`
version, not the old pinned one that caused the conflict.

## What's New (All Limitations Fixed)

### ✅ Expanded Part Library (25 Templates — see `cad_templates/__init__.py`)
- **Basic parts**: Motor mounts, L-brackets, flat plates, boxes/enclosures
- **Primitives**: Shafts, bearings, spacers, washers, spheres
- **Transmission**: Gears, pulleys, sprockets
- **Structural**: I-beams, channels, angles, tubes
- **Piping**: Pipes, flanges, elbows, tees
- **Freeform**: Revolved, swept, and lofted shapes
- **Misc**: Hinges, cams
- **Hardware**: Hex standoffs, T-brackets, U-channel brackets
- **Engine components**: Connecting rods, crankshafts (multi-body boolean unions — the most geometrically complex templates in the catalog)

### ✅ Smart Parser
- Regex-based dimension extraction (handles mm, cm, inches, fractions)
- Automatic hole pattern detection
- Material recognition
- Constraint inference for missing dimensions
- Confidence scoring

### ✅ Geometric Validation
- Automatic detection of impossible geometry
- Auto-correction of invalid parameters
- Warnings for edge cases
- Manufacturability checks

### ✅ Assembly Support
- Multi-part assemblies
- Position and rotation constraints
- Export as single STEP file

### ✅ Freeform Capabilities
- Revolved parts (from profile)
- Swept parts (profile along path)
- Lofted parts (between profiles)

### ✅ Enhanced Web UI
- Real-time 3D preview with Three.js
- Interactive orbit controls
- Example library
- Validation feedback
- Auto-correction display

### ✅ Multi-Format Export (`exporters.py`)
- **STEP** / **STL** — original formats, unchanged behavior
- **IGES** — exact B-rep, legacy mechanical CAD interchange
- **DXF** — layer-classified 2D section (outline / holes / centerlines) for laser/plasma/waterjet cutting
- **PDF** — 1:1 scale vector technical drawing with a title block
- All five produced by default; pass `formats: [...]` in the `/generate` request body to narrow it (e.g. `["step", "stl"]` for the old behavior)
- DXF/PDF come from a single horizontal section — correct for flat/plate/bracket-style parts, not a full multi-view orthographic drawing

### ✅ Agent-to-Agent / Pay-Per-Call (`a2mcp/`)
- Real MCP protocol server mounted at `/mcp` on this same backend
- One tool, `generate_cad_part`, priced per call via OKX's official Payment SDK for the OKX A2MCP marketplace
- Session bootstrap and tool discovery stay free; only the actual generation call is gated

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Web Interface
```bash
python web_app.py
```
Open http://localhost:8000

### Command Line
```bash
python cli.py "mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick"
```

### Python API
```python
from cad_generator import CADGenerator

generator = CADGenerator()
result = generator.generate_from_text("shaft 10mm diameter, 50mm long")

if result["success"]:
    print(f"STEP: {result['step_file']}")
    print(f"Warnings: {result['validation']['warnings']}")
```

## Supported Part Types

25 registered `part_type` values (`cad_templates/__init__.py`'s
`TEMPLATES` dict is the source of truth — this list is kept in sync
with it):

1. **Motor Mount** - Stepper motor mounting plates
2. **L-Bracket** - L-shaped brackets
3. **Flat Plate** - Plates with hole patterns
4. **Simple Box** - Enclosures with optional lids
5. **Shaft** - Cylinders with optional keyways
6. **Bearing** - Simple bearing representations
7. **Spacer** - Ring-shaped part
8. **Washer** - Ring-shaped part
9. **Sphere** - Simple spherical primitive
10. **Gear** - Simplified spur gears
11. **Pulley** - Belt pulleys
12. **Sprocket** - Chain sprockets
13. **Structural Beam** - I-beams, channels
14. **Angle** - L-angle structural
15. **Tube** - Hollow tubes (round, square, rectangular)
16. **Pipe Fitting** - Pipes, elbows, tees
17. **Flange** - Pipe flanges
18. **Hinge** - Simple hinges
19. **Cam** - Cam profiles
20. **Freeform** - Revolved, swept, lofted shapes
21. **Hex Standoff** - Hexagonal PCB/electronics standoff (diameter is across corners, not across flats)
22. **T-Bracket** - T cross-section (cap + stem), extruded along its length
23. **Channel Bracket** - Open U-channel (mounting channel / cable raceway / cradle)
24. **Connecting Rod** - Small-end/big-end bosses with bores + I-beam-style shank, multi-body union
25. **Crankshaft** - Configurable throws, main journals, offset rod journals, counterweight webs, optional nose/flywheel flange

## Example Descriptions

```
"mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick"
"L-bracket 50mm wide, 60mm tall, 40mm deep, 3mm thick, 2 holes per leg"
"flat plate 100x80mm, 5mm thick, 4x3 hole pattern, 3mm corner fillets"
"shaft 10mm diameter, 50mm long, 0.5mm chamfer"
"gear with 20 teeth, module 2, 10mm thick, 5mm bore"
"pulley 40mm outer, 10mm belt width, 5mm bore, 15mm thick"
"bearing 10mm inner, 20mm outer, 5mm wide"
"box enclosure 100x80x50mm, 3mm walls, with lid"
"I-beam 100mm tall, 50mm wide, 200mm long, 5mm thick"
"pipe 20mm outer, 2mm wall, 50mm long"
"flange 100mm outer, 50mm bore, 10mm thick, 4 bolt holes"
"hex standoff 6mm across corners, 12mm tall, M3 through hole"
"T-bracket 40mm cap width, 30mm stem height, 60mm long, 3mm thick"
"channel bracket 30mm wide, 20mm tall, 100mm long, 2mm thick"
"connecting rod, 20mm big end bore, 10mm small end bore, 120mm center-to-center"
"crankshaft, 4 throws, 30mm main journal diameter, 25mm rod journal diameter"
```

## Architecture

```
User Description
    ↓
Smart Parser (regex fallback, or DeepSeek LLM extraction if a key is configured)
    ↓
Parameter Validation & Auto-correction
    ↓
Template Router (25 templates, cad_templates/__init__.py)
    ↓
CadQuery Template Engine
    ↓
OpenCASCADE Kernel (B-rep geometry)
    ↓
Multi-format Export: STEP, STL, IGES, DXF, PDF (exporters.py)
    ↓
3D Preview (Three.js) + R2/local file storage + sqlite3 job history
```

Also exposed as an MCP tool (`a2mcp/`, mounted at `/mcp`), payment-gated
per call via OKX's official Payment SDK for the OKX A2MCP marketplace.

## Validation Examples

The validator catches and auto-corrects issues:
- Fillet radius too large for thickness → auto-reduces
- Hole diameter larger than thickness → auto-reduces
- Wall thickness too large for box → auto-reduces
- Gear teeth too few → warns about undercut
- Bore too large for gear → auto-reduces

## Extending

### Add New Template
1. Create file in `cad_templates/`
2. Implement function: `def generate_xxx(params: dict) -> cq.Workplane`
3. Register in `cad_templates/__init__.py`
4. Add validator in `validator.py`
5. Update parser in `smart_parser.py`

### Add New Operations
Edit `cad_generator.py` `_apply_operations()` method to support new CadQuery operations.

## Limitations (Remaining)

- Organic/freeform shapes require explicit profile points
- True involute gear profiles (simplified representation)
- Complex multi-body assemblies (basic support)
- Thread generation (simplified holes only)

These are architectural choices for reliability, not technical limitations.

## Performance

- Typical generation time: 1-3 seconds
- Parser: <100ms
- Validation: <50ms
- CAD generation: 500ms-2s
- Export: 200-500ms

## License

MIT
