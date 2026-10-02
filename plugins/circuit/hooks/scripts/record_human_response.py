"""Record hash-bound pointers to user HumanRequest responses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _provenance import project_dir

EVENTS_DIR_ENV = "CIRCUIT_AGENT_EVENTS_DIR"
DEFAULT_EVENTS_ROOT = Path(".openhands/agent-canvas/dev_conversations")
RESPONSE_LINE = re.compile(r"^CIRCUIT-HUMAN-RESPONSE ([0-9a-f]{16})$")
REQUEST_SHA = re.compile(r"^[0-9a-f]{64}$")


def _events_dir(payload: dict[str, Any]) -> Path | None:
    override = os.environ.get(EVENTS_DIR_ENV)
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    candidate = Path.home() / DEFAULT_EVENTS_ROOT / session_id.strip() / "events"
    return candidate if candidate.is_dir() else None


def _message_text(value: Any, key: str = "") -> list[str]:
    if isinstance(value, str):
        return [value] if key in {"content", "message", "text"} else []
    if isinstance(value, list):
        return [text for child in cast(list[Any], value) for text in _message_text(child)]
    if isinstance(value, dict):
        record = cast(dict[str, Any], value)
        return [
            text
            for child_key, child in record.items()
            for text in _message_text(child, str(child_key))
        ]
    return []


def _request_id(record: dict[str, Any]) -> str | None:
    for text in _message_text(record):
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        match = RESPONSE_LINE.fullmatch(first_line)
        if match is not None:
            return match.group(1)
    return None


def _write_pointer(
    request_root: Path,
    event_path: Path,
    request_id: str,
    request_sha256: str,
    event_sha256: str,
) -> None:
    directory = request_root / "responses"
    pointer = directory / f"{request_id}.{event_sha256[:12]}.json"
    if pointer.exists():
        return
    value = {
        "artifact_kind": "circuit_human_response_pointer",
        "request_id": request_id,
        "request_sha256": request_sha256,
        "event_path": str(event_path.resolve()),
        "event_sha256": event_sha256,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{pointer.name}.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        with suppress(FileExistsError):
            os.link(temporary, pointer)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        payload_record = cast(dict[str, Any], payload)
        events_dir = _events_dir(payload_record)
        if events_dir is None:
            return 0
        project = project_dir(payload_record)
        request_roots = (
            project / ".confidential" / "library" / "requests",
            project / "library" / "requests",
        )
        for event_path in sorted(events_dir.glob("event-*.json")):
            try:
                raw = event_path.read_bytes()
                event: Any = json.loads(raw)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_record = cast(dict[str, Any], event)
            if event_record.get("source") != "user":
                continue
            request_id = _request_id(event_record)
            if request_id is None:
                continue
            for request_root in request_roots:
                try:
                    request = json.loads(
                        (request_root / f"{request_id}.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(request, dict):
                    continue
                request_record = cast(dict[str, Any], request)
                request_sha256 = request_record.get("request_sha256")
                if (
                    request_record.get("request_id") != request_id
                    or not isinstance(request_sha256, str)
                    or REQUEST_SHA.fullmatch(request_sha256) is None
                    or not request_sha256.startswith(request_id)
                ):
                    continue
                _write_pointer(
                    request_root,
                    event_path,
                    request_id,
                    request_sha256,
                    hashlib.sha256(raw).hexdigest(),
                )
                break
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
