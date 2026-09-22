# ADR-0009 Design brief and connectivity gate

- Status: Accepted
- Date: 2026-09-20

## Decision

Design intent generated from conversation is stored as a machine-readable
design brief with parts, nets, and board, and serves as the input to KiCad
schematic authoring. Konnect's analysis results are used for steering and
diagnosis, but the connectivity verdict is decided only by a deterministic gate
that compares the netlist produced by `kicad-cli sch export netlist` against
the design brief.

## Rationale

Drawing wires directly between pins can pass through intermediate component
pins and unintentionally merge a different net. Placing net labels on each pin
endpoint expresses design intent independently of component placement. By
comparing the netlist that KiCad itself outputs, we verify the actual
connections in the saved schematic rather than LLM or Konnect descriptions.

## Consequences

The connectivity gate, the schematic readability lint, ERC, DRC, and outputs
are consolidated into a single design report. If any of connectivity,
sch_lint, ERC, or DRC was not run or did not pass, the design report fails
closed (ADR-0015). DRC is recorded as evidence of the authoring flow; even
when connectivity and ERC pass, a DRC failure is not hidden.
