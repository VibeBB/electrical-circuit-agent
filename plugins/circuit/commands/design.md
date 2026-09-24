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
and wiring them like a hand-drawn schematic: connect the main signal chain and
serial paths with `add_wire`/`batch_add_wire` (or `connect_pins`/`batch_connect_pins`)
and drop `add_junction` at every T-junction. Reserve `batch_connect_to_net` net
labels for power rails (VCC/GND) and for nets that would otherwise force wires to
cross — a schematic that connects everything with labels and no wires is
electrically valid but unreadable, and `circuit_sch_lint` reports it as
`label_only_connectivity`. Do not draw pin-to-pin wires across
components. In Konnect 0.12.1, `save_project` takes `{}`. Schematic files are only
written by Konnect operations — never by generated scripts or hand-edited
s-expressions. When dynamically loaded `konnect_*` toolsets are not visible to the
harness, run those same operations through `circuit_konnect_call` rather than
falling back to file writes; batch them in its `ops` array so `load_toolset` and
the real ops share one managed session. Run `circuit_sch_lint` on the authored schematic and stop if it
fails; if the tool is unavailable or errors, the gate fails closed — stop and
report the missing gate rather than continuing to ERC. Repair warning-severity
findings through Konnect ops and re-run the lint until it is quiet:
`property_on_symbol` and misplaced labels via `reset_schematic_field_positions`,
`batch_edit_schematic_components`, `list_schematic_labels`,
`move_labels_by_offset`, or `batch_rotate_labels`; empty title-block fields via
`edit_sheet`; a cramped sheet via `bulk_move_schematic_components` or
`edit_sheet` paper; `power_flag_crowded` by keeping one `power:PWR_FLAG` per
driven rail at its source. Then call
`circuit_connectivity_check` and stop if its kicad-cli netlist gate fails, then run
`circuit_erc`. Do not re-run a gate whose inputs have not changed.

For the layout stage, start the KiCad api-server, update the PCB, place and route
it, save, then run `circuit_drc`. Fix silkscreen overlaps and illegible or
rotated reference designators reported by rendered views with
`edit_board_footprint_graphic` before the final render. The design is not
complete until the pipeline
tail has also run: `circuit_render` for top and bottom board views plus a side
elevation (`kind: board3d` with `side`/`rotate`), a schematic page plot
(`kind: schematic`), and per-layer plots (`kind: layers`) when copper detail
matters — place the PNGs under
`circuit-reports/`, `circuit_export` with each kind under `<project>/exports/<kind>/`,
`circuit_jobset_run` for the manufacturing outputs, and `circuit_diff` whenever a
baseline or snapshot exists (write `*.diff.json` under `circuit-reports/`).
`circuit_stackup` writes a deterministic section-diagram SVG next to the
stackup JSON for cross-section review; `circuit_import` and
`circuit_rasterize` cover foreign CAD files and PDF intake.
Record every advisory check outcome as a
`circuit-reports/<stage>-<slug>.advisory.json` file — one JSON object per check
following the AdvisoryResult contract: `{"tool": <konnect tool or check name>,
"stage": "intake|schematic|layout|review|manufacturing", "status":
"ok|error|not_applicable", "summary": "<one line>", "artifacts": [<paths>],
"detail": {...}}`; `circuit_design_report` aggregates them. Finish with
`circuit_design_report`.

The design report collects exports, renders, diffs, the jobset record, and the
advisory Konnect section from these conventional paths. Treat that section as
supplemental evidence only: the verdict continues to come exclusively from
kicad-cli connectivity, ERC, and DRC JSON.
