# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Changed

- Docker-only runtime: `circuit_launcher.py` no longer falls back to a local
  `docker build` when no pinned image resolves — `$CIRCUIT_TOOLS_IMAGE` or a
  digest lock (`tools-image.json` / `docker/image-digests.json`) is now
  required, and a failed pull is an error.

### Added

- `skills/circuit-brief-rules`: path-triggered rule (`*.brief.json`,
  `*.intake.json`) injecting design-brief contract and intake-provenance
  reminders deterministically — the same PathTrigger mechanism mech uses.
- `circuit-brief` and `circuit-review` declare the `record-vision-tool-event`
  post hook in frontmatter (plugin hooks do not propagate to task sub-agents).

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
