---
description: Diagnose circuit-agent tools and runtime installation.
allowed-tools:
  - terminal
---

Run `python3 "$CIRCUIT_PLUGIN/scripts/circuit_launcher.py" doctor` and report its JSON
result without changing or weakening any check. Use
`python3 "$CIRCUIT_PLUGIN/scripts/circuit_launcher.py" doctor --warn` for session
startup diagnostics where findings must not block the session.

Inside OpenHands, circuit commands run inside the pinned circuit-tools image
via the plugin launcher. Resolve the plugin root the same way the hooks do
(`$CIRCUIT_PLUGIN_ROOT`, `${OPENHANDS_PROJECT_DIR}/plugins/circuit`,
`~/.agents/plugins/circuit`, `~/.openhands/plugins/installed/circuit`) into
`$CIRCUIT_PLUGIN`, then call `python3 "$CIRCUIT_PLUGIN/scripts/circuit_launcher.py"
<args>`. In a repo checkout, `uv run python -m circuit.<module> <args>` is
equivalent.
