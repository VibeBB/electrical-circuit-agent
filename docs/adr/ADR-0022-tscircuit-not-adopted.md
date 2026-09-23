# ADR-0022 tscircuit evaluated and not adopted

## Status

Accepted

## Context

tscircuit (https://tscircuit.com, MIT-licensed, started 2024-03) is a
React/TypeScript toolchain where circuits are written as JSX
(`<resistor/>`, `<chip/>`, `<trace/>` inside `<board/>`) and rendered
through React Fiber into schematic, PCB, and 3D views. Its pipeline is:

- `@tscircuit/core` evaluates TSX into **Circuit JSON**, a single JSON
  representation that is the hub for all checks, views, and exporters.
- `@tscircuit/checks` runs placement, netlist, pin-specification, and
  routing DRC; `tsci check` validates artifacts partially.
- `@tscircuit/capacity-autorouter` (MIT) autoroutes in-process, and
  Specctra DSN export targets external routers such as FreeRouting.
- `tsci simulate analog` generates a SPICE netlist and runs `spicey` or
  `ngspice`.
- `tsci export` emits Gerbers, STEP, `kicad_pcb`/KiCad project, DSN,
  SPICE, netlists, BOM, pick-and-place, and images;
  `circuit-json-to-kicad` also writes hierarchical `.kicad_sch`.
- `tsci dev` gives live browser preview; the `tscircuit.com` registry
  plus a JLCPCB parts engine supply components, and `tsci convert`
  imports `.kicad_mod` to TSX.
- It positions itself for AI-generated electronics (agent skills, AI
  footprint generation).

## Comparison against this stack

| Axis | This repository | tscircuit |
|---|---|---|
| Source of truth | Native `.kicad_sch` / `.kicad_pcb` | TSX compiled to Circuit JSON |
| Verdicts | `kicad-cli` JSON only (ERC/DRC/jobset) | `@tscircuit/checks` (narrower rule set) |
| Autorouting | FreeRouting (mature Specctra flow) | New capacity autorouter; DSN out (known export defects reported upstream) |
| Simulation | Via KiCad/ngspice ecosystem | `tsci simulate` built-in |
| Parts/libraries | CERN KiCad libraries, pinned, gated | Web registry + JLCPCB parts engine |
| Authoring | Konnect advisory MCP + `e2e_authoring.py` | Deterministic TSX, agent-friendly diffs |
| Runtime | Digest-pinned Docker image, headless | Node/browser, no image required |
| Licensing | BSD-3 + Konnect AGPL separation | Uniform MIT |

## Decision

tscircuit is not adopted. The repository's invariants require ERC/DRC and
connectivity verdicts from `kicad-cli` JSON on native KiCad files, and the
KiCad toolchain (jobset manufacturing outputs, CERN library depth,
FreeRouting maturity, Konnect's 234-tool authoring surface) exceeds
tscircuit's current rule coverage and conversion fidelity — upstream
issues show schematic coordinate-precision and DSN-export defects.

tscircuit is recorded here as a monitored alternative. Its strengths —
git-diffable code-first authoring, live preview, and agent-oriented
registry — match directions this product may revisit.

Revisit if: (a) verdicts are re-scoped to allow converter-generated
KiCad files gated by `kicad-cli`, or (b) tscircuit reaches KiCad-parity
DRC and stable KiCad/DSN round-tripping.

## Consequences

- No dependency or runtime change; the KiCad/Konnect/FreeRouting
  boundary is unchanged.
- This ADR is the durable record of the comparison so the evaluation
  need not be repeated.
