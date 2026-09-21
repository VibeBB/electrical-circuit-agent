# ADR-0012 Adoption of kicad-cli jobset, render, and diff

## Status

Accepted

## Decision

KiCad 11 nightly's `kicad-cli jobset run`, `pcb render`, `sch diff`, and
`pcb diff` are adopted as authoring evidence. A jobset declaratively reproduces
ERC, DRC, netlist, schematic PDF, and manufacturing outputs, and self-
consistency is checked by comparing against directly executed ERC/DRC JSON.

Render PNGs and diff JSON are stored in the DesignReport as auxiliary evidence
for human review. The deterministic verdicts for connectivity, ERC, and DRC
continue to rely solely on the netlist and direct kicad-cli JSON; render/diff
and Konnect descriptions are never promoted to verdicts.

## Consequences

The jobset output destination is bound to the caller's workspace; missing
files, abnormal exits, and JSON parse failures are fail-closed. Jobset and
direct-run ERC/DRC are compared excluding the generated timestamp, and a
mismatch fails the self-consistency gate.

## Rationale

In `/home/ubuntu/kicad-cli-probe/results.md`, the 10 jobset jobs, headless
render, and diff exit codes 0/5 were measured on KiCad 10.99 nightly. PNGs and
diffs improve review reproducibility but do not guarantee electrical
correctness, so they are limited to auxiliary evidence.
