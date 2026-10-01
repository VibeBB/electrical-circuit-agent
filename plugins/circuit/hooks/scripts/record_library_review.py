"""Record pointers to user-authored library review decisions."""

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
REVIEW_LINE = re.compile(r"^CIRCUIT-LIBRARY-REVIEW ([0-9a-f]{16})$")


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


def _packet_id(record: dict[str, Any]) -> str | None:
    for text in _message_text(record):
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        match = REVIEW_LINE.fullmatch(first_line)
        if match:
            return match.group(1)
    return None


def _write_pointer(project: Path, event_path: Path, packet_id: str, event_sha256: str) -> None:
    directory = project / "library" / "reviews" / "decisions"
    pointer = directory / f"{packet_id}.{event_sha256[:12]}.json"
    if pointer.exists():
        return
    value = {
        "artifact_kind": "circuit_library_review_pointer",
        "packet_id": packet_id,
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
        for event_path in sorted(events_dir.glob("event-*.json")):
            try:
                raw = event_path.read_bytes()
                record: Any = json.loads(raw)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or cast(dict[str, Any], record).get("source") != "user":
                continue
            packet_id = _packet_id(cast(dict[str, Any], record))
            if packet_id is not None:
                _write_pointer(
                    project,
                    event_path,
                    packet_id,
                    hashlib.sha256(raw).hexdigest(),
                )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
