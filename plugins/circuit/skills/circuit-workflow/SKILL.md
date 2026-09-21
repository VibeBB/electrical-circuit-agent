---
name: circuit-workflow
description: Execute a KiCad design workflow with explicit board server lifecycle and deterministic verification.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - KiCad design
  - circuit workflow
  - 回路設計
  - 基板設計
---

# Circuit workflow

Clarify requirements, identify the project and source paths, and assign the brief-intake
agent first. Resolve open questions, require an `ready` intake report and a `pass` library
report, then assign schematic, layout, and review work through the SDK task tools. A headless API server handles one
`.kicad_pcb` at a time. Start it before IPC operations and stop it before switching
boards. Use Konnect live IPC for interactive edits; use direct file editing only when
the operation does not require an open board. Write and validate a design brief,
author schematic nets with labels rather than crossing pin-to-pin wires, and run the
authoritative netlist connectivity check before ERC. Save before running ERC or DRC,
and use the circuit MCP server as the deterministic verification boundary.
