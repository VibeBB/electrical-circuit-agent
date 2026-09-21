# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-21

First public release.

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
