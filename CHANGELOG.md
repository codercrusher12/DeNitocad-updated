# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/).

## [2.3.0] — Multi-view orthographic + multi-part DXF/PDF export

`export_dxf`/`export_pdf` previously derived their entire drawing from
one horizontal section through the part (`Workplane.section()` at
Z=0) — correct for flat/plate-style parts, but blind to any geometry
that didn't intersect that one cutting plane (a stepped shaft's
diameter change along its axis, for example). Replaced with a real
multi-view orthographic drawing.

### Changed
- **`exporters.py`** — `export_dxf`/`export_pdf` now generate front,
  top, and right-side views, each via OCCT's `HLRBRep_Algo`
  hidden-line-removal algorithm (through OCP — no new dependency),
  separating visible edges (solid line) from hidden edges (dashed
  line). Views are laid out in the standard third-angle drafting
  arrangement (top above front, side beside front, shared-axis
  registered — see `_layout_three_views`), with per-view labels and a
  title block. This is the new default behavior, not an opt-in flag —
  there is no `multi_view`/`view_mode` parameter.
- DXF layers changed from the section-based `PIECE_OUTLINE` / `HOLES`
  / `CENTERLINES` set to `VISIBLE` / `HIDDEN` / `LABELS` / `BORDER`,
  since HLR classifies edges by visibility, not by section-wire
  nesting/area.
- **`export_all`** / **`export_dxf`** / **`export_pdf`** gained an
  optional `parts: list[(name, shape)]` parameter. For assemblies,
  passing `parts` draws one full front/top/side block per sub-part,
  stacked on one sheet and individually labeled, instead of a single
  block for the merged compound. step/stl/iges are unaffected.
- **`assembly.py`** — added `get_assembly_parts(assy)`, which flattens
  an `Assembly` into `(name, shape)` pairs with each part's geometry
  already moved into its assembly-world location, feeding the `parts`
  param above.
- **`cad_generator.py`** — `_generate_assembly` now calls
  `get_assembly_parts` and threads the result through to
  `exporters.export_all` for the dxf/pdf formats.
- `/generate`, the `/download/*` routes, `storage.upload_export`, and
  the frontend format checkboxes needed no changes — they all operate
  on format name, not view mode.

### Verification status
Same standing caveat as the rest of `exporters.py`: written and
syntax-checked against the cadquery 2.3.1 / OCP API surface pinned in
`requirements.txt`, but not executed against real OCCT geometry (no
network access to install cadquery's native dependencies in this
environment). View orientation in particular (which axis maps to
which side of the page) was worked out algebraically against OCCT's
`HLRAlgo_Projector` convention, not confirmed against a rendered
drawing. Run `pytest tests/test_exporters.py` for real before shipping.

---

## [2.2.1] — Multi-format export is now the default

`exporters.DEFAULT_FORMATS` was `("step", "stl")` — the 2.2.0 export
matrix (IGES/DXF/PDF) was opt-in only, via an explicit `formats: [...]`
list. That undersold the feature: a caller who didn't know the param
existed still got the old two-format behavior. Changed the default to
every supported format: `("step", "stl", "iges", "dxf", "pdf")`.

### Changed
- **`exporters.py`** — `DEFAULT_FORMATS` now lists all five supported
  formats. `validate_formats([])` and `export_all(..., formats=None)`
  both fall through to this constant, so the change is one line plus
  updated comments — no new code paths.
- **`web_app.py`** / **`cad_generator.py`** — docstrings/comments
  describing the old `["step", "stl"]` default corrected to match.
- A caller that wants the old, narrower behavior back still can, by
  passing `formats: ["step", "stl"]` explicitly — nothing about the
  opt-in mechanism changed, only what happens when it's omitted.

### Fixed
- **`tests/test_exporters.py`** — `TestValidateFormats` and
  `TestExportAll.test_export_all_default_formats` asserted the old
  two-format default; updated to expect all five, since they'd
  otherwise fail against the new behavior.

### Verification status
Same standing caveat as the rest of `exporters.py`: this is a one-line
constant change with no new logic, but still unverified against a real
CadQuery/OCP install (no network access in this environment). The
existing `tests/test_exporters.py` (updated above) covers it — run it
for real before relying on this.

---

## [2.2.0] — Tier-1 export formats

Adds the production export matrix on top of the existing STEP/STL
pipeline: exact-geometry IGES, layer-classified DXF for 2D cutting, and
1:1 scale vector PDF technical drawings.

### Added
- **`exporters.py`** — unified multi-format export module.
  - `export_step` / `export_stl` — the existing exports, moved here
    behind one interface (no behavior change).
  - `export_iges` — true boundary-representation IGES via OCP's
    `IGESControl_Writer`, called directly since cadquery 2.3.1's own
    `exporters` module doesn't expose an IGES `ExportType`. No new
    native dependency — OCP already ships with cadquery.
  - `export_dxf` — layer-classified DXF (`PIECE_OUTLINE`, `HOLES`,
    `CENTERLINES`) built from a real horizontal section through the
    solid and written via `ezdxf`, with holes auto-classified from the
    outer boundary by enclosed area.
  - `export_pdf` — 1:1 scale vector PDF with a title block (part name,
    material, scale, bounding-box size, date), built from the same
    section geometry as the DXF export, via `reportlab`.
  - `export_all` — orchestrator; validates the requested format list up
    front (`UnsupportedFormatError`, 422) before doing any geometry
    work.
  - **Scope note**: DXF/PDF are derived from a single horizontal
    section, not full multi-view orthographic projection — correct and
    sufficient for the flat/plate/bracket-style parts this catalog is
    dominated by, but a part whose relevant geometry isn't captured by
    one horizontal cut will only export that one cross-section. See the
    module docstring for the full scope discussion, including why GLTF
    isn't included in this pass.
- **`GenerateRequest.formats`** / **`GenerateResponse`** — `/generate`
  now accepts an optional `formats: ["step", "stl", "iges", "dxf",
  "pdf"]` list (defaults to `["step", "stl"]`, matching prior behavior
  exactly when omitted). Response gains `iges_file`/`dxf_file`/
  `pdf_file` and matching `_url` fields, populated only for formats that
  were actually requested.
- **`/download/iges/{filename}`**, **`/download/dxf/{filename}`**,
  **`/download/pdf/{filename}`** — new download routes, same
  path-traversal-safe resolution (`safe_output_path`) as the existing
  `/download/step` and `/download/stl` routes.
- **`storage.upload_export(local_path, fmt)`** — generic R2 upload for
  any supported format, added alongside (not replacing)
  `upload_step`/`upload_stl`.
- **`exceptions.UnsupportedFormatError`** (422) — a bad/unknown format
  name in the `formats` list fails fast, before any parse or geometry
  work, with a clear client-fixable error.
- Frontend (`web_app.py`'s inline HTML/JS) — format checkboxes next to
  the existing DeepSeek toggle; download links render dynamically for
  whichever formats come back in the response.
- **`tests/test_exporters.py`** — covers format validation, each
  exporter individually, `export_all`, and an end-to-end
  `CADGenerator.generate_from_text(..., formats=[...])` call. Marked
  `requires_cadquery` per the existing convention (`test_cad_templates.py`)
  — written and reviewed against the pinned cadquery 2.3.1/OCP API
  surface but **not executed against real OCCT geometry** in the
  environment this was authored in (no network access to install
  cadquery's native dependencies there). Run this file for real, in an
  environment with the project's actual dependencies installed (the
  existing Dockerfile/CI already has them), before relying on it.

### Changed
- `requirements.txt` / `pyproject.toml` — added `ezdxf==1.3.5` and
  `reportlab==4.2.5`.
- `cad_generator.py` — `_generate_single_part` and `_generate_assembly`
  now return a `dict[str, Path]` keyed by format instead of a fixed
  `(step_path, stl_path)` tuple. Assembly STEP export still goes through
  `Assembly.save()` (preserves named sub-parts); every other
  format — including STEP/STL/IGES/DXF/PDF for single parts — goes
  through `exporters.export_all` on the built shape.

---

## [2.1.0] — Engine components: connecting_rod, crankshaft

Two new CAD templates — the most geometrically complex in this project
so far (multi-body boolean unions, not a single extrude+cut like most
existing templates).

### Added
- **`cad_templates/connecting_rod.py`** — big-end/small-end bosses of
  different diameters, a shank between them, an optional raised center
  rib (the visual signature of a forged I-section rod), and
  representative big-end cap bolt holes. See the module's own docstring
  for exactly what's simplified (no true tapered I-beam cross-section,
  no actual two-piece rod/cap split — a single STEP solid can't
  represent a bolted joint between separate bodies).
- **`cad_templates/crankshaft.py`** — configurable number of throws,
  main journals, offset rod journals, webs with counterweight lobes,
  optional front nose and rear flywheel flange. Default even throw
  spacing (`360°/num_throws`); pass `phase_angles_deg` for a specific
  engine's real firing-order spacing instead.
- Both registered in `cad_templates/__init__.py`'s `TEMPLATES` dict,
  wired into `smart_parser.py`'s keyword detection (`infer_part_type`)
  ahead of the existing generic `shaft`/`rod` keywords they'd otherwise
  be shadowed by, and given real validators
  (`validate_connecting_rod`, `validate_crankshaft` in `validator.py`).
- Test coverage in `tests/test_cad_templates.py` and
  `tests/test_validator.py`, plus both added to `smoke_test.py`'s
  real-pipeline test case list.

### Fixed during development (not shipped bugs — caught before delivery)
- An early `connecting_rod.py` draft used
  `faces(">Z").workplane().moveTo(x, y).hole(d)` to cut the bores —
  which centers its local coordinate system on the selected face's
  center of mass, not the global origin. Harmless for a part symmetric
  about the origin (which is why the existing `flat_plate.py` template
  gets away with the same pattern), but silently wrong for this rod's
  two off-origin, different-sized bosses: the holes would land at the
  wrong location. Fixed by cutting with explicit cutter solids at known
  global coordinates instead. `TestConnectingRod::test_big_end_bore_is_actually_at_the_big_end_not_the_origin`
  regression-tests this directly.
- An early `crankshaft.py` draft built one continuous full-length
  cylinder at main-journal diameter as a "backbone" and unioned the
  throws onto it — which left main-journal-diameter material running
  straight through every throw region as well as the actual main
  bearing surfaces, producing a fat cylinder with bumps instead of an
  actual crank profile. Fixed by building each main journal as its own
  discrete segment, with the offset rod journal/webs only present in
  the gaps between them. `TestCrankshaft::test_main_journal_diameter_does_not_run_through_the_throw_region`
  regression-tests this directly.

### Verification status
Same caveat as every CadQuery-dependent change in this file: written
and syntax-checked, but **not run against a real CadQuery install** in
the environment this was authored in (no native OCCT build available).
Run `pytest -m requires_cadquery` (or the full `make test`) plus
`python smoke_test.py` on your VPS before relying on either template.

---

## [2.0.0] — Professional hardening pass

A structural pass on top of the existing generation engine (parser,
templates, validator, CadQuery pipeline) — none of that logic changed,
this is entirely infrastructure: config, error handling, observability,
security, and testing. See sections below for what moved where.

### Added
- **`config.py`** — centralized `pydantic-settings` configuration.
  Every environment variable the project reads (previously ~15 separate
  `os.getenv` calls across `db.py`, `storage.py`, `deepseek_parser.py`,
  `web_app.py`) is now declared, typed, and validated in one place. A
  typo'd env var name now fails at startup instead of silently resolving
  to "not configured."
- **`logging_config.py`** — structured logging (JSON or console format,
  `LOG_FORMAT` env var) with per-request correlation IDs, replacing
  every `print()` call in the request path. `X-Request-ID` is echoed
  back on every response and threaded through all log lines emitted
  while handling that request.
- **`exceptions.py`** — a real exception hierarchy (`ParseError`,
  `GeometryValidationError`, `GenerationError`, `UnsupportedPartTypeError`,
  `AssemblyError`, `StorageError`) instead of one bare `except Exception`
  around the whole pipeline. FastAPI exception handlers map each to an
  appropriate status code (422 for bad input, 500 for internal failures)
  and never leak a Python traceback into an HTTP response body — full
  detail goes to the logs only.
- **`file_safety.py`** — fixes a real path-traversal vulnerability in
  `/download/step/{filename}` and `/download/stl/{filename}`: the
  filename from the URL is now resolved and confirmed to stay inside
  the output directory before being served, instead of being joined
  onto the output path unchecked.
- **Rate limiting** (`slowapi`) on `/generate` (20/min per IP by
  default) and `/api/keys/generate` (5/hour per IP) — each `/generate`
  call does real OCCT geometry work, and unauthenticated key issuance
  was previously uncapped.
- **`/healthz`** (liveness) and **`/readyz`** (readiness, checks DB
  connectivity) endpoints for container/orchestrator health checks.
- **Pydantic response models** (`GenerateResponse`, `ApiKeyResponse`,
  etc.) on every route — gives accurate OpenAPI docs at `/docs` instead
  of untyped `dict` returns, and FastAPI now validates outgoing
  responses match the documented shape.
- **Test suite** (`tests/`) — previously the only test artifact was
  `smoke_test.py`, a manual script not picked up by any test runner.
  Now: unit tests for the validator and regex parser (no CadQuery
  needed, run everywhere), API integration tests via `TestClient`
  (auth, rate limiting, request validation, path-traversal protection),
  and a CadQuery-gated suite (`pytest -m requires_cadquery`) covering
  template geometry validity and the fillet-degradation / structural-beam
  regressions called out in the "Fixed" sections below. **Written but
  not run against a real CadQuery install** — no network/native-build
  access in the environment this was authored in; run `pytest` on your
  VPS to confirm, same caveat every prior DeepSeek/CadQuery change in
  this file has carried.
- **CI** (`.github/workflows/ci.yml`) — lint (ruff), type check (mypy,
  informational for now), and two test jobs (with/without CadQuery) on
  every push/PR.
- **Tooling** — `pyproject.toml` (ruff + mypy + pytest + coverage
  config), `.pre-commit-config.yaml`, `Makefile` with `make check` /
  `make test` / `make fmt` etc.
- **Non-root Docker user + HEALTHCHECK** in the `Dockerfile`.

### Changed
- `cad_generator.py` — every pipeline stage now raises a typed exception
  on failure instead of falling through to a generic `except Exception`;
  `generate_from_text()`'s external dict-return contract (used by
  `cli.py` and `a2mcp/server.py`) is unchanged, but failures now include
  a stable `error_type` field alongside `error`.
  Also added: a cap on the number of chained `operations` per request
  (`MAX_OPERATIONS_PER_REQUEST = 25`) — an unbounded `operations` list
  was a cheap way to tie up the OCCT kernel for a long time on one
  request.
- `web_app.py` — CORS origins, rate limits, and docs visibility now come
  from `config.settings` instead of hardcoded values; added a request
  logging/correlation-id middleware.
- `db.py` / `storage.py` — read configuration via `config.settings`
  instead of scattered `os.getenv` calls; `db.py` gained
  `check_connection()` for the readiness probe.
- `deepseek_parser.py` — the DeepSeek client now has an explicit request
  timeout (`DEEPSEEK_TIMEOUT_SECONDS`, default 20s). Previously a stalled
  API call blocked the request indefinitely instead of falling back to
  the regex parser the way every other DeepSeek failure mode already did.

### Fixed
- **Path traversal** on the download routes (see `file_safety.py` above)
  — the one concrete security bug found in this pass, not something
  discovered in production. Treat this as sensitive if reporting it.
- **`assembly.py`** — an assembly part with an unsupported/typo'd
  `part_type` previously vanished from the output with zero signal
  anywhere; now logged as a warning so a shrinking assembly is
  diagnosable.

### Unchanged (deliberately)
- Every CAD template (`cad_templates/`), the regex/keyword parser
  (`smart_parser.py`), the DeepSeek prompt/extraction logic, the
  validator's actual geometric rules, and the assembly/x402/MCP gateway
  logic — this pass is infrastructure only. Prior fixes documented
  further down this file (fillet degradation, i-beam/channel flange
  overlap, etc.) still apply and now have regression tests in
  `tests/test_cad_templates.py`.

---

## Prior history

See the sections below this line for the pre-2.0.0 history (DeepSeek
integration, R2/db/security hardening, the three-round fillet bug fix,
the OKX A2MCP gateway) — carried over from the previous README, not
reproduced twice.
