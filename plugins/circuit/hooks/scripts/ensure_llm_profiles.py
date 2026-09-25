#!/usr/bin/env python3
"""SessionStart hook: provision the vibebb-* LLM profiles agents need.

Every circuit sub-agent declares `model: vibebb-author` or `model:
vibebb-review`, resolved through the SDK's LLMProfileStore
(~/.openhands/profiles/<name>.json). A missing profile raises ValueError
at task spawn and silently disables task delegation. This hook clones the
conversation's `active_profile` into the two vibebb profile slots when
they are absent so `task` works out of the box; operators can then edit
the files to route each lane at a different model.

Advisory only: reads settings, writes absent files, prints a JSON
finding, always exits 0.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

_PROFILES = ("vibebb-author", "vibebb-review")


def _settings() -> dict[str, object]:
    path = Path.home() / ".openhands" / "settings.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def main() -> int:
    settings = _settings()
    active = settings.get("active_profile")
    findings: list[str] = []
    store = Path.home() / ".openhands" / "profiles"

    if not isinstance(active, str) or not active:
        findings.append("no active_profile in ~/.openhands/settings.json")
    else:
        src = store / f"{active}.json"
        try:
            template = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            template = None
            findings.append(f"active_profile {active!r} unreadable at {src}")
        if isinstance(template, dict):
            for name in _PROFILES:
                dest = store / f"{name}.json"
                if dest.is_file():
                    continue
                try:
                    _atomic_write(dest, json.dumps(template, indent=2) + "\n")
                    findings.append(f"provisioned {name} from {active}")
                except OSError as exc:
                    findings.append(f"could not write {dest}: {exc}")

    missing = [n for n in _PROFILES if not (store / f"{n}.json").is_file()]
    print(
        json.dumps(
            {
                "hook": "ensure-llm-profiles",
                "profiles": _PROFILES,
                "missing": missing,
                "findings": findings,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
