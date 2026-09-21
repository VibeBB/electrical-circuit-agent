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
components. In Konnect 0.12.1, `save_project` takes `{}`. Call
`circuit_connectivity_check` and stop if its kicad-cli netlist gate fails, then run
`circuit_erc`. Start the KiCad api-server, update the PCB, place and route it, save,
run `circuit_drc`, export artifacts, and finish with `circuit_design_report`.

The design report also contains an advisory Konnect section with coverage results and
export comparisons. Treat that section as supplemental evidence only: the verdict
continues to come exclusively from kicad-cli connectivity, ERC, and DRC JSON.
