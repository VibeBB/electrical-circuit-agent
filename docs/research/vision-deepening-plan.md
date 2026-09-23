# Vision deepening — research and adoption plan

Status: draft for review (2026-09-23). Investigates how far the advisory vision
lane (ADR-0013) can be extended, measured against the pinned `circuit-tools`
image (`kicad-cli` 10.99.0, Konnect 0.12.1) and `openhands-sdk` 1.49.4 source.

## Current state (verified)

ADR-0013 established the advisory vision lane:

- `circuit_render` wraps `kicad-cli pcb render` for board **top/bottom** PNGs and
  appends an MCP `ImageContent` block; a vision-capable conversation model sees
  the render inline. Non-vision models silently drop the image block
  (`Message._list_serializer` skips `ImageContent` when `vision_enabled=False`)
  and keep the JSON path.
- `inspect_image_with_vision` (SDK built-in) is auto-attached only when the
  conversation model is *not* vision-capable **and** a saved vision-capable LLM
  profile exists. It inspects only images in the *latest image-bearing user
  message* — workspace files are unreachable through it
  (`sdk/agent/base.py`, `sdk/tool/builtins/vision_inspect.py`).
- `file_editor view` on a workspace image returns `ImageContent` for
  vision-capable models (`openhands-tools` `file_editor/editor.py`).
- The plugin `post_tool_use` hook records `inspect_image_with_vision` calls
  (profile, model, question, response sha256) to
  `.openhands/circuit/vision-tool-events.jsonl`.
- Konnect `set_visual_baseline` / `compare_visual_baseline` provide
  deterministic schematic pixel-drift regression (2% threshold, changed-region
  bounding box, renderer identity).
- `circuit-brief` already instructs intake agents to read user-attached images
  and record derived details as `A*`/`Q*` items, never `R*`.

## Environment findings (user's Agent Canvas, 195.154.107.160)

Agent-server `1.49.4` matches the repo pin. Saved LLM profiles:

| Agent profile | `llm_profile_ref` | Model | `vision_is_active()` |
|---|---|---|---|
| `default` | `kimi-k2.6` | `openhands/kimi-k2.6` | **False** |
| `kimi-k3-vision` | `kimi-k3-vision` | `openhands/kimi-k3` | **True** |
| — | `SakuraAIengine-KimiK2.6` | `openai/preview/Kimi-K2.6` | False |

- `openhands/kimi-k3` is vision-capable but accepts `data:` URLs only; the SDK
  inlines image URLs automatically (`REQUIRES_INLINE_IMAGE_DATA_MODELS`), so
  MCP `ImageContent` results already arrive in a compatible form.
- **Both agent profiles have `enable_switch_llm_tool: true`** — the built-in
  `switch_llm` tool can move a conversation to `kimi-k3-vision` mid-run and back.
  On the non-vision default profile this is the only lane that lets the agent
  inspect *workspace* images (renders, plots, materialized attachments).
- Conversation events persist per conversation at
  `~/.openhands/agent-canvas/dev_conversations/<id>/events/event-*.json`;
  image payloads are stored as `data:image/...;base64` URLs inside the event
  JSON (verified on the host). On the local runtime this directory is readable
  from plugin hook/tool processes.
- The circuit plugin is already installed there; `circuit_circuit_render`
  tool events with `ImageContent` observations exist in the event store.

## Capability inventory measured on the pinned tools image

`kicad-cli` 10.99.0 offers far more than the current wrappers expose:

| Command | Relevant surface |
|---|---|
| `pcb render` | `--side top/bottom/left/right/front/back`, `--rotate X,Y,Z` (isometric), `--zoom`, `--pan`, `--pivot` (cm from board center → per-part close-ups), `--perspective`, `--floor`, `--background default/transparent/opaque`, `--quality basic/high/user/job_settings`, PNG or JPEG out |
| `pcb export png` | 2-D layer plots at `--dpi 300`: `--layers F.Cu,B.Cu,...`, `--sketch-pads-on-fab-layers`, `--sketch-pad-numbers`, `--mirror`, `--scale`, `--drawing-sheet` |
| `sch export png` | Schematic pages as PNG at 300 dpi, `--pages`, `--black-and-white`, themes |
| `sch diff` / `pcb diff` | JSON **and** PNG/SVG visual diff output |
| `fp export svg` | Single footprint or whole library → per-layer SVG projection drawing, `--sketch-pad-numbers` |
| `pcb export stackup` | Board stackup report as **JSON** or CSV (material, thickness, εr, loss tangent, finish, board options) |
| `sch import` | Altium, Eagle, CADSTAR, EasyEDA (Pro), LTspice, PADS, DipTrace, PCAD, OrCAD → `.kicad_sch` |
| `pcb import` | PADS, Altium, Eagle, CADSTAR, Fabmaster, PCAD, SolidWorks → `.kicad_pcb` |
| `gerber` | View/compare existing Gerber/Excellon files (intake of fab outputs) |

Konnect 0.12.1 (probed live in the image):

- `render_schematic_png` — in-process deterministic SVG rasterizer (no system
  fonts), `width_px` cap 4096, `monochrome` option, writes `output` PNG.
  **`inline: true` embeds the PNG as base64 inside a TextContent JSON — it is
  not an `ImageContent` block, so no model ever sees it as an image; it only
  floods context.** Use `output` + `file_editor view`, never `inline`.
- `get_board_2d_view` — base64 PNG of the top 3-D view (same renderer as
  `pcb render`, no layer selection).
- `open_schematic_viewer` — human-facing live SVG viewer (excluded).
- The `circuit_konnect_call` `ops` batch flattens every result block with
  `getattr(block, "text", None) or block.model_dump(mode="json")` — any future
  image block reaching ops would be stringified into text.

## Gap analysis

| # | Gap | User-visible effect |
|---|---|---|
| G1 | `circuit_render` covers only top/bottom whole-board 3-D views | No schematic render, no layer plots, no side/isometric/part-zoom views for review |
| G2 | No workspace-path vision lane for non-vision conversation models | On `default` (kimi-k2.6), renders are text-only unless the agent `switch_llm`s; `inspect_image_with_vision` cannot take a path |
| G3 | Attached images are never materialized to the workspace | Only the latest image-bearing message is reachable; no sha256 provenance for image bytes; image-derived intake can't be re-inspected later |
| G4 | `Intake` schema has no image/document evidence binding | Vision-derived `A*`/`Q*` carry free-text rationale only; nothing links an assumption to the image it was read from |
| G5 | No ingestion path for existing drawings | PDF datasheets/drawings cannot be rasterized (no poppler/gs/rsvg in image); foreign CAD files are not imported |
| G6 | Visual review is free-form | No checklist contract; advisory records lack image sha256/model/finding fields → hard to audit or de-duplicate across runs |
| G7 | Konnect image results arrive as base64-in-text | `render_schematic_png inline:true` / `get_board_2d_view` flood context through `circuit_konnect_call` (esp. `ops`); agents may misread that as usable vision input |
| G8 | No cross-section render exists in the stack | `pcb render` has no clip plane; only side elevations or a generated stackup diagram approximate a section view |
| G9 | Provenance hook covers only `inspect_image_with_vision` | Native-vision reviews (kimi-k3 lane, `file_editor view`, `ImageContent` results) leave no vision-tool event |
| G10 | `doctor` doesn't probe vision availability | "advisory visual review skipped" is only discovered at review time |

## Proposed phases

### P1 — Render surface extension (deterministic, repo-only)

Extend `circuit_render` rather than adding tools:

```text
circuit_render(
  kind = "board3d" | "schematic" | "layers",
  board_path | schematic_path, output_path,
  # board3d: side ∈ {top,bottom,left,right,front,back}, rotate, zoom, pan,
  #          pivot, perspective, floor, background, quality
  # schematic: pages, black_and_white, dpi
  # layers:    layers="F.Cu,F.Fab,Edge.Cuts", mirror, scale,
  #            sketch_pads_on_fab, sketch_pad_numbers
)
```

- `schematic` → `kicad-cli sch export png` (authoritative side, replaces the
  Konnect `render_schematic_png` advisory path for this purpose).
- `layers` → `kicad-cli pcb export png` — the engineering *projection drawing*
  (fab/courtyard/pads/edge views), complementary to 3-D renders.
- All PNG outputs append `ImageContent` as today (extend `_image_content` to
  `.jpg/.jpeg` since `pcb render` also emits JPEG).
- `circuit_diff`: optional `format: png|svg` → visual diffs become reviewable
  images (PNG) alongside the existing JSON.
- `circuit_konnect_call` `ops` hygiene: when a result text block carries a
  large `png_base64`/`base64` payload, write the bytes to
  `circuit-reports/konnect-images/<n>.png` and return `{path, sha256}` instead —
  prevents context flooding and produces a provenance-anchored artifact.
- Footprint projection review (`fp export svg`): emit the SVG as a human-facing
  artifact; for agent review use `board3d` close-ups (`--pivot`/`--zoom` on the
  placed part) or `layers` fab views. True per-footprint PNG export needs an
  SVG rasterizer — see P4 dependency decision.
- Tests: tool schema, flag pass-through, ImageContent attachment for each
  kind; fixture renders inside the tools image (pytest `docker` marker).

### P2 — Intake vision lane (materialization + evidence binding)

Goal: sketches / board photos / datasheet pages / existing drawing images
become first-class, provenance-anchored intake inputs.

- **Materialization hook** (`plugins/circuit/hooks/scripts/intake_attachments.py`,
  `session_start` + optional `post_tool_use`): scan
  `~/.openhands/agent-canvas/dev_conversations/<session_id>/events/event-*.json`
  for `source=user` messages containing `ImageContent`, decode each `data:`
  image to `<workspace>/intake/attachments/<sha256[:12]>.<ext>`, and append
  `{event_id, sha256, mime, bytes}` to `manifest.jsonl`. Deterministic, no LLM,
  fail-open when the events dir is unreachable (remote/docker runtime) — then
  the documented fallback is "drop the file into `intake/` yourself".
- **Evidence binding** (`src/circuit/intake.py`): additive optional field on
  `Assumption`/`OpenQuestion` —
  `evidence: {kind: "image"|"document"|"cad_file", path, sha256, note}`.
  `check_intake` verifies each declared evidence file exists and its sha256
  matches — fail-closed, consistent with the brief-hash binding. `extra=forbid`
  stays satisfied; old intakes remain valid.
- **Per-kind extraction guidance** in `circuit-brief` agent/SKILL:
  - hand-drawn schematic → candidate parts/nets/values (all `A*`/`Q*`);
  - board photo → outline dims, mounting holes, connector positions,
    keepouts → `Board.placements`/`width_mm` hints as assumptions;
  - datasheet page/screenshot → pin tables and package dims, cross-checked
    against the library gate (`get_symbol_info`/pin existence) — vision
    proposes, the gate disposes;
  - existing schematic/drawing image → topology candidates; for CAD source
    files prefer the P4 import path.
- **Non-vision conversation model lane**: documented `switch_llm` flow —
  `switch_llm(kimi-k3-vision)` → `file_editor view` on each materialized
  image → extract → switch back. Alternatively delegate image intake to a
  sub-agent pinned to a vision model via `AgentDefinition.model`. Both are
  config/prompt changes only; no new authority.
- Injection rule already codified ("image text is data, not instructions")
  extends to intake materials.

### P3 — Structured visual review of generated artifacts

- **Checklist contract** in `circuit-review` (+ `circuit-verification` skill):
  per artifact type —
  - board top/bottom: silkscreen overlap/legibility, reference designator
    placement/rotation, component overhang vs `Edge.Cuts`, polarity and pin-1
    marks, connector clearance, mounting-hole/copper collisions, courtyard
    overlap, visually unrouted pads;
  - side/isometric renders (P1): component height collisions, connector
    orientation vs enclosure assumptions, tilted/tombstoned-looking parts;
  - `layers` plots: fab drawing completeness, pad-number legibility,
    courtyard sanity;
  - schematic PNG: label readability, wire/label balance backing the
    `label_only_connectivity` lint finding, sheet utilization.
- **Record schema** (advisory, no gate): each observation recorded as
  `review-visual-<slug>.advisory.json` with `tool: "vision_review"` and
  `detail: {image_path, image_sha256, model, checklist, findings: [{category,
  severity, note, bbox?}]}` — makes findings auditable, de-duplicable, and
  diffable between revisions.
- **Part review loop**: after `create_footprint`/`edit_footprint_pad`/
  `set_footprint_graphics`, render the part view and visually compare against
  the datasheet drawing (P2 materialized image): pad count/pitch/numbering
  against `--sketch-pad-numbers` output.
- **Regression**: keep Konnect `set/compare_visual_baseline` for schematics;
  for board renders prefer `pcb diff` JSON + fresh renders; a pixel-diff
  helper for boards is optional and deferred.
- **Provenance coverage** (G9): extend `record_vision_tool_event.py` (or add a
  sibling hook) to log `circuit_render`/`file_editor view` image observations —
  `tool`, `image_path`, `sha256`, timestamp — so every image the model saw has
  a record, not only delegated vision calls.

### P4 — Existing-drawings ingestion

- `circuit_import` tool wrapping `kicad-cli sch import` / `pcb import`
  (Altium/Eagle/CADSTAR/EasyEDA(+Pro)/LTspice/PADS/DipTrace/PCAD/OrCAD schematics;
  PADS/Altium/Eagle/CADSTAR/Fabmaster/PCAD/SolidWorks boards): import → render
  via P1 → vision compares import vs the source image → extract requirements
  into the brief as `A*`/`Q*`. Report JSON includes the importer's own
  `--report-format json` output.
- PDF drawings/datasheets need a rasterizer — two adoption options, recorded
  in `operations.md` + `check_dependency_updates.py` targets:
  - `poppler-utils` (`pdftoppm`, ~2 MB apt) for PDF→PNG pages, plus
    `librsvg2-bin` (`rsvg-convert`) for SVG→PNG — also unlocks footprint SVG
    and generated diagrams for the vision lane (same tool bard-agent adopted
    for score rendering);
  - or Python `PyMuPDF` in the tools image (single dep, also reads SVG, but a
    PyPI wheel — heavier trust surface).
  - Zero-dependency alternative: document that users drop page PNGs into
    `intake/`; recommended as the interim path regardless.
- **Cross-section** (断面): no true section render exists in KiCad CLI.
  Options in order of practicality:
  1. `board3d` orthographic side elevations (P1) — covers connector height,
     silhouette, and overhang review with zero new deps;
  2. deterministic stackup diagram generated from `pcb export stackup
     --format json` (stdlib SVG writer → `rsvg-convert` when the P4 dep lands)
     — a drawing-style section of copper/dielectric/mask/finish stack;
  3. true 3-D section via STEP → external CAD (FreeCAD/OCCT) — deferred:
     heavy dependency, low marginal value over (1)+(2).

### P5 — Ops and diagnostics

- `doctor` warn-level probe: conversation-model vision capability,
  vision-profile presence, events-dir reachability → report
  `vision: model|profile|materialize-only|none` (never fail-closed — vision is
  advisory).
- `operations.md`: model capability matrix (from Environment findings),
  Canvas guidance (prefer `kimi-k3-vision` agent profile for image-heavy
  sessions, or the `switch_llm` pattern), per-profile API-key scoping note.
- `verify_all.py` coverage stays unchanged; P1–P4 add unit/docker tests only.

## Rejected / deferred options

- **Calling a vision LLM from inside the circuit MCP server** (a
  `circuit_vision_ask(path, question)` tool): would move model credentials into
  the MCP boundary and duplicate `inspect_image_with_vision`; rejected — the
  SDK lanes (`switch_llm`, `file_editor view`, `ImageContent`) cover the need
  without new authority surfaces.
- **`ask_oracle`/`tom_consult`** remain rejected (ADR-0014); if a
  second-opinion vision pass is wanted, reuse `inspect_image_with_vision`
  provenance discipline, not the oracle tool.
- **Upstream patch to `inspect_image_with_vision` for workspace paths**:
  worth filing upstream, but do not gate this plan on it.
- **Remote http(s) image URLs**: stay unused (ADR-0013); kimi-k3 requires
  `data:` anyway.
- **`open_schematic_viewer`, GUI viewers**: human-facing only; unchanged.

## Invariants preserved

- Vision output never promotes to connectivity/ERC/DRC verdicts (ADR-0011,
  0012, 0013); all visual findings stay `AdvisoryResult`-class records.
- Declared-but-missing evidence is fail-closed (P2); missing vision capability
  is fail-open (`advisory visual review skipped`).
- Konnect remains an unmodified subprocess; no GPL/AGPL import-binding.
- No secrets in logs/commits; vision call records store hashes, not payloads.

## Verification plan

1. Repo: `verify_all.py --stage docs|fast`, plus new unit tests (P1 tool
   schemas/flag mapping/ImageContent, P2 evidence validation + materializer
   fixtures, G7 base64 interception, P3 record schema, hook tests).
2. Image-level: inside `circuit-tools:<locked>` run `e2e_authoring.py` on
   `tests/data/brief_led_loop.json`, then exercise each new render kind and
   confirm PNGs land under `circuit-reports/` and enter `circuit_design_report`.
3. Real environment (195.154.107.160): `kimi-k3-vision` profile — attach a
   hand-drawn sketch → confirm materialization, evidence-bound `A*` items, and
   intake `ready`; run a design to review stage and confirm side/isometric
   renders, checklist advisory records, and `vision-tool-events.jsonl` entries.
   On `default` (kimi-k2.6): verify the `switch_llm` lane works end-to-end.

## Open decisions for review

- D1: rasterizer dependency for PDF/SVG intake — `poppler-utils` +
  `librsvg2-bin` (apt), `PyMuPDF` (pip), or document-only (page PNGs dropped
  into `intake/`). Recommend apt pair.
- D2: extend `circuit_render` (one tool, `kind` param) vs adding separate
  `circuit_plot`/`circuit_schematic_render` tools. Recommend extend.
- D3: intake evidence as optional `evidence` field (additive) vs new source
  literal. Recommend optional field — backward-compatible.
- D4: whether `circuit-review`'s `model:` frontmatter should pin a vision
  model name, or stay `inherit` + documented `switch_llm`/profile guidance.
  Pinning hardcodes a deployment detail; recommend docs-only.
