"""Reject writes to bundled KiCad library trees and to design files."""

from __future__ import annotations

import json
import sys
from pathlib import PurePath
from typing import Any, cast

DESIGN_SUFFIXES = (".kicad_sch", ".kicad_pcb")
WRITE_TOOLS = {"file_editor", "apply_patch"}
VIEW_ACTIONS = {"view", "read", "undo_edit"}
WRITE_ACTIONS = {"create", "str_replace", "insert", "edit", "write"}
WRITE_PAYLOAD_KEYS = ("file_text", "new_str", "content", "patch", "insert_text")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def _targets_design_file(tool_input: dict[str, Any]) -> bool:
    for value in _strings(tool_input):
        normalized = value.replace("\\", "/")
        if any(suffix in normalized for suffix in DESIGN_SUFFIXES):
            return True
    return False


def _is_design_write(payload: dict[str, Any]) -> bool:
    tool_name = payload.get("tool_name")
    if tool_name not in WRITE_TOOLS:
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    tool_input = cast(dict[str, Any], tool_input)
    if not _targets_design_file(tool_input):
        return False
    if tool_name == "apply_patch":
        return True
    action = tool_input.get("command") or tool_input.get("action")
    if isinstance(action, str):
        if action in VIEW_ACTIONS:
            return False
        if action in WRITE_ACTIONS:
            return True
    return any(key in tool_input for key in WRITE_PAYLOAD_KEYS)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"invalid hook input: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("invalid hook input: not an object", file=sys.stderr)
        return 2
    payload = cast(dict[str, Any], payload)
    if _is_design_write(payload):
        print(
            "design files (.kicad_sch/.kicad_pcb) are authored through the"
            " Konnect MCP operations, not file_editor or apply_patch",
            file=sys.stderr,
        )
        return 2
    paths = _strings(payload.get("tool_input", payload))
    blocked = (PurePath("/opt/circuit/libraries"), PurePath("libraries/cern-kicad-libs"))
    for value in paths:
        normalized = value.replace("\\", "/")
        if any(str(path) in normalized for path in blocked):
            print(f"library writes are prohibited: {value}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
