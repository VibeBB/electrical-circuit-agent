#!/usr/bin/env python3
"""SessionStart hook: provision blind PartSpec author profiles."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import cast

_PROFILES = ("vibebb-part-author-a", "vibebb-part-author-b")


def _settings() -> dict[str, object]:
    path = Path.home() / ".openhands" / "settings.json"
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return cast(dict[str, object], data)


def _read_profile(path: Path) -> dict[str, object] | None:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return cast(dict[str, object], data)


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(payload)
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
        template = _read_profile(store / f"{active}.json")
        if template is None:
            findings.append(f"active_profile {active!r} is unreadable")
        else:
            for name in _PROFILES:
                destination = store / f"{name}.json"
                if destination.is_file():
                    continue
                try:
                    _atomic_write(destination, json.dumps(template, indent=2) + "\n")
                    findings.append(f"provisioned {name} from {active}")
                except OSError as exc:
                    findings.append(f"could not write {destination}: {exc}")

    missing = [name for name in _PROFILES if not (store / f"{name}.json").is_file()]
    profiles = {name: _read_profile(store / f"{name}.json") for name in _PROFILES}
    models = [
        profile.get("model")
        for profile in profiles.values()
        if profile is not None and isinstance(profile.get("model"), str) and profile["model"]
    ]
    if len(models) != len(_PROFILES):
        diversity = "unknown"
    else:
        diversity = "distinct" if models[0] != models[1] else "same"
    if diversity != "distinct":
        findings.append(
            "blind authoring profiles are not known to use distinct models; "
            "configure vibebb-part-author-a and vibebb-part-author-b independently"
        )

    print(
        json.dumps(
            {
                "hook": "ensure-part-author-profiles",
                "profiles": _PROFILES,
                "missing": missing,
                "authoring_model_diversity": diversity,
                "findings": findings,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
