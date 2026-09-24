# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Changed

- Generated sheets fit their frame: `inject_title_block` wraps over-long
  `comments=` into consecutive `(comment N ...)` fields so no printed
  comment line overruns the sheet frame, and e2e authoring picks the
  smallest `paper` size that covers the placement grid
  (`titleblock.paper_for_part_count`, exposed also as
  `titleblock.set_paper_size`).
- `circuit-schematic` hardens the wiring policy — `label_only_connectivity`
  is an authoring defect, one `power:PWR_FLAG` per driven rail at its
  source — and documents the Konnect repair path for ERC violations
  (undriven pins → `add_schematic_component` + `connect_to_net`, floating
  pins → wire ops, duplicate references → `annotate_schematic`).
- Docker-only runtime: `circuit_launcher.py` no longer falls back to a local
  `docker build` when no pinned image resolves — `$CIRCUIT_TOOLS_IMAGE` or a
  digest lock (`tools-image.json` / `docker/image-digests.json`) is now
  required, and a failed pull is an error.

### Added

- `circuit_sch_lint` gains a `power_flag_crowded` warning when two
  `power:PWR_FLAG` symbols sit closer than 15 mm — one flag per rail at
  its source reads as intent, a pile reads as a patch.
- `skills/circuit-brief-rules`: path-triggered rule (`*.brief.json`,
  `*.intake.json`) injecting design-brief contract and intake-provenance
  reminders deterministically — the same PathTrigger mechanism mech uses.
- `circuit-brief` and `circuit-review` declare the `record-vision-tool-event`
  post hook in frontmatter (plugin hooks do not propagate to task sub-agents).
- Drawing-quality review: `circuit-review` now reviews every rendered sheet
  on baseline fidelity (accurate, legible, unambiguous), manufacturing
  completeness (title block, fab/drill/assembly notes, silkscreen as
  assembly instruction), and design intent (functional grouping, signal
  flow, power/ground topology, decoupling proximity).
  `VisualReviewDetail` gains a required `impression` field — the
  reviewer's subjective reading of the drawing — and four shared
  categories: `ambiguous_notation`, `missing_dimension`,
  `missing_manufacturing_info`, `design_intent`.
- Generated sheets now carry design intent, not just connectivity:
  `inject_title_block` accepts `comments=` (numbered KiCad
  `(comment N "...")` fields), and e2e authoring writes the brief
  `description` into `comment 1` so the sheet explains itself to a
  reader without the brief. `circuit-schematic` documents the intent
  practices it should author (functional blocks, left-to-right signal
  flow, ground-down/supply-up, decoupling proximity, single-point
  grounds drawn as topology, notes for non-obvious decisions), and
  `circuit-layout` covers silkscreen/fab-layer documentation.
- `sch_lint` gains three warnings on the same axes the reviewer uses:
  `junction_missing` (a wire endpoint taps mid-run with no junction
  dot — reads as a pass-through), `label_off_wire` (a net label on no
  wire and near no symbol — reads as a connection that exists nowhere),
  and `notes_absent` (no text notes and no title-block comments — the
  sheet explains nothing about itself).

### Fixed

- `scripts/check_plugin_load.py` renders the OK summary from the actual
  expected asset sets instead of a hardcoded string that could drift.


### Added

- MCP tool metadata: every `circuit_*` tool now carries
  `annotations.title` plus `readOnlyHint` / `destructiveHint` /
  `idempotentHint` / `openWorldHint` so MCP clients (including
  AgentCanvas) can gate calls on honest write semantics.
  `circuit_konnect_call` is the only `destructiveHint: true` tool
  (arbitrary Konnect ops mutate the live board).

### Changed

- `openhands-sdk` / `openhands-tools` pins `1.49.4` → `1.49.5`.
- `kicad-nightly-symbols` pin `202609221218+1565b6644~12~ubuntu26.04.1`
  → `202609231218+2ad44fc37~12~ubuntu26.04.1` (librarian URL + SHA-256;
  `_cvpcb.kiface` ERC re-test verified, see `docs/operations.md`).

## [0.1.0] — unreleased


First public release (in preparation; no git tag published yet).

### Added

- OpenHands plugin `plugins/circuit` with brief/schematic/layout/review
  sub-agents, commands, hooks, and skills.
- `circuit` runtime MCP server (`python3 -m circuit.mcp_server`) for
  deterministic KiCad operations.
- KiCad 11 nightly headless operation via `kicad-cli api-server` IPC, with no
  GUI or Xvfb.
- Konnect v0.12.1 bundled as an unmodified separate MCP process, preserving the
  AGPL-3.0-only boundary.
- Design brief authoring flow with intake provenance sidecar, library gate, and
  netlist connectivity gate.
- ERC/DRC verdicts determined solely from `kicad-cli` JSON output
  (fail-closed).
- Digest-locked GHCR images (`circuit-tools`, `circuit-server`) via
  `docker/image-digests.json`.
- `kicad-cli` jobset run, `pcb render`, and `sch`/`pcb diff` as auxiliary
  authoring evidence.

[Unreleased]: https://github.com/VibeBB/electrical-circuit-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/VibeBB/electrical-circuit-agent/releases/tag/v0.1.0
