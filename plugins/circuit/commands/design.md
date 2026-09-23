---
description: Orchestrate a conversational schematic and PCB design workflow.
argument-hint: <project_dir> <requirements summary>
allowed-tools:
  - task_tracker
  - task_tool_set
  - terminal
---

Clarify requirements with the user, create a task-tracker plan, then delegate first to
`circuit-brief`. Resolve every returned `open_questions` item with the user and repeat
until the intake report is `ready` and the library report is `pass`. Only then delegate
to `circuit-schematic`, `circuit-layout`, and `circuit-review` through the SDK task tool
set. Run ERC and DRC through the circuit MCP server before summarizing.
Use `TaskToolSet`, `AgentDefinition`, and `TaskTrackerTool`; do not use the
deprecated DelegateTool or WorkflowToolSet. The JSON verdicts, not a sub-agent
opinion, determine pass or fail.

For a new design, first delegate conversation intake to `circuit-brief`, which writes a
machine-readable design brief and intake sidecar. Call `circuit_brief_validate`,
`circuit_brief_library_check`, and `circuit_brief_intake_check`; a blocked intake or
failed library report must stop delegation. Create the schematic through Konnect by registering the
required libraries, looking up every symbol with `get_symbol_info`, placing symbols,
and using `batch_connect_to_net` net labels. Do not draw pin-to-pin wires across
components. In Konnect 0.12.1, `save_project` takes `{}`. Schematic files are only
written by Konnect operations — never by generated scripts or hand-edited
s-expressions. When dynamically loaded `konnect_*` toolsets are not visible to the
harness, run those same operations through `circuit_konnect_call` rather than
falling back to file writes. Run `circuit_sch_lint` on the authored schematic and stop if it
fails; if the tool is unavailable or errors, the gate fails closed — stop and
report the missing gate rather than continuing to ERC, then call
`circuit_connectivity_check` and stop if its kicad-cli netlist gate fails, then run
`circuit_erc`. Do not re-run a gate whose inputs have not changed.

For the layout stage, start the KiCad api-server, update the PCB, place and route
it, save, then run `circuit_drc`. The design is not complete until the pipeline
tail has also run: `circuit_render` for top and bottom views (place the PNGs under
`circuit-reports/`), `circuit_export` with each kind under `<project>/exports/<kind>/`,
`circuit_jobset_run` for the manufacturing outputs, and `circuit_diff` whenever a
baseline or snapshot exists (write `*.diff.json` under `circuit-reports/`).
Record advisory Konnect results as `*.advisory.json` files or `advisory.jsonl`
lines under `circuit-reports/`, then finish with `circuit_design_report`.

The design report collects exports, renders, diffs, the jobset record, and the
advisory Konnect section from these conventional paths. Treat that section as
supplemental evidence only: the verdict continues to come exclusively from
kicad-cli connectivity, ERC, and DRC JSON.
