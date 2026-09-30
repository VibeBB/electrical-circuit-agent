from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from .workspace import workspace_path

__all__ = [
    "_footprint_library_path",
    "_installed_footprint_roots",
    "_is_path_argument",
    "_json",
    "_literal",
    "_netlist_path",
    "_optional_literal",
    "_optional_string",
    "_output_path",
    "_required_string",
    "_socket_url",
    "_workspace_arguments",
    "_workspace_path_argument",
]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _installed_footprint_roots() -> tuple[Path, ...]:
    kicad_share = Path(os.environ.get("CIRCUIT_KICAD_SHARE", "/usr/share/kicad-nightly"))
    cern = Path(os.environ.get("CIRCUIT_CERN_LIBS", "/opt/circuit/libraries/cern-kicad-libs"))
    return kicad_share / "footprints", cern / "PcbLib"


def _footprint_library_path(value: str) -> Path:
    try:
        return workspace_path(value)
    except ValueError as workspace_error:
        candidate = Path(value)
        if candidate.is_absolute():
            for root in _installed_footprint_roots():
                try:
                    return workspace_path(candidate, root=root)
                except ValueError:
                    pass
        raise ValueError(
            "footprint library must be inside the workspace or an installed KiCad/CERN library"
        ) from workspace_error


def _is_path_argument(key: str) -> bool:
    return key in {"path", "paths"} or key.endswith(("_path", "_dir"))


def _workspace_path_argument(
    name: str,
    key: str,
    value: Any,
    required_paths: set[str],
    arguments: dict[str, Any],
) -> Any:
    if isinstance(value, list):
        return [
            _workspace_path_argument(name, key, item, required_paths, arguments)
            for item in cast(list[Any], value)
        ]
    if value is None:
        if key in required_paths:
            raise ValueError(f"'{key}' must be a non-empty path")
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a path string")
    if not value:
        if key in required_paths:
            raise ValueError(f"'{key}' must be a non-empty path")
        return value
    if name == "circuit_export" and key == "source_path" and arguments.get("kind") == "fp_svg":
        return str(_footprint_library_path(value))
    return str(workspace_path(value))


def _workspace_arguments(
    name: str,
    arguments: dict[str, Any],
    tools: list[tuple[str, str, dict[str, Any]]],
) -> dict[str, Any]:
    required_paths: set[str] = set()
    for tool_name, _, schema in tools:
        if tool_name == name:
            required = schema.get("required", [])
            if isinstance(required, list):
                required_paths = {key for key in cast(list[Any], required) if isinstance(key, str)}
            break

    normalized = dict(arguments)
    for key, value in arguments.items():
        if _is_path_argument(key):
            normalized[key] = _workspace_path_argument(name, key, value, required_paths, arguments)
        elif name == "circuit_konnect_call" and key == "ops" and isinstance(value, list):
            ops: list[Any] = []
            for item in cast(list[Any], value):
                if isinstance(item, dict):
                    operation = cast(dict[str, Any], item)
                    operation_tool = operation.get("tool")
                    operation_arguments = operation.get("arguments")
                    if isinstance(operation_tool, str) and isinstance(operation_arguments, dict):
                        operation = {
                            **operation,
                            "arguments": _workspace_arguments(
                                operation_tool,
                                cast(dict[str, Any], operation_arguments),
                                tools,
                            ),
                        }
                    ops.append(operation)
                else:
                    ops.append(item)
            normalized[key] = ops
        elif (
            name == "circuit_konnect_call"
            and key == "arguments"
            and isinstance(value, dict)
            and isinstance(arguments.get("tool"), str)
        ):
            normalized[key] = _workspace_arguments(
                cast(str, arguments["tool"]),
                cast(dict[str, Any], value),
                tools,
            )
    return normalized


def _socket_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.lower().startswith("ipc://")
        or urlsplit(value).scheme.lower() != "ipc"
    ):
        raise ValueError("circuit_konnect_call 'socket' must use ipc://")
    return value


def _output_path(source: Path, output: str | None, kind: str) -> Path:
    path = (
        Path(output) if output else source.parent / "circuit-reports" / f"{source.stem}.{kind}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _netlist_path(source: Path, output: str | None) -> Path:
    path = Path(output) if output else source.parent / "circuit-reports" / f"{source.stem}.net"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _required_string(args: dict[str, Any], name: str, context: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} requires '{name}'")
    return value


def _literal[LiteralT: str](
    args: dict[str, Any],
    key: str,
    allowed: tuple[LiteralT, ...],
    default: LiteralT | None = None,
    *,
    context: str,
) -> LiteralT:
    value = args.get(key, default)
    if value is None:
        raise ValueError(f"{context} requires '{key}'")
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"'{key}' must be one of {list(allowed)}, got {value!r}")
    return cast(LiteralT, value)


def _optional_literal[LiteralT: str](
    args: dict[str, Any], key: str, allowed: tuple[LiteralT, ...], *, context: str
) -> LiteralT | None:
    value = args.get(key)
    if value is None or value == "":
        return None
    return _literal(args, key, allowed, context=context)
