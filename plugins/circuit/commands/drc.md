---
description: Run deterministic KiCad DRC and report its JSON verdict.
argument-hint: <board.kicad_pcb>
allowed-tools:
  - terminal
---

For boards containing connectors, first call `circuit_connector_placement_check`
with the board path and a reference-to-PartSpec map. Resolve every placement
finding and unknown-envelope HumanRequest before continuing.

Call the `circuit_drc` MCP tool with the board path. Report the returned JSON verdict
and report path verbatim. Missing tools, process errors, or malformed JSON are
fail-closed.
