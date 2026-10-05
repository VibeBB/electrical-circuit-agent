# Commands

Agent-facing slash commands under `plugins/circuit/commands/`:

| Command | File | Purpose |
|---|---|---|
| `/circuit:doctor` | `doctor.md` | Probe the KiCad/Konnect execution environment (tools, versions, socket dir, CERN libs, vision lane) |
| `/circuit:design` | `design.md` | Orchestrate brief → library → schematic → layout → review → export; checks `circuit_ux_inbox` at start and per stage and answers every circuit liaison request |
| `/circuit:erc` | `erc.md` | Run the electrical rules check on a schematic via `circuit_erc` |
| `/circuit:drc` | `drc.md` | Run the design rules check on a board via `circuit_drc` |
| `/circuit:export` | `export.md` | Regenerate manufacturing artifacts only (jobset/export tools) |

All commands take a project directory and produce/verify deterministic JSON
reports; none accepts model opinion as a verdict.
