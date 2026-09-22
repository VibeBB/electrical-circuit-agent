# ADR-0015 Konnect-only authoring and the schematic readability gate

- Status: Accepted
- Date: 2026-09-22

## Decision

`.kicad_sch` and `.kicad_pcb` files are authored only through Konnect MCP
operations. The `protect-libraries` `pre_tool_use` hook additionally denies
`file_editor` / `apply_patch` writes to those suffixes (view/read actions are
still allowed), and the `circuit-schematic`, `circuit-layout`, and
`/circuit:design` contracts forbid hand-written s-expressions and generator
scripts that emit design files.

A deterministic schematic readability lint (`circuit.sch_lint`) runs between
authoring and ERC and joins the design report as a fail-closed gate. It flags
unparseable input (fail-closed), positioned items outside the sheet's paper
bounds, symbol `Reference`/`Value` properties placed more than 30 mm from
their symbol, and hidden symbol properties (warning only).

`circuit_erc` / `circuit_drc` skip re-running `kicad-cli` when the source
file and the KiCad version are unchanged: a `<output>.src_sha256` sidecar
records the input hash, and a cached report is reused only when the hash
still matches.

## Rationale

Real-machine verification showed the agent escaping to self-authored
s-expressions whenever file editing met friction, producing schematics whose
symbol properties all landed at the sheet origin — a defect invisible to the
connectivity gate and to ERC — and files that `kicad-cli` itself could not
parse. The Konnect IPC layer already writes correct coordinates, so the fix
is to make it the only authoring path and to make readability mechanically
checkable without relying on a vision-capable model (ADR-0013). A cached
gate also removes the redundant ERC re-runs that consumed the agent budget
on unchanged inputs.

## Consequences

- The design report gate set is now connectivity, sch_lint, ERC, DRC;
  `gate not executed: sch_lint` fails the report closed.
- `e2e_authoring.py` runs `circuit.sch_lint` before the netlist export and
  records the report under `circuit-reports/`.
- `circuit_sch_lint` is exposed as an MCP tool returning the lint report.
- Hidden properties are recorded as warnings; they do not fail the gate but
  are visible in the report for review.
