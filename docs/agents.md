# Agents

Sub-agents live in `plugins/circuit/agents/`. All declare the same MCP map
(`circuit` runtime + `konnect` with `KICAD_API_SOCKET=ipc:///tmp/circuit-kicad.sock`)
because plugin MCP configuration is not inherited from the parent. Every agent
carries the "Records you must leave" (VRP) and "Vision points" sections in its
body; circuit-brief additionally carries the SLP section.

| Agent | Model | Tools | MCP | Frontmatter hooks | When it runs | Records duty |
|---|---|---|---|---|---|---|
| `circuit-brief` | `vibebb-author` | terminal, file_editor, grep, glob, task_tracker | circuit, konnect | pre: protect-libraries (`*`), safety-rail (terminal); post: record-vision-tool-event | Intake conversation → validated brief + intake | decisions for part/package and topology choices; stage impression; vision reviews for intake images; answers liaison requests |
| `circuit-library` | `vibebb-author` | terminal, file_editor, grep, glob, task_tracker, task_tool_set | circuit, konnect | pre: protect-libraries, safety-rail | Orchestrates blind lanes, verification, review packet | lane comparison decision + vision reviews after `circuit_part_author_compare`; stage impression |
| `circuit-part-author-a` | `vibebb-part-author-a` | terminal, file_editor, grep, glob, task_tracker | circuit (lane a), konnect | pre: blind-author-lane-guard (`*`, lane a); post: record-authoring-commit | Blind datasheet→PartSpec lane | MUST NOT call `circuit_record_*`/`circuit_records_status`/`circuit_ux_*` (blind isolation); commit impression enforced by the tool |
| `circuit-part-author-b` | `vibebb-part-author-b` | same as lane a | circuit (lane b), konnect | pre: blind-author-lane-guard (lane b); post: record-authoring-commit | Blind datasheet→PartSpec lane | same blind-lane exception |
| `circuit-schematic` | `vibebb-author` | terminal, file_editor, grep, glob, task_tracker | circuit, konnect | pre: protect-libraries, safety-rail | Schematic authoring + ERC | decisions for net naming/label strategy; stage impression; page-plot vision review |
| `circuit-layout` | `vibebb-author` | terminal, file_editor, grep, glob, task_tracker | circuit, konnect | pre: protect-libraries, safety-rail | Placement, routing, DRC | stackup/profile/placement/routing decisions; DRC waiver refusals; stage impression |
| `circuit-review` | `vibebb-review` | terminal, file_editor, grep, glob, task_tracker | circuit, konnect | pre: protect-libraries, safety-rail; post: record-vision-tool-event | Independent review of gate JSON + diffs | diff PNG vision reviews; stage impression; no verdict authority |

All agents cap at `max_iteration_per_run` 30 (40 for circuit-library) and
`max_budget_per_run` 3.0, and run `permission_mode: never_confirm` where
declared.
