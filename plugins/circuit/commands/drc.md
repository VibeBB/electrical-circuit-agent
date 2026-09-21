---
description: Run deterministic KiCad DRC and report its JSON verdict.
argument-hint: <board.kicad_pcb>
allowed-tools:
  - terminal
---

Call the `circuit_drc` MCP tool with the board path. Report the returned JSON verdict
and report path verbatim. Missing tools, process errors, or malformed JSON are
fail-closed.
