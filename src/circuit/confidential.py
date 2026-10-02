"""Helpers for project-local confidential artifacts."""

from __future__ import annotations

from pathlib import Path

_GITIGNORE = "*\n!.gitignore\n"


def project_root_for(path: Path) -> Path:
    resolved = path.resolve()
    for ancestor in (resolved, *resolved.parents):
        if ancestor.name in {"library", ".confidential"}:
            return ancestor.parent
    return resolved.parent


def confidential_root_for(path: Path) -> Path | None:
    resolved = path.resolve()
    for ancestor in (resolved, *resolved.parents):
        if ancestor.name == ".confidential":
            return ancestor
    return None


def ensure_confidential_store(project: Path) -> Path:
    root = project.resolve() / ".confidential"
    root.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".gitignore"
    if gitignore.exists():
        current = gitignore.read_text(encoding="utf-8")
        if "*" not in current.splitlines() or "!.gitignore" not in current.splitlines():
            content = current.rstrip("\n")
            gitignore.write_text(
                f"{content}\n{_GITIGNORE}" if content else _GITIGNORE,
                encoding="utf-8",
            )
    else:
        gitignore.write_text(_GITIGNORE, encoding="utf-8")
    return root
