"""Reject writes to bundled KiCad library trees and to design files.

Only path-bearing arguments decide the verdict: file bodies such as
file_text/new_str may legitimately mention design suffixes or library paths, so
payload content is never scanned. For the terminal, writes are detected from
shell-level operators (redirects, tee, cp/mv destinations, dd, sed -i, rm,
mkdir, chmod, ...) instead of any mention of a protected path, so read-only
commands like `find` or `grep` on the libraries are allowed.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from typing import Any, cast

DESIGN_SUFFIXES = (".kicad_sch", ".kicad_pcb")
BLOCKED_PATHS = ("/opt/circuit/libraries", "libraries/cern-kicad-libs")
WRITE_TOOLS = {"file_editor", "apply_patch"}
VIEW_ACTIONS = {"view", "read", "undo_edit"}
WRITE_ACTIONS = {"create", "str_replace", "insert", "edit", "write"}
PATH_KEYS = ("path", "file_path", "paths", "target_file", "old_path", "new_path")
COMMAND_SEPARATORS = {"|", "||", "&&", "&", ";", "(", ")"}
WRAPPER_COMMANDS = {"sudo", "doas", "env", "nice", "time", "ionice", "taskset", "stdbuf"}
LAST_OPERAND_WRITES = {"cp", "mv", "install", "rsync", "ln", "scp", "cpio"}
ANY_OPERAND_WRITES = {
    "tee",
    "rm",
    "rmdir",
    "touch",
    "mkdir",
    "chmod",
    "chown",
    "chgrp",
    "truncate",
    "shred",
}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def _path_values(tool_input: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in PATH_KEYS:
        if key in tool_input:
            values.extend(_strings(tool_input[key]))
    return values


def _is_protected(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return any(suffix in normalized for suffix in DESIGN_SUFFIXES) or any(
        path in normalized for path in BLOCKED_PATHS
    )


def _is_design_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return any(suffix in normalized for suffix in DESIGN_SUFFIXES)


def _is_library_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return any(path in normalized for path in BLOCKED_PATHS)


def _is_design_write(payload: dict[str, Any]) -> bool:
    tool_name = payload.get("tool_name")
    if tool_name not in WRITE_TOOLS:
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    tool_input = cast(dict[str, Any], tool_input)
    if tool_name == "apply_patch":
        return any(_is_design_path(value) for value in _strings(tool_input))
    if not any(_is_design_path(value) for value in _path_values(tool_input)):
        return False
    action = tool_input.get("command") or tool_input.get("action")
    if isinstance(action, str):
        if action in VIEW_ACTIONS:
            return False
        if action in WRITE_ACTIONS:
            return True
    return any(key in tool_input for key in ("file_text", "new_str", "content", "insert_text"))


def _is_library_write(payload: dict[str, Any]) -> bool:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    tool_input = cast(dict[str, Any], tool_input)
    if tool_name == "apply_patch":
        return any(_is_library_path(value) for value in _strings(tool_input))
    if tool_name != "file_editor":
        return False
    if not any(_is_library_path(value) for value in _path_values(tool_input)):
        return False
    action = tool_input.get("command") or tool_input.get("action")
    return not (isinstance(action, str) and action in VIEW_ACTIONS)


def _operands(tokens: list[str]) -> list[str]:
    operands = [token for token in tokens if not token.startswith("-")]
    return operands


def _command_name(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _terminal_write_target(command: str) -> str | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in COMMAND_SEPARATORS:
            i += 1
            continue
        if token in {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>"}:
            if i + 1 < len(tokens) and _is_protected(tokens[i + 1]):
                return tokens[i + 1]
            i += 2
            continue
        redirect = re.match(r"^(\d*|&)>(>?)([^&].*)?$", token)
        if redirect is not None:
            target = redirect.group(3) or (tokens[i + 1] if i + 1 < len(tokens) else "")
            if _is_protected(target):
                return target
            i += 2 if not redirect.group(3) else 1
            continue
        name = _command_name(token)
        if name in WRAPPER_COMMANDS:
            i += 1
            while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
                i += 1
            continue
        if name == "dd":
            j = i + 1
            while j < len(tokens) and tokens[j] not in COMMAND_SEPARATORS:
                if tokens[j].startswith("of=") and _is_protected(tokens[j][3:]):
                    return tokens[j][3:]
                j += 1
            i = j
            continue
        if name == "sed":
            j = i + 1
            args: list[str] = []
            in_place = False
            while j < len(tokens) and tokens[j] not in COMMAND_SEPARATORS:
                if tokens[j] == "-i" or tokens[j].startswith("-i"):
                    in_place = True
                else:
                    args.append(tokens[j])
                j += 1
            if in_place:
                for operand in _operands(args):
                    if _is_protected(operand):
                        return operand
            i = j
            continue
        if name in LAST_OPERAND_WRITES or name in ANY_OPERAND_WRITES:
            j = i + 1
            args: list[str] = []
            while j < len(tokens) and tokens[j] not in COMMAND_SEPARATORS:
                args.append(tokens[j])
                j += 1
            operands = _operands(args)
            if name in LAST_OPERAND_WRITES:
                operands = operands[-1:] if operands else []
            for operand in operands:
                if _is_protected(operand):
                    return operand
            i = j
            continue
        i += 1
    return None


def _is_terminal_write(payload: dict[str, Any]) -> str | None:
    if payload.get("tool_name") != "terminal":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    return _terminal_write_target(command)


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
    if _is_library_write(payload):
        print("library writes are prohibited", file=sys.stderr)
        return 2
    target = _is_terminal_write(payload)
    if target is not None:
        print(
            f"design files and bundled libraries may not be written through the terminal: {target}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
