# electrical-circuit-agent documentation index

| Document | Contents |
|---|---|
| [`../README.md`](../README.md) | Product overview (English + 日本語) |
| [`../AGENTS.md`](../AGENTS.md) | Working contract |
| [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) | Third-party licenses and pins |
| [`architecture.md`](architecture.md) | Responsibility boundaries, execution sequence, records + liaison layers |
| [`workflow.md`](workflow.md) | Stage-by-stage workflow: tools, gates, records left, vision points |
| [`agents.md`](agents.md) | The 7 sub-agents: models, tools, hooks, records duty |
| [`skills.md`](skills.md) | The 9 skills |
| [`commands.md`](commands.md) | The agent-facing slash commands |
| [`mcp.md`](mcp.md) | All 60 circuit MCP tools: purpose, inputs, access, inline images |
| [`hooks.md`](hooks.md) | Every plugin and agent-frontmatter hook |
| [`contracts.md`](contracts.md) | Every JSON artifact: shape, producer/consumer, strictness |
| [`records-and-vision.md`](records-and-vision.md) | VRP for circuit: stages, decisions, globs, vision points |
| [`sister-cooperation.md`](sister-cooperation.md) | SLP v2 rules/states and the interchange table |
| [`performance-and-limits.md`](performance-and-limits.md) | Real numeric constants from the code |
| [`operations.md`](operations.md) | Build, smoke, CI checks, dependency updates |
| [`development.md`](development.md) | Setup, verify_all stages, test commands, release |
| [`test-coverage.md`](test-coverage.md) | C0/C1/C2/MCC/MC/DC and boundary coverage, floors, test-design techniques |
| [`modules.md`](modules.md) | Every `src/circuit` module and its public API |
| [`improvement-notes.md`](improvement-notes.md) | Implemented items vs remaining ideas |
| [`konnect-tools.md`](konnect-tools.md) | Konnect v0.13.0 complete tool coverage matrix |

## Accepted ADR list

| ADR | Title |
|---|---|
| [0001](adr/ADR-0001-independent-product.md) | Positioning as an independent lightweight product |
| [0002](adr/ADR-0002-kicad11-nightly-headless.md) | KiCad 11 nightly and headless IPC |
| [0003](adr/ADR-0003-konnect-agpl-boundary.md) | Adoption of Konnect and the AGPL boundary |
| [0004](adr/ADR-0004-plugin-digest-image.md) | Plugin and digest-pinned Docker image |
| [0005](adr/ADR-0005-openhands-delegation.md) | SDK delegation mechanism |
| [0006](adr/ADR-0006-library-policy.md) | KiCad library policy |
| [0007](adr/ADR-0007-plugin-mcp-lifecycle.md) | Plugin MCP configuration and api-server lifecycle |
| [0008](adr/ADR-0008-cicd-ghcr-digest-lock.md) | GHCR publishing and digest-pinned lock |
| [0009](adr/ADR-0009-design-brief-connectivity-gate.md) | Design brief and connectivity gate |
| [0010](adr/ADR-0010-brief-intake-provenance-and-library-gate.md) | Brief intake provenance and library gate |
| [0011](adr/ADR-0011-konnect-advisory-layer-and-coverage-matrix.md) | Konnect advisory layer and coverage matrix |
| [0012](adr/ADR-0012-kicad-cli-jobset-render-diff.md) | Adoption of kicad-cli jobset, render, and diff |
| [0013](adr/ADR-0013-advisory-vision-review.md) | Advisory vision review via SDK ImageContent |
| [0014](adr/ADR-0014-oracle-consult-tools-not-adopted.md) | Oracle and consult tools not adopted |
| [0015](adr/ADR-0015-konnect-authoring-and-sch-lint-gate.md) | Konnect-only authoring and the schematic readability gate |
| [0016](adr/ADR-0016-extended-render-surface-and-konnect-image-materialization.md) | Extended render surface and Konnect image materialization |
| [0017](adr/ADR-0017-intake-attachment-materialization-and-evidence-binding.md) | Intake attachment materialization and evidence binding |
| [0018](adr/ADR-0018-structured-visual-review-records.md) | Structured visual review records and image-observation provenance |
| [0019](adr/ADR-0019-existing-drawings-ingestion-and-rasterizer-deps.md) | Existing-drawings ingestion, rasterizer dependencies, and the stackup section diagram |
| [0020](adr/ADR-0020-vision-lane-diagnostics.md) | Vision-lane diagnostics in doctor |
| [0021](adr/ADR-0021-openj9-freerouting-runtime.md) | Semeru OpenJ9 and FreeRouting runtime in the tools image |
| [0022](adr/ADR-0022-tscircuit-not-adopted.md) | tscircuit evaluated and not adopted |
| [0023](adr/ADR-0023-firmware-pinmap-interchange.md) | Firmware pin map interchange with firmware-agent |
| [0024](adr/ADR-0024-attest-published-tools-images.md) | Attest published tools images |
| [0025](adr/ADR-0025-part-library-evidence-authority.md) | Part library evidence authority |
| [0026](adr/ADR-0026-library-human-review-gate.md) | Human-reviewed library packets and hash-bound approval |
| [0027](adr/ADR-0027-pinout-orientation-oracles.md) | Pinout orientation oracles |
| [0028](adr/ADR-0028-tool-managed-vision-and-blind-authoring.md) | Tool-managed vision and blind PartSpec authoring |
| [0029](adr/ADR-0029-layered-rule-profiles-and-footprint-lineage.md) | Layered rule profiles and footprint lineage |
| [0030](adr/ADR-0030-KLC-test-board-and-vision-oracles.md) | KLC, test-board, and vision oracles |
| [0031](adr/ADR-0031-step-model-generation-and-inspection.md) | Deterministic STEP model generation and inspection |
| [0032](adr/ADR-0032-library-release-evidence.md) | Golden-corpus, mutation, and escape-rate release evidence |
| [0033](adr/ADR-0033-human-requests-and-confidential-datasheets.md) | Human requests, confidential datasheets, and library-authoring stops |
| [0034](adr/ADR-0034-records-liaison-docs.md) | VRP v1, SLP v2, and the documentation split |
| [0035](adr/ADR-0035-iso7200-drawing-sheet.md) | ISO 7200 drawing sheet and title-block data from the brief |
| [0036](adr/ADR-0036-structural-coverage.md) | Structural coverage gate (C0, C1, C2, MC/DC, boundaries) |

## Research

- [SDK v1.50.1 feature evaluation](research/sdk-v1.50.1-feature-evaluation.md) — OpenHands SDK/tools adoption decisions
