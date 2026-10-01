"""Record successful lane-seal hashes for fresh authoring consensus checks."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _provenance import actor, event_id, events_path, next_sequence

EVENTS_ENV = "CIRCUIT_AUTHORING_EVENTS"
EVENTS_RELATIVE_PATH = Path("observations/circuit/authoring-events.jsonl")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for child in cast(dict[str, Any], value).values() for text in _strings(child)]
    if isinstance(value, list):
        return [text for child in cast(list[Any], value) for text in _strings(child)]
    return []


def _commit(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict):
        record = cast(dict[str, object], value)
        lane = record.get("lane")
        digest = record.get("sha256")
        if (
            isinstance(lane, str)
            and lane in {"a", "b"}
            and isinstance(digest, str)
            and _SHA256.fullmatch(digest)
        ):
            return {"lane": lane, "sha256": digest}
    for text in _strings(value):
        try:
            result = _commit(json.loads(text))
        except json.JSONDecodeError:
            continue
        if result is not None:
            return result
    if isinstance(value, dict):
        for child in cast(dict[str, Any], value).values():
            result = _commit(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in cast(list[Any], value):
            result = _commit(child)
            if result is not None:
                return result
    return None


def main() -> int:
    try:
        payload: object = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    data = cast(dict[str, object], payload)
    if data.get("tool_name") != "circuit_part_author_commit":
        return 0
    response = data.get("tool_response")
    if not isinstance(response, dict):
        return 0
    response_data = cast(dict[str, object], response)
    if "error" in response_data or response_data.get("is_error"):
        return 0
    commit = _commit(response_data)
    if commit is None:
        return 0
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    input_data = cast(dict[str, object], tool_input)
    run_dir = input_data.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return 0
    working_dir = data.get("working_dir")
    base_dir = os.environ.get("OPENHANDS_PROJECT_DIR") or (
        working_dir if isinstance(working_dir, str) else "."
    )
    base = Path(base_dir).resolve()
    run_path = Path(run_dir)
    if not run_path.is_absolute():
        run_path = base / run_path
    record = {
        **commit,
        "run_dir": str(run_path.resolve()),
        "recorded_at": datetime.now(UTC).isoformat(),
        "session_id": data.get("session_id"),
        "actor": actor(cast(dict[str, Any], data)),
        "tool_call_id": data.get("tool_call_id") or data.get("action_id"),
    }
    identity = cast(dict[str, Any], {"sequence": 0, **record})
    path = events_path(cast(dict[str, Any], data), EVENTS_ENV, EVENTS_RELATIVE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sequence = next_sequence(path)
        identity["sequence"] = sequence
        record["sequence"] = sequence
        record["event_id"] = event_id(identity)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"authoring event log unavailable: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
