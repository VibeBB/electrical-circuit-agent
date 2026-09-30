from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Literal, cast

from mcp.types import ImageContent
from pydantic import ValidationError

from . import kicad_cli
from .advisory import AdvisoryResult

__all__ = [
    "_IMAGE_MIME",
    "_MAX_INLINE_IMAGES",
    "_collect_advisory",
    "_collect_diffs",
    "_collect_exports",
    "_collect_jobset",
    "_collect_renders",
    "_image_content",
    "_jobset_consistent",
    "_load_report",
    "_reports_dirs",
]

_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_MAX_INLINE_IMAGES = 4


def _image_content(path: Path) -> ImageContent | None:
    mime = _IMAGE_MIME.get(path.suffix.lower())
    if mime is None or not path.is_file():
        return None
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return ImageContent(type="image", data=data, mimeType=mime)


def _load_report(path: Path, *, kind: str, source: Path) -> kicad_cli.Report:
    try:
        with path.open(encoding="utf-8") as handle:
            value: object = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise kicad_cli.KicadCliError(f"could not read {kind} report {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise kicad_cli.KicadCliError(f"{kind} report has no kicad_version")
    value = cast(dict[str, object], value)
    kicad_version = value.get("kicad_version")
    if not isinstance(kicad_version, str):
        raise kicad_cli.KicadCliError(f"{kind} report has no kicad_version")
    return kicad_cli.Report.from_json_file(
        path,
        kind=cast(Literal["erc", "drc"], kind),
        source=source,
        kicad_version=kicad_version,
    )


def _reports_dirs(schematic: Path, board: Path) -> list[Path]:
    directories: list[Path] = []
    for source in (schematic, board):
        directory = source.parent / "circuit-reports"
        if directory.is_dir() and directory not in directories:
            directories.append(directory)
    return directories


def _collect_exports(project_dirs: list[Path]) -> dict[str, list[str]]:
    exports: dict[str, list[str]] = {}
    for project_dir in project_dirs:
        root = project_dir / "exports"
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir():
                files = [str(p) for p in sorted(child.rglob("*")) if p.is_file()]
                if files:
                    exports[child.name] = files
            elif child.is_file():
                exports.setdefault("exports", []).append(str(child))
    return exports


def _collect_advisory(directories: list[Path]) -> list[AdvisoryResult]:
    results: list[AdvisoryResult] = []
    for directory in directories:
        for path in sorted(directory.glob("*.advisory.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                results.append(AdvisoryResult.model_validate(value))
            except (OSError, json.JSONDecodeError, ValidationError):
                continue
        journal = directory / "advisory.jsonl"
        if not journal.is_file():
            continue
        try:
            lines = journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                results.append(AdvisoryResult.model_validate_json(line))
            except ValidationError:
                continue
    return results


def _collect_renders(directories: list[Path]) -> list[str]:
    renders: list[str] = []
    for directory in directories:
        for pattern in ("*.png", "*.jpg", "*.jpeg"):
            renders.extend(str(path) for path in sorted(directory.glob(pattern)))
    return renders


def _collect_diffs(directories: list[Path]) -> dict[str, kicad_cli.DiffReport]:
    diffs: dict[str, kicad_cli.DiffReport] = {}
    for directory in directories:
        for path in sorted(directory.glob("*.diff.json")):
            try:
                diffs[path.name[: -len(".diff.json")]] = kicad_cli.DiffReport.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                continue
    return diffs


def _collect_jobset(directories: list[Path]) -> kicad_cli.JobsetResult | None:
    for directory in directories:
        for path in sorted(directory.glob("*.jobset.json"), reverse=True):
            try:
                return kicad_cli.JobsetResult.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                continue
    return None


def _jobset_consistent(
    jobset: kicad_cli.JobsetResult | None,
    erc_report: kicad_cli.Report | None,
    drc_report: kicad_cli.Report | None,
) -> bool | None:
    if jobset is None:
        return None
    checks: list[bool] = []
    if jobset.erc_report is not None and erc_report is not None:
        checks.append(kicad_cli.reports_equivalent(jobset.erc_report, erc_report.report_path))
    if jobset.drc_report is not None and drc_report is not None:
        checks.append(kicad_cli.reports_equivalent(jobset.drc_report, drc_report.report_path))
    return all(checks) if checks else None
