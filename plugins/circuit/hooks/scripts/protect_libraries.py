"""Reject protected design/library access through agent tools.

Only path-bearing arguments decide the design and library-write verdict:
file bodies such as file_text/new_str may legitimately mention design suffixes
or library paths, so payload content is not scanned for those rules.
Vision-control and corpus references are blocked in every tool input. For the
terminal, library writes are detected from shell-level operators rather than
path mentions so read-only library commands remain available.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _provenance import project_dir

DESIGN_SUFFIXES = (".kicad_sch", ".kicad_pcb")
BLOCKED_PATHS = (
    "/opt/circuit/libraries",
    "libraries/cern-kicad-libs",
    "library/corpus",
    ".openhands/agent-canvas",
)
CORPUS_PATH = re.compile(r"(?:^|/)library/corpus(?:/|$)", re.IGNORECASE)
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
WEB_FETCH_TOOL_MARKERS = ("web", "fetch", "browser")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        return [item for child in mapping.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in cast(list[Any], value) for item in _strings(child)]
    return []


def _path_values(tool_input: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in PATH_KEYS:
        if key in tool_input:
            values.extend(_strings(tool_input[key]))
    return values


def _references_vision_control(payload: dict[str, Any]) -> bool:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return any(
        ".vision-control" in value.replace("\\", "/").casefold() for value in _strings(tool_input)
    )


def _references_corpus(payload: dict[str, Any]) -> bool:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return any(
        CORPUS_PATH.search(value.replace("\\", "/")) is not None for value in _strings(tool_input)
    )


def _confidential_paths(project: Path) -> set[str]:
    manifests = (
        project / "intake" / "attachments" / "manifest.jsonl",
        project / ".confidential" / "intake" / "attachments" / "manifest.jsonl",
    )
    paths: set[str] = set()
    for manifest in manifests:
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            record = cast(dict[str, Any], record)
            if record.get("confidential") is not True:
                continue
            for key in ("attachment_path", "image_path", "path"):
                value = record.get(key)
                if not isinstance(value, str) or not value:
                    continue
                normalized = value.replace("\\", "/")
                paths.add(normalized.casefold())
                path = Path(value)
                if path.is_absolute():
                    try:
                        relative = path.resolve().relative_to(project.resolve()).as_posix()
                    except ValueError:
                        continue
                    paths.add(relative.casefold())
    return paths


def _is_confidential_reference(value: str, confidential_paths: set[str]) -> bool:
    normalized = value.replace("\\", "/").casefold()
    return bool(re.search(r"(?:^|/)\.confidential(?:/|$)", normalized)) or any(
        path in normalized for path in confidential_paths
    )


def _references_confidential_web_path(
    payload: dict[str, Any],
    confidential_paths: set[str],
) -> bool:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if (
        not isinstance(tool_name, str)
        or not any(marker in tool_name.casefold() for marker in WEB_FETCH_TOOL_MARKERS)
        or not isinstance(tool_input, dict)
    ):
        return False
    return any(
        _is_confidential_reference(value, confidential_paths)
        for value in _strings(cast(dict[str, Any], tool_input))
    )


def _git_paths(project: Path, *, cached: bool) -> set[str]:
    args = (
        ["diff", "--cached", "--name-only", "--diff-filter=ACMRT"]
        if cached
        else ["diff", "--name-only", "@{upstream}..HEAD"]
    )
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip().replace("\\", "/").casefold() for line in result.stdout.splitlines()}


def _terminal_confidential_git_target(
    payload: dict[str, Any],
    project: Path,
    confidential_paths: set[str],
) -> str | None:
    if payload.get("tool_name") != "terminal":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = cast(dict[str, Any], tool_input).get("command")
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for index, token in enumerate(tokens[:-1]):
        if _command_name(token) != "git":
            continue
        subcommand_index = index + 1
        while subcommand_index < len(tokens):
            option = tokens[subcommand_index]
            if option in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}:
                subcommand_index += 2
            elif option.startswith("-"):
                subcommand_index += 1
            else:
                break
        if subcommand_index >= len(tokens):
            continue
        subcommand = tokens[subcommand_index]
        if subcommand not in {"add", "commit", "push"}:
            continue
        operands = [
            item
            for item in tokens[subcommand_index + 1 :]
            if item not in COMMAND_SEPARATORS and not item.startswith("-")
        ]
        for operand in operands:
            if _is_confidential_reference(operand, confidential_paths):
                return operand
        if subcommand == "add":
            continue
        changed_paths = _git_paths(project, cached=subcommand == "commit")
        if subcommand == "push" and not changed_paths:
            try:
                result = subprocess.run(
                    ["git", "ls-files"],
                    cwd=project,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                changed_paths = {
                    line.strip().replace("\\", "/").casefold()
                    for line in result.stdout.splitlines()
                }
        for path in changed_paths:
            if _is_confidential_reference(path, confidential_paths):
                return path
    return None


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
    tool_input = cast(dict[str, Any], tool_input)
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
    if payload.get("tool_name") == "circuit_corpus_score":
        print("golden corpus scoring is inaccessible through agent tools", file=sys.stderr)
        return 2
    if _references_corpus(payload):
        print("golden corpus is inaccessible through agent tools", file=sys.stderr)
        return 2
    if _references_vision_control(payload):
        print("vision control state is inaccessible through agent tools", file=sys.stderr)
        return 2
    project = project_dir(payload)
    confidential_paths = _confidential_paths(project)
    if _references_confidential_web_path(payload, confidential_paths):
        print("confidential artifacts may not be sent to web or fetch tools", file=sys.stderr)
        return 2
    confidential_target = _terminal_confidential_git_target(
        payload,
        project,
        confidential_paths,
    )
    if confidential_target is not None:
        print(
            f"confidential artifacts may not be staged, committed, or pushed: "
            f"{confidential_target}",
            file=sys.stderr,
        )
        return 2
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
