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


def _profile_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


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
    templates: dict[str, str | None] = {name: None for name in _PROFILES}
    preserved: list[str] = []

    if not isinstance(active, str) or not active:
        findings.append("no active_profile in ~/.openhands/settings.json")
    else:
        template = _read_profile(store / f"{active}.json")
        if template is None:
            findings.append(f"active_profile {active!r} is unreadable")
        else:
            author_b_name = active
            author_b_template = template
            active_model = template.get("model")
            if isinstance(active_model, str):
                for candidate_path in sorted(store.glob("*.json"), key=lambda path: path.name):
                    if candidate_path.stem.startswith("vibebb-"):
                        continue
                    candidate = _read_profile(candidate_path)
                    if candidate is None:
                        continue
                    candidate_model = candidate.get("model")
                    if isinstance(candidate_model, str) and candidate_model != active_model:
                        author_b_name = candidate_path.stem
                        author_b_template = candidate
                        break
            profile_templates = {
                "vibebb-part-author-a": (active, template),
                "vibebb-part-author-b": (author_b_name, author_b_template),
            }
            for name, (source_name, profile_template) in profile_templates.items():
                destination = store / f"{name}.json"
                if _profile_exists(destination):
                    preserved.append(name)
                    continue
                templates[name] = source_name
                try:
                    _atomic_write(
                        destination,
                        json.dumps(profile_template, indent=2) + "\n",
                    )
                    findings.append(f"provisioned {name} from {source_name}")
                except OSError as exc:
                    findings.append(f"could not write {destination}: {exc}")

    missing = [name for name in _PROFILES if not _profile_exists(store / f"{name}.json")]
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
                "templates": templates,
                "preserved": preserved,
                "findings": findings,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
