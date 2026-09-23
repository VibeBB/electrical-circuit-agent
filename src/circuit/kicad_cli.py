"""Fail-closed wrappers around kicad-cli."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field


class KicadCliError(RuntimeError):
    """Raised when kicad-cli cannot produce a valid result."""


class CompletedRun(BaseModel):
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class Violation(BaseModel):
    type: str = "unknown"
    severity: str = "unknown"
    description: str = ""
    sheet: str | None = None
    items: list[str] = Field(default_factory=list)


class Report(BaseModel):
    kind: Literal["erc", "drc"]
    source: Path
    report_path: Path
    kicad_version: str
    errors: int
    warnings: int
    exclusions: int
    unconnected: int
    violations: list[Violation] = []
    verdict: Literal["pass", "fail"]

    @classmethod
    def from_json_file(
        cls,
        path: Path,
        *,
        kind: Literal["erc", "drc"],
        source: Path,
        kicad_version: str,
    ) -> Report:
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise KicadCliError(f"could not parse {kind} report {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise KicadCliError(f"{kind} report must be a JSON object")
        data = cast(dict[str, Any], data)
        expected_schema = f"https://schemas.kicad.org/{kind}.v1.json"
        if data.get("$schema") != expected_schema:
            raise KicadCliError(f"{kind} report has unsupported schema: {data.get('$schema')!r}")
        if kind == "erc":
            violations = _erc_violations(data)
            unconnected = 0
        else:
            violations = _drc_violations(data)
            raw_unconnected = data.get("unconnected_items", [])
            if not isinstance(raw_unconnected, list):
                raise KicadCliError("drc report unconnected_items must be a list")
            unconnected = len(cast(list[Any], raw_unconnected))
        errors = sum(v.severity.lower() == "error" for v in violations)
        warnings = sum(v.severity.lower() == "warning" for v in violations)
        exclusions = sum(v.severity.lower() == "exclusion" for v in violations)
        return cls(
            kind=kind,
            source=source,
            report_path=path,
            kicad_version=kicad_version,
            errors=errors,
            warnings=warnings,
            exclusions=exclusions,
            unconnected=unconnected,
            violations=violations,
            verdict="pass" if errors == 0 and unconnected == 0 else "fail",
        )


def _violation(item: dict[str, Any], *, sheet: str | None = None) -> Violation:
    for key in ("type", "severity", "description"):
        if not isinstance(item.get(key), str):
            raise KicadCliError(f"violation {key} must be a string")
    severity = item["severity"].lower()
    if severity not in {"error", "warning", "exclusion"}:
        raise KicadCliError(f"unsupported violation severity: {item['severity']!r}")
    raw_items = item.get("items", [])
    if not isinstance(raw_items, list):
        raise KicadCliError("violation items must be a list")
    items: list[str] = []
    for raw_item in cast(list[Any], raw_items):
        if not isinstance(raw_item, dict):
            raise KicadCliError("violation items must contain descriptions")
        raw_item_dict = cast(dict[str, Any], raw_item)
        description = raw_item_dict.get("description")
        if not isinstance(description, str):
            raise KicadCliError("violation items must contain descriptions")
        items.append(description)
    return Violation(
        type=item["type"],
        severity=item["severity"],
        description=item["description"],
        sheet=sheet,
        items=items,
    )


def _violation_list(value: Any, *, sheet: str | None = None) -> list[Violation]:
    if not isinstance(value, list):
        raise KicadCliError("violation group must be a list")
    violations: list[Violation] = []
    for item in cast(list[Any], value):
        if not isinstance(item, dict):
            raise KicadCliError("violation entries must be objects")
        violations.append(_violation(cast(dict[str, Any], item), sheet=sheet))
    return violations


def _erc_violations(data: dict[str, Any]) -> list[Violation]:
    sheets = data.get("sheets")
    if not isinstance(sheets, list):
        raise KicadCliError("erc report sheets must be a list")
    violations: list[Violation] = []
    for raw_sheet in cast(list[Any], sheets):
        if not isinstance(raw_sheet, dict):
            raise KicadCliError("erc report sheets must contain paths")
        sheet = cast(dict[str, Any], raw_sheet)
        path = sheet.get("path")
        if not isinstance(path, str):
            raise KicadCliError("erc report sheets must contain paths")
        violations.extend(_violation_list(sheet.get("violations"), sheet=path))
    return violations


def _drc_violations(data: dict[str, Any]) -> list[Violation]:
    violations: list[Violation] = []
    for key in ("unconnected_items", "violations", "schematic_parity"):
        if key not in data:
            raise KicadCliError(f"drc report missing {key}")
        violations.extend(_violation_list(data[key]))
    return violations


def command_prefix() -> list[str]:
    return shlex.split(os.environ.get("CIRCUIT_KICAD_CLI", "kicad-cli"))


def run(args: list[str], *, cwd: Path | None = None, timeout_s: float = 600.0) -> CompletedRun:
    command = [*command_prefix(), *args]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KicadCliError(f"kicad-cli failed: {exc}") from exc
    return CompletedRun(
        args=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def version() -> str:
    result = run(["--version"])
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or "kicad-cli --version failed")
    return result.stdout.strip()


def _report(kind: Literal["erc", "drc"], source: Path, output: Path) -> Report:
    result = run(
        [
            "sch" if kind == "erc" else "pcb",
            "erc" if kind == "erc" else "drc",
            "--format",
            "json",
            "--severity-all",
            "--output",
            str(output),
            str(source),
        ]
    )
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or f"kicad-cli {kind} failed")
    if not output.is_file():
        raise KicadCliError(result.stderr.strip() or "kicad-cli produced no report")
    return Report.from_json_file(
        output,
        kind=kind,
        source=source,
        kicad_version=version(),
    )


def _cache_sidecar(output: Path) -> Path:
    return output.with_name(output.name + ".src_sha256")


def _cached_report(output: Path, *, kind: Literal["erc", "drc"], source: Path) -> Report | None:
    sidecar = _cache_sidecar(output)
    if not output.is_file() or not sidecar.is_file():
        return None
    try:
        recorded = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if recorded != hashlib.sha256(source.read_bytes()).hexdigest():
        return None
    try:
        with output.open(encoding="utf-8") as handle:
            data: object = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    kicad_version = cast(dict[str, Any], data).get("kicad_version")
    if not isinstance(kicad_version, str):
        return None
    try:
        return Report.from_json_file(output, kind=kind, source=source, kicad_version=kicad_version)
    except KicadCliError:
        return None


def erc(sch: Path, out: Path) -> Report:
    cached = _cached_report(out, kind="erc", source=sch)
    if cached is not None:
        return cached
    report = _report("erc", sch, out)
    _cache_sidecar(out).write_text(hashlib.sha256(sch.read_bytes()).hexdigest(), encoding="utf-8")
    return report


def drc(pcb: Path, out: Path) -> Report:
    cached = _cached_report(out, kind="drc", source=pcb)
    if cached is not None:
        return cached
    report = _report("drc", pcb, out)
    _cache_sidecar(out).write_text(hashlib.sha256(pcb.read_bytes()).hexdigest(), encoding="utf-8")
    return report


def export_netlist(sch: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    result = run(
        [
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            "-o",
            str(out),
            str(sch),
        ]
    )
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or "kicad-cli netlist export failed")
    if not out.is_file():
        raise KicadCliError(result.stderr.strip() or "kicad-cli produced no netlist")
    return out


ExportKind = Literal[
    "gerbers",
    "drill",
    "pos",
    "bom",
    "netlist",
    "pdf-sch",
    "step",
    "sch_pdf",
    "sch_svg",
    "pcb_pdf",
    "pcb_svg",
    "dxf",
    "ipc2581",
    "odb",
    "gencad",
    "vrml",
    "glb",
    "fp_svg",
]


DiffFormat = Literal["json", "png", "svg"]


class DiffReport(BaseModel):
    kind: Literal["sch", "pcb"]
    left: Path
    right: Path
    identical: bool
    exit_code: int
    output: Path
    format: DiffFormat = "json"
    changes: list[dict[str, object]] = Field(default_factory=lambda: list[dict[str, object]]())


class JobsetResult(BaseModel):
    jobset: Path
    project: Path
    output_dir: Path
    exit_code: int
    outputs: list[Path] = Field(default_factory=lambda: list[Path]())
    erc_report: Path | None = None
    drc_report: Path | None = None


def export(kind: ExportKind, source: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if kind in {"gerbers", "drill"}:
        args = ["pcb", "export", kind, "--output", str(out_dir) + "/", str(source)]
    elif kind == "pos":
        args = [
            "pcb",
            "export",
            "pos",
            "--output",
            str(out_dir / f"{source.stem}-pos.csv"),
            str(source),
        ]
    elif kind == "step":
        args = ["pcb", "export", "step", "--output", str(out_dir / "board.step"), str(source)]
    elif kind == "sch_pdf":
        args = [
            "sch",
            "export",
            "pdf",
            "--output",
            str(out_dir / f"{source.stem}.pdf"),
            str(source),
        ]
    elif kind == "sch_svg":
        # kicad-cli treats -o as an output directory for svg and writes
        # <dir>/<stem>.svg inside it.
        args = [
            "sch",
            "export",
            "svg",
            "--output",
            str(out_dir),
            str(source),
        ]
    elif kind in {"pcb_pdf", "pcb_svg", "dxf"}:
        export_kind = {"pcb_pdf": "pdf", "pcb_svg": "svg"}.get(kind, kind)
        mode = {
            "pcb_pdf": "--mode-multipage",
            "pcb_svg": "--mode-multi",
            "dxf": "--mode-multi",
        }[kind]
        output = str(out_dir / f"{source.stem}.pdf") if kind == "pcb_pdf" else str(out_dir)
        args = [
            "pcb",
            "export",
            export_kind,
            mode,
            "--layers",
            "F.Cu,B.Cu,Edge.Cuts",
            "--output",
            output,
            str(source),
        ]
    elif kind == "fp_svg":
        # kicad-cli 10.99 resolves the footprint library, so the input must be
        # a library directory containing .kicad_mod files; a bare .kicad_mod
        # file path fails with "Footprint library does not exist".
        args = [
            "fp",
            "export",
            "svg",
            "--sketch-pads-on-fab-layers",
            "--sketch-pad-numbers",
            "--output",
            str(out_dir),
            str(source),
        ]
    elif kind in {"ipc2581", "odb", "gencad", "vrml", "glb"}:
        suffix = {"ipc2581": "xml", "odb": "zip", "gencad": "cad", "vrml": "wrl", "glb": "glb"}[
            kind
        ]
        args = [
            "pcb",
            "export",
            kind,
            "--output",
            str(out_dir / f"board.{suffix}"),
            str(source),
        ]
    else:
        mapping = {"bom": "bom", "netlist": "netlist", "pdf-sch": "pdf"}
        suffix = {"bom": ".csv", "netlist": ".net", "pdf-sch": ".pdf"}[kind]
        args = [
            "sch",
            "export",
            mapping[kind],
            "--output",
            str(out_dir / f"{source.stem}{suffix}"),
            str(source),
        ]
    result = run(args)
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or f"export {kind} failed")
    return sorted(path for path in out_dir.rglob("*") if path.is_file())


def _require_input(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise KicadCliError(f"{label} is missing or empty: {path}")


CameraSide = Literal["top", "bottom", "left", "right", "front", "back"]
RenderBackground = Literal["default", "transparent", "opaque"]
RenderQuality = Literal["basic", "high", "user", "job_settings"]

_XYZ_PATTERN = re.compile(r"^-?\d+(?:\.\d+)?(,-?\d+(?:\.\d+)?){2}$")


def _xyz_arg(name: str, value: str | None) -> list[str]:
    if value is None:
        return []
    if not _XYZ_PATTERN.match(value):
        raise KicadCliError(f"{name} must be three comma-separated numbers 'X,Y,Z'")
    return [f"--{name}", value]


def render(
    pcb: Path,
    out: Path,
    *,
    side: CameraSide = "top",
    width: int = 1280,
    height: int = 720,
    rotate: str | None = None,
    zoom: float | None = None,
    pan: str | None = None,
    pivot: str | None = None,
    perspective: bool = False,
    floor: bool = False,
    background: RenderBackground | None = None,
    quality: RenderQuality | None = None,
) -> Path:
    _require_input(pcb, "PCB")
    if width <= 0 or height <= 0:
        raise KicadCliError("render dimensions must be positive")
    if zoom is not None and zoom <= 0:
        raise KicadCliError("render zoom must be positive")
    args = [
        "pcb",
        "render",
        "--output",
        str(out),
        "--width",
        str(width),
        "--height",
        str(height),
        "--side",
        side,
    ]
    args += _xyz_arg("rotate", rotate)
    args += _xyz_arg("pan", pan)
    args += _xyz_arg("pivot", pivot)
    if zoom is not None:
        args += ["--zoom", str(zoom)]
    if perspective:
        args.append("--perspective")
    if floor:
        args.append("--floor")
    if background is not None:
        args += ["--background", background]
    if quality is not None:
        args += ["--quality", quality]
    args.append(str(pcb))
    out.parent.mkdir(parents=True, exist_ok=True)
    result = run(args)
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or "kicad-cli render failed")
    if not out.is_file() or out.stat().st_size == 0:
        raise KicadCliError("kicad-cli produced no render")
    return out


def _collect_pngs(out_dir: Path, stem: str) -> list[Path]:
    images = sorted(path for path in out_dir.glob(f"{stem}*.png") if path.is_file())
    if not images:
        raise KicadCliError(f"kicad-cli produced no PNG output in {out_dir}")
    return images


def render_schematic(
    sch: Path,
    out_dir: Path,
    *,
    pages: str | None = None,
    dpi: int = 300,
    black_and_white: bool = False,
    exclude_drawing_sheet: bool = False,
    theme: str | None = None,
) -> list[Path]:
    _require_input(sch, "schematic")
    if dpi <= 0:
        raise KicadCliError("render dpi must be positive")
    args = [
        "sch",
        "export",
        "png",
        "--output",
        str(out_dir),
        "--dpi",
        str(dpi),
    ]
    if pages:
        args += ["--pages", pages]
    if black_and_white:
        args.append("--black-and-white")
    if exclude_drawing_sheet:
        args.append("--exclude-drawing-sheet")
    if theme:
        args += ["--theme", theme]
    args.append(str(sch))
    out_dir.mkdir(parents=True, exist_ok=True)
    result = run(args)
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or "kicad-cli schematic png export failed")
    return _collect_pngs(out_dir, sch.stem)


def render_layers(
    pcb: Path,
    out_dir: Path,
    *,
    layers: str,
    common_layers: str | None = None,
    mirror: bool = False,
    scale: int | None = None,
    sketch_pads_on_fab_layers: bool = False,
    sketch_pad_numbers: bool = False,
    black_and_white: bool = False,
    include_border_title: bool = False,
    dpi: int = 300,
    theme: str | None = None,
) -> list[Path]:
    _require_input(pcb, "PCB")
    if not layers.strip():
        raise KicadCliError("layers must name at least one layer")
    if dpi <= 0:
        raise KicadCliError("render dpi must be positive")
    if scale is not None and scale < 0:
        raise KicadCliError("scale must be non-negative")
    args = [
        "pcb",
        "export",
        "png",
        "--output",
        str(out_dir),
        "--layers",
        layers,
        "--dpi",
        str(dpi),
    ]
    if common_layers:
        args += ["--common-layers", common_layers]
    if mirror:
        args.append("--mirror")
    if scale is not None:
        args += ["--scale", str(scale)]
    if sketch_pads_on_fab_layers:
        args.append("--sketch-pads-on-fab-layers")
    if sketch_pad_numbers:
        args.append("--sketch-pad-numbers")
    if black_and_white:
        args.append("--black-and-white")
    if include_border_title:
        args.append("--include-border-title")
    if theme:
        args += ["--theme", theme]
    args.append(str(pcb))
    out_dir.mkdir(parents=True, exist_ok=True)
    result = run(args)
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or "kicad-cli pcb png export failed")
    return _collect_pngs(out_dir, pcb.stem)


def diff(
    kind: Literal["sch", "pcb"],
    left: Path,
    right: Path,
    out: Path,
    *,
    format: DiffFormat = "json",
) -> DiffReport:
    _require_input(left, "left input")
    _require_input(right, "right input")
    out.parent.mkdir(parents=True, exist_ok=True)
    result = run(
        [
            kind,
            "diff",
            "--format",
            format,
            "--output",
            str(out),
            str(left),
            str(right),
        ]
    )
    if result.returncode not in {0, 5}:
        raise KicadCliError(result.stderr.strip() or f"kicad-cli {kind} diff failed")
    if not out.is_file() or out.stat().st_size == 0:
        raise KicadCliError("kicad-cli produced no diff report")
    changes: list[dict[str, object]] = []
    if format == "json":
        try:
            with out.open(encoding="utf-8") as handle:
                value: object = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise KicadCliError(f"could not parse {kind} diff report {out}: {exc}") from exc
        if not isinstance(value, dict):
            raise KicadCliError(f"{kind} diff report has no changes list")
        diff_data = cast(dict[str, Any], value)
        if not isinstance(diff_data.get("changes"), list):
            raise KicadCliError(f"{kind} diff report has no changes list")
        changes = cast(list[dict[str, object]], diff_data["changes"])
    return DiffReport(
        kind=kind,
        left=left,
        right=right,
        identical=result.returncode == 0,
        exit_code=result.returncode,
        output=out,
        format=format,
        changes=changes,
    )


def _jobset_with_output(jobset: Path, output_dir: Path) -> dict[str, object]:
    try:
        data = json.loads(jobset.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KicadCliError(f"could not parse jobset {jobset}: {exc}") from exc
    if not isinstance(data, dict):
        raise KicadCliError("jobset must contain an outputs list")
    jobset_data = cast(dict[str, Any], data)
    if not isinstance(jobset_data.get("outputs"), list):
        raise KicadCliError("jobset must contain an outputs list")
    outputs = cast(list[Any], jobset_data["outputs"])
    for raw_destination in outputs:
        if not isinstance(raw_destination, dict):
            raise KicadCliError("jobset outputs must contain objects")
        destination = cast(dict[str, Any], raw_destination)
        if destination.get("type") == "folder":
            settings = destination.get("settings")
            if not isinstance(settings, dict):
                raise KicadCliError("jobset folder output requires settings")
            settings["output_path"] = str(output_dir)
    return cast(dict[str, object], jobset_data)


def jobset_run(
    project_pro: Path,
    out_dir: Path,
    jobset: Path | None = None,
) -> JobsetResult:
    _require_input(project_pro, "project")
    selected_jobset = jobset or Path(__file__).parent / "data" / "default.kicad_jobset"
    _require_input(selected_jobset, "jobset")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="circuit-jobset-") as temp_dir:
        run_jobset = Path(temp_dir) / selected_jobset.name
        run_jobset.write_text(
            json.dumps(_jobset_with_output(selected_jobset, out_dir), indent=2) + "\n",
            encoding="utf-8",
        )
        result = run(
            [
                "jobset",
                "run",
                "--file",
                str(run_jobset),
                str(project_pro),
            ]
        )
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or "kicad-cli jobset failed")
    outputs = sorted(path for path in out_dir.rglob("*") if path.is_file())
    if not outputs:
        raise KicadCliError("kicad-cli jobset produced no outputs")
    return JobsetResult(
        jobset=selected_jobset,
        project=project_pro,
        output_dir=out_dir,
        exit_code=result.returncode,
        outputs=outputs,
        erc_report=next((path for path in outputs if path.name == "erc.json"), None),
        drc_report=next((path for path in outputs if path.name == "drc.json"), None),
    )


def reports_equivalent(a: Path, b: Path) -> bool:
    try:
        left = json.loads(a.read_text(encoding="utf-8"))
        right = json.loads(b.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_data = cast(dict[str, Any], left)
    right_data = cast(dict[str, Any], right)
    left_data.pop("date", None)
    right_data.pop("date", None)
    return left_data == right_data


def version_about() -> str:
    result = run(["version", "--format", "about"])
    if result.returncode:
        raise KicadCliError(result.stderr.strip() or "kicad-cli version --format about failed")
    output = result.stdout.strip()
    if not output:
        raise KicadCliError("kicad-cli version --format about returned no output")
    return output
