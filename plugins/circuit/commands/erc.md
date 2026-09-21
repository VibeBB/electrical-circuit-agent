---
description: Run deterministic KiCad ERC and report its JSON verdict.
argument-hint: <schematic.kicad_sch>
allowed-tools:
  - terminal
---

Call the `circuit_erc` MCP tool with the schematic path. Report the returned JSON
verdict and report path verbatim. Missing tools, process errors, or malformed JSON
are failures and must not be replaced by an agent judgement.
