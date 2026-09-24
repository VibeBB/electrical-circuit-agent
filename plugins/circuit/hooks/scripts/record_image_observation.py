"""Record every image the conversation actually saw, without blocking.

Companion to record_vision_tool_event.py: that hook logs delegated
inspect_image_with_vision calls; this one logs direct image observations —
`circuit_render` (rendered PNG paths in the tool result) and `file_editor`
`view` commands on image files. Each observation is appended to
`observations/circuit/image-observations.jsonl` as
{sequence, event_id, tool_name, image_path, image_sha256, recorded_at,
session_id} so every image the model saw has a provenance record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

EVENTS_ENV = "CIRCUIT_IMAGE_OBSERVATIONS"
EVENTS_RELATIVE_PATH = Path("observations/circuit/image-observations.jsonl")
OBSERVED_TOOLS = {"circuit_render", "circuit_diff", "circuit_rasterize", "file_editor"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
_IMAGE_PATH = re.compile(r"[^\s\"'<>]+?\.(?:png|jpe?g)", re.IGNORECASE)


def _project_dir(payload: dict[str, Any]) -> Path:
    return Path(
        os.environ.get("OPENHANDS_PROJECT_DIR") or payload.get("working_dir") or "."
    ).resolve()


def _events_path(payload: dict[str, Any]) -> Path:
    override = os.environ.get(EVENTS_ENV)
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else _project_dir(payload) / path
    return _project_dir(payload) / EVENTS_RELATIVE_PATH


def _event_id(record: dict[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolve(candidate: str, base: Path) -> Path | None:
    path = Path(candidate)
    if not path.is_absolute():
        path = base / path
    if path.suffix.lower() not in _IMAGE_SUFFIXES or not path.is_file():
        return None
    return path


def _image_paths(payload: dict[str, Any], base: Path) -> list[Path]:
    tool_name = payload.get("tool_name")
    candidates: list[str] = []
    if tool_name == "file_editor":
        tool_input = payload.get("tool_input")
        tool_input = {} if not isinstance(tool_input, dict) else cast(dict[str, Any], tool_input)
        if tool_input.get("command") != "view":
            return []
        path = tool_input.get("path")
        if isinstance(path, str):
            candidates.append(path)
    else:
        for text in _strings(payload.get("tool_response")):
            candidates.extend(_IMAGE_PATH.findall(text))
    seen: set[str] = set()
    paths: list[Path] = []
    for candidate in candidates:
        resolved = _resolve(candidate, base)
        if resolved is not None and str(resolved) not in seen:
            seen.add(str(resolved))
            paths.append(resolved)
    return paths


def _strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for child in cast(dict[str, Any], value).values():
            found.extend(_strings(child))
    elif isinstance(value, list):
        for child in cast(list[Any], value):
            found.extend(_strings(child))
    return found


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("tool_name") not in OBSERVED_TOOLS:
        return 0
    response = payload.get("tool_response")
    if isinstance(response, dict):
        response = cast(dict[str, Any], response)
        if "error" in response or response.get("is_error"):
            return 0
    base = _project_dir(cast(dict[str, Any], payload))
    paths = _image_paths(cast(dict[str, Any], payload), base)
    if not paths:
        return 0
    path = _events_path(cast(dict[str, Any], payload))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sequence = 1
        if path.exists():
            sequence += len(path.read_text(encoding="utf-8").splitlines())
        with path.open("a", encoding="utf-8") as stream:
            for image_path in paths:
                try:
                    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                except OSError:
                    continue
                identity = {
                    "sequence": sequence,
                    "tool_name": payload["tool_name"],
                    "image_path": str(image_path),
                    "image_sha256": digest,
                }
                record = {
                    "sequence": sequence,
                    "event_id": _event_id(identity),
                    "tool_name": payload["tool_name"],
                    "image_path": str(image_path),
                    "image_sha256": digest,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "session_id": payload.get("session_id"),
                }
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")
                sequence += 1
    except (OSError, UnicodeDecodeError) as exc:
        print(f"image observation log unavailable: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
