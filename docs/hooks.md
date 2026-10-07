# Hooks

Hook commands resolve the plugin root (`CIRCUIT_PLUGIN_ROOT`, the project
`plugins/circuit`, `~/.agents/plugins/circuit`,
`~/.openhands/plugins/installed/circuit`, `${HOME}/plugins/installed/circuit`
or `${OH_PERSISTENCE_DIR}/plugins/installed/circuit`) then exec the script.
The two extra candidates resolve the plugin inside an OpenHands docker
conversation runtime (inner `HOME=/var/openhands/.openhands`), where
`circuit_launcher.py` then fails closed with guidance — docker is
unavailable there by design. Scripts read
one JSON event on stdin; exit 0 allows, exit 2 denies with stderr shown to
the agent, other exits are non-blocking errors. `_records.py`,
`require_records.py`, `ensure_llm_profiles.py` and `ensure_agent_profiles.py`
are canonical across the family — never edit locally
(`scripts/check_shared_hooks.py` verifies normalized-AST hashes).

## Plugin hooks (`plugins/circuit/hooks/hooks.json`)

| Event | Matcher | Hook (script) | Behaviour | Files written |
|---|---|---|---|---|
| session_start | `*` | circuit-doctor | Reports environment diagnostics into the session context | none |
| session_start | `*` | intake-attachments | Materializes newly attached images to `intake/attachments/<sha256[:12]>.<ext>` + `manifest.jsonl` | intake/attachments/* |
| session_start | `*` | ensure-llm-profiles | Ensures `.openhands/profiles/` LLM profiles exist (`vibebb-author`/`vibebb-review`/`oracle`, cloned from the active profile; shared canon) | .openhands/profiles/* |
| session_start | `*` | ensure-agent-profiles | Writes `~/.openhands/agent-profiles/vibebb-circuit.json` when missing: openhands-kind, `llm_profile_ref=vibebb-author`, MCP scoped to `circuit`, no secrets (shared canon) | .openhands/agent-profiles/* |
| session_start | `*` | ensure-part-author-profiles | Ensures the lane-a/b author profiles exist | .openhands/profiles/* |
| session_start | `*` | require-records (`session-start`) | Injects the VRP hint: which record tools exist and what must be left | none (records-status.json read only) |
| user_prompt_submit | `*` | intake-attachments | Same attachment materialization per user message | intake/attachments/* |
| user_prompt_submit | `*` | record-library-review | Mirrors trusted library-review user responses into the review journal | library review journal |
| user_prompt_submit | `*` | record-human-response | Binds user answers to outstanding HumanRequests | human request responses |
| pre_tool_use | `*` | protect-libraries | Denies file_editor/terminal writes to `libraries/` outside verified flows, and any write to `observations/circuit/*.jsonl`, `records-status.json`, `.sessions/`, `liaison/*.ux-response.json` (exit 2) | none |
| pre_tool_use | terminal | safety-rail | Denies destructive/risky terminal commands | none |
| stop | `*` | require-records (`stop`) | FIRST stop hook: denies stop while artifact-bound records are still owed (max_stop_denials: 2 per `records-policy.json`) | records-status.json |
| stop | `*` | report-design-status | Prints design status summary at session end | none |
| stop | `*` | intake-attachments | Final attachment sweep | intake/attachments/* |
| stop | `*` | record-library-review | Final review-journal mirror | library review journal |
| post_tool_use | `circuit_render\|circuit_diff\|circuit_rasterize\|circuit_stackup\|circuit_vision_read\|circuit_vision_compare\|circuit_model_compare\|file_editor` | record-image-observation | Appends produced/looked-at image paths to `observations/circuit/image-observations.jsonl` (superset of `IMAGE_TOOLS` + file_editor) | image-observations.jsonl |
| post_tool_use | inspect_image_with_vision | record-vision-tool-event | Appends the vision Q&A to `observations/circuit/vision-tool-events.jsonl` | vision-tool-events.jsonl |
| post_tool_use | circuit_part_author_commit | record-authoring-commit | Seals the lane's author-commit impression record | authoring commit record |

## Agent-frontmatter hooks

| Agent | Event / matcher | Hook | Notes |
|---|---|---|---|
| circuit-brief, circuit-schematic, circuit-layout, circuit-library, circuit-review | pre_tool_use `*` | protect-libraries | Sub-agents do not inherit plugin hooks — declared per AgentDefinition |
| same five | pre_tool_use `terminal` | safety-rail | same |
| circuit-brief, circuit-review | post_tool_use `inspect_image_with_vision` | record-vision-tool-event | same |
| circuit-part-author-a/-b | pre_tool_use `*` | blind-author-lane-guard (`guard_author_lane.py`, `CIRCUIT_AUTHORING_LANE=a|b`) | Denies (exit 2) other-lane run dirs, `observations/circuit/` paths, and `circuit_record_*`/`circuit_records_status`/`circuit_ux_*` tools so shared logs cannot leak reasoning across the blind lanes |
| circuit-part-author-a/-b | post_tool_use `circuit_part_author_commit` | record-authoring-commit | Seals each lane's commit impression |
