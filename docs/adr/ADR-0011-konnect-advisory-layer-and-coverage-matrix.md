# ADR-0011 Konnect advisory layer and coverage matrix

## Context

Konnect v0.12.1 provides 234 MCP tools spanning schematic, PCB, verification,
review, and manufacturing. Managing them only through individual experiments
and agent prompts makes it easy to overlook unused tools, IPC dependencies,
fixture dependencies, and external service dependencies. On the other hand,
promoting Konnect descriptions or analysis results to ERC/DRC verdicts would
break the responsibility boundary with the deterministic KiCad gates.

## Decision

`plugins/circuit/skills/circuit-konnect/references/konnect-tools.json` is the
single source of truth for the 234 tools, and each tool is assigned a category,
role, stage, IPC requirement, and notes. `docs/konnect-tools.md` is generated
from this matrix.

In E2E authoring, Konnect calls for schematic, layout, review, and
manufacturing are recorded in the design report as `AdvisoryResult`. Advisory
failures do not stop the normal authoring gates; they are saved as
`status=error` or `not_applicable`. Connectivity, ERC, and DRC verdicts are
always determined from `kicad-cli` JSON only, and advisory results never change
verdicts.

Mutating advisory tools use `dry_run` wherever possible; comparisons and
exports that cannot be dry-run write to a duplicated workspace or a dedicated
`konnect-exports/` directory. Manufacturing exports are auxiliary artifacts for
comparison against the authoritative kicad-cli exports; Konnect output is never
the sole manufacturing evidence. JLCPCB/parts database, GUI-only, editor
navigation, user config, and destructive rename tools are marked `excluded` in
the coverage.

## Consequences

- Coverage, IPC prerequisites, and usability of all 234 tools are maintained as
  reviewable data.
- Agent and E2E advisory evidence can be tracked with a consistent JSON schema.
- Even with temporary Konnect malfunctions or unsupported tools, the
  fail-closed semantics of the kicad-cli gates are preserved.
- Exhaustive advisory execution increases E2E time and artifact volume.
  Fixture-dependent tools record their prerequisites in the matrix notes.
