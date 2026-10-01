"""Prevent blind datasheet authors from reading the other lane or sealed comparison data."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, cast


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for child in cast(dict[str, Any], value).values() for text in _strings(child)]
    if isinstance(value, list):
        return [text for child in cast(list[Any], value) for text in _strings(child)]
    return []


def _blocked(payload: dict[str, object], lane: str) -> bool:
    name = payload.get("tool_name")
    if isinstance(name, str) and "circuit_part_author_compare" in name:
        return True
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    values = _strings(tool_input)
    if any(
        re.search(r"(?:^|[/\\])sealed(?:[/\\]|$)", value)
        or re.search(r"(?:^|[/\\])comparison\.json$", value)
        or re.search(r"(?:^|[/\\])commits\.jsonl$", value)
        for value in values
    ):
        return True
    other = "b" if lane == "a" else "a"
    return any(re.search(rf"(?:^|[/\\]){other}(?:[/\\]|$)", value) for value in values)


def main() -> int:
    lane = os.environ.get("CIRCUIT_AUTHORING_LANE", "")
    if lane not in {"a", "b"}:
        return 0
    try:
        payload: object = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"invalid author-lane hook input: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("invalid author-lane hook input: not an object", file=sys.stderr)
        return 2
    if _blocked(cast(dict[str, object], payload), lane):
        print("blind authoring lane context is isolated", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
