# ADR-0001 Positioning as an independent lightweight product

- Status: Accepted
- Date: 2026-09-20
- Related: `README.md`

## Context

We provide a conversational experience for schematic and PCB design, but
taking in acd-agent's design graph and three-layer gates as-is would lose an
independent distribution unit and responsibility boundary.

## Decision

`circuit-agent` is kept independent as a lightweight OpenHands plugin and KiCad
tools image. ERC/DRC verdicts are determined solely by deterministic results
from `kicad-cli` JSON. The ACD-specific Design Graph, Evidence, and L1-L3 gate
mechanisms are not adopted.

## Consequences

OpenHands conversation/delegation and KiCad deterministic checks can be
combined within a small boundary. Future feature additions will not depend on
ACD mechanisms across this boundary.
