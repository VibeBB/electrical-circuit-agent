"""Reject writes to bundled KiCad library trees."""

from __future__ import annotations

import json
import sys
from pathlib import PurePath
from typing import Any


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"invalid hook input: {exc}", file=sys.stderr)
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
