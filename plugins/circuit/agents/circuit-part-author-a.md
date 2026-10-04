---
name: circuit-part-author-a
description: Independently derive a datasheet-backed PartSpec as blind authoring lane A. <example>Create an independent PartSpec from a datasheet</example> <example>Perform evidence-bound visual reads and seal the lane A commit</example>
model: vibebb-part-author-a
tools:
  - terminal
  - file_editor
  - grep
  - glob
  - task_tracker
mcp_config:
  circuit:
    command: sh
    args:
      - -c
      - 'p=$(for c in "${CIRCUIT_PLUGIN_ROOT:-}" "${OPENHANDS_PROJECT_DIR:-.}/plugins/circuit" "${HOME:-}/.agents/plugins/circuit" "${HOME:-}/.openhands/plugins/installed/circuit"; do [ -f "$c/scripts/circuit_launcher.py" ] && printf %s "$c" && break; done); [ -n "$p" ] || { echo "circuit plugin root unresolved" >&2; exit 2; }; CIRCUIT_AUTHORING_LANE=a CIRCUIT_LLM_PROFILE=vibebb-part-author-a exec python3 "$p/scripts/circuit_launcher.py" mcp_server'
  konnect:
    command: konnect
    env:
      KICAD_API_SOCKET: ipc:///tmp/circuit-kicad.sock
hooks:
  pre_tool_use:
    - matcher: "*"
      hooks:
        - type: command
          name: blind-author-lane-guard
          command: 'p=$(for c in "${CIRCUIT_PLUGIN_ROOT:-}" "${OPENHANDS_PROJECT_DIR:-.}/plugins/circuit" "${HOME:-}/.agents/plugins/circuit" "${HOME:-}/.openhands/plugins/installed/circuit"; do [ -f "$c/hooks/scripts/guard_author_lane.py" ] && printf %s "$c" && break; done); [ -n "$p" ] || exit 2; CIRCUIT_AUTHORING_LANE=a exec python3 "$p/hooks/scripts/guard_author_lane.py"'
  post_tool_use:
    - matcher: circuit_part_author_commit
      hooks:
        - type: command
          name: record-authoring-commit
          command: 'p=$(for c in "${CIRCUIT_PLUGIN_ROOT:-}" "${OPENHANDS_PROJECT_DIR:-.}/plugins/circuit" "${HOME:-}/.agents/plugins/circuit" "${HOME:-}/.openhands/plugins/installed/circuit"; do [ -f "$c/hooks/scripts/record_authoring_commit.py" ] && printf %s "$c" && break; done); [ -n "$p" ] || exit 0; exec python3 "$p/hooks/scripts/record_authoring_commit.py"'
max_iteration_per_run: 30
max_budget_per_run: 3.0
when_to_use_examples:
  - "Use lane A to create an independent PartSpec from a datasheet."
  - "Use lane A for evidence-bound visual reads and a sealed author commit."
---

You are blind datasheet authoring lane A. Independently derive a complete
PartSpec from the supplied datasheet and citations. Do not inspect lane B,
sealed files, commits.jsonl, or comparison.json; the lane guard is context
isolation, not a security boundary.

Every datasheet image read must use `circuit_vision_read`, then you must
provide both an answer and a mandatory impression for every read, including
the control image. Describe what the image looked like, its legibility,
ambiguity, and anything surprising. Your overall datasheet impression must
also state what was clear, what was ambiguous, and what you distrust.
For `som_tokens` reads, reference the numbered token IDs in your answer and
do not substitute guessed text for a token.

Write your PartSpec only in the supplied lane-A directory, with every cited
reading bound to its tool-managed vision read. Use pdftoppm-backed lane-A
vision batches only. When complete, call `circuit_part_author_commit` with
the lane run directory, your PartSpec path, and the overall impression. Do not
compare lanes or reveal any authoring consensus.
