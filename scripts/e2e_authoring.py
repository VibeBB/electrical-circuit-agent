#!/usr/bin/env python3
"""Run the deterministic design-brief authoring flow."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from circuit import __version__ as _circuit_version
from circuit import (
    apiserver,
    brief,
    intake,
    kicad_cli,
    libraries,
    netlist,
    report,
    sch_lint,
    titleblock,
)
from circuit.advisory import AdvisoryResult, merge_detail
from konnect_client import call_tool, notify, request

# Cold-start toolset loads can exceed the default 30s MCP timeout while
# Konnect brings the KiCad session up; keep them on a longer budget.
LOAD_TOOLSET_TIMEOUT = 180.0

TOOLSETS = [
    "project",
    "library",
    "sch_components",
    "sch_wiring",
    "sch_analysis",
    "sch_batch",
    "sch_export",
    "pcb_board",
    "pcb_components",
    "pcb_routing",
    "placement",
    "pcb_export",
    "verification",
    "manufacturing",
    "design_review",
    "templates",
]


class StepFailure(RuntimeError):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


Point = tuple[float, float]
Segment = tuple[Point, Point]
AdvisoryStage = Literal["intake", "schematic", "layout", "review", "manufacturing"]
AdvisoryStatus = Literal["ok", "error", "not_applicable"]


def _axis_segment_distance(point: Point, start: Point, end: Point) -> float:
    x, y = point
    x1, y1 = start
    x2, y2 = end
    if abs(x1 - x2) < 1e-6:
        if min(y1, y2) <= y <= max(y1, y2):
            return abs(x - x1)
        return math.hypot(x - x1, min(abs(y - y1), abs(y - y2)))
    if abs(y1 - y2) < 1e-6:
        if min(x1, x2) <= x <= max(x1, x2):
            return abs(y - y1)
        return math.hypot(min(abs(x - x1), abs(x - x2)), y - y1)
    return math.inf


def _segments_intersect(first: Segment, second: Segment) -> bool:
    (x1, y1), (x2, y2) = first
    (x3, y3), (x4, y4) = second
    first_horizontal = abs(y1 - y2) < 1e-6
    second_horizontal = abs(y3 - y4) < 1e-6
    if first_horizontal and second_horizontal:
        return abs(y1 - y3) < 1e-6 and max(min(x1, x2), min(x3, x4)) <= min(
            max(x1, x2), max(x3, x4)
        )
    if not first_horizontal and not second_horizontal:
        return abs(x1 - x3) < 1e-6 and max(min(y1, y2), min(y3, y4)) <= min(
            max(y1, y2), max(y3, y4)
        )
    horizontal, vertical = (first, second) if first_horizontal else (second, first)
    (hx1, hy), (hx2, _) = horizontal
    (vx, vy1), (_, vy2) = vertical
    return min(hx1, hx2) <= vx <= max(hx1, hx2) and min(vy1, vy2) <= hy <= max(vy1, vy2)


def _path_is_clear(
    points: list[Point],
    net_name: str,
    pad_positions: dict[tuple[str, str], Point],
    pad_clearances: dict[tuple[str, str], float],
    occupied: list[tuple[str, Segment]],
    board_width: float,
    board_height: float,
) -> bool:
    if any(not (0.0 <= x <= board_width and 0.0 <= y <= board_height) for x, y in points):
        return False
    endpoints = {points[0], points[-1]}
    segments = list(pairwise(points))
    for segment in segments:
        start, end = segment
        if abs(start[0] - end[0]) > 1e-6 and abs(start[1] - end[1]) > 1e-6:
            return False
        for key, position in pad_positions.items():
            if position in endpoints and position in segment:
                continue
            if _axis_segment_distance(position, start, end) <= pad_clearances[key]:
                return False
        for other_net, other_segment in occupied:
            if other_net != net_name and _segments_intersect(segment, other_segment):
                return False
    return True


def _route_path(
    start: Point,
    end: Point,
    net_name: str,
    pad_positions: dict[tuple[str, str], Point],
    pad_clearances: dict[tuple[str, str], float],
    occupied: list[tuple[str, Segment]],
    board_width: float,
    board_height: float,
) -> list[Point]:
    candidates = [[start, end]]
    for offset in (-2.0, 2.0, -4.0, 4.0, -6.0, 6.0):
        via_y = start[1] + offset
        candidates.append([start, (start[0], via_y), (end[0], via_y), end])
    for points in candidates:
        compact = [points[0]]
        for point in points[1:]:
            if point != compact[-1]:
                compact.append(point)
        if _path_is_clear(
            compact,
            net_name,
            pad_positions,
            pad_clearances,
            occupied,
            board_width,
            board_height,
        ):
            return compact
    raise StepFailure("route_trace", f"no clear route between {start} and {end}")


def summarize(value: object) -> object:
    if isinstance(value, dict):
        value = cast(dict[str, Any], value)
        keys = (
            "status",
            "source",
            "message",
            "error",
            "valid",
            "plan_revision",
            "violations",
            "unconnected_items",
            "shorted_nets",
            "components",
            "nets",
            "output",
        )
        selected: dict[str, object] = {key: value[key] for key in keys if key in value}
        return selected or value
    if isinstance(value, list):
        value = cast(list[Any], value)
        return {"count": len(value), "first": value[:3]}
    return value


def _revision(value: object) -> str | None:
    if isinstance(value, dict):
        value = cast(dict[str, Any], value)
        found = value.get("plan_revision")
        if isinstance(found, str):
            return found
        for child in value.values():
            revision = _revision(cast(object, child))
            if revision:
                return revision
    elif isinstance(value, list):
        value = cast(list[Any], value)
        for child in value:
            revision = _revision(cast(object, child))
            if revision:
                return revision
    return None


def _pins(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        value = cast(dict[str, Any], value)
        for key in ("number", "num"):
            item = value.get(key)
            if isinstance(item, (str, int, float)):
                found.add(str(item))
        for child in value.values():
            found.update(_pins(cast(object, child)))
    elif isinstance(value, list):
        value = cast(list[Any], value)
        for child in value:
            found.update(_pins(cast(object, child)))
    return found


def _has_short(value: object) -> bool:
    if isinstance(value, dict):
        value = cast(dict[str, Any], value)
        for key in ("shorted", "shorted_nets", "shorts"):
            item = value.get(key)
            if item is True or (isinstance(item, list) and item):
                return True
        return any(_has_short(cast(object, child)) for child in value.values())
    if isinstance(value, list):
        value = cast(list[Any], value)
        return any(_has_short(cast(object, child)) for child in value)
    return False


def _record(log: TextIO, entry: dict[str, object]) -> None:
    if entry.get("result") is None:
        entry["result"] = {"status": "recorded"}
    encoded = json.dumps(entry["result"], ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded) > 2000:
        text = encoded
        truncated: dict[str, object] = {"truncated": True, "text": ""}
        while len(json.dumps(truncated, ensure_ascii=False, separators=(",", ":"))) > 2000:
            text = text[: max(1, len(text) - 100)]
            truncated["text"] = text + "..."
        entry["result"] = truncated
    log.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    log.flush()


def _call(
    process: subprocess.Popen[str],
    next_id: int,
    name: str,
    arguments: Mapping[str, object],
    log: TextIO,
    timeout: float = 30.0,
) -> tuple[object, int]:
    try:
        raw_result, next_id = call_tool(process, next_id, name, dict(arguments), timeout)
        result = raw_result
    except Exception as exc:
        _record(
            log,
            {
                "tool": name,
                "payload": arguments,
                "result": {"error": str(exc)},
                "error": True,
            },
        )
        raise StepFailure(name, str(exc)) from exc
    _record(log, {"tool": name, "payload": arguments, "result": summarize(result)})
    return result, next_id


def _advise(
    process: subprocess.Popen[str],
    next_id: int,
    name: str,
    arguments: Mapping[str, object],
    stage: AdvisoryStage,
    log: TextIO,
    results: list[AdvisoryResult],
    artifacts_fn: Callable[[object], list[str]] | None = None,
) -> tuple[object | None, int]:
    """Run a non-blocking Konnect observation and preserve every failure."""
    try:
        raw_result, next_id = call_tool(process, next_id, name, dict(arguments))
        result_value: object = cast(object, raw_result)
        status: AdvisoryStatus = "ok"
        if isinstance(result_value, dict):
            result_dict = cast(dict[str, object], result_value)
            raw_status = result_dict.get("status")
            if raw_status in {"ok", "error", "not_applicable"}:
                status = cast(AdvisoryStatus, raw_status)
        artifacts = artifacts_fn(cast(object, result_value)) if artifacts_fn is not None else []
        summary = json.dumps(summarize(cast(Any, result_value)), ensure_ascii=False, default=str)
        detail = cast(dict[str, object], result_value) if isinstance(result_value, dict) else None
        results.append(
            AdvisoryResult(
                tool=name,
                stage=stage,
                status=status,
                summary=summary,
                artifacts=artifacts,
                detail=detail,
            )
        )
        _record(
            log,
            {
                "step": name,
                "tool": name,
                "advisory": True,
                "payload": dict(arguments),
                "result": summarize(cast(Any, result_value)),
            },
        )
        return cast(object, result_value), next_id
    except Exception as exc:
        error_text = str(exc)
        results.append(
            AdvisoryResult(
                tool=name,
                stage=stage,
                status="error",
                summary=error_text,
            )
        )
        _record(
            log,
            {
                "step": name,
                "tool": name,
                "advisory": True,
                "payload": dict(arguments),
                "result": {
                    "error": error_text,
                    "status": "error",
                },
                "error": True,
            },
        )
        return None, next_id


def _artifacts_under(root: Path, workdir: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(workdir))
        for path in root.rglob("*")
        if path.is_file() and path.is_relative_to(workdir)
    )


def _advisory_detail(results: list[AdvisoryResult], tool: str, detail: dict[str, object]) -> None:
    for result in results:
        if result.tool == tool:
            merge_detail(result, detail)


def _start_konnect(
    env: dict[str, str],
    log: TextIO,
    next_id: int,
) -> tuple[subprocess.Popen[str], int]:
    process = subprocess.Popen(
        ["konnect"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        raw_response, _ = request(
            process,
            next_id,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "circuit-e2e-authoring", "version": "0.1"},
            },
            LOAD_TOOLSET_TIMEOUT,
        )
        response = raw_response
        _record(log, {"method": "initialize", "payload": {}, "result": summarize(response)})
        next_id += 1
        notify(process, "notifications/initialized")
        _record(log, {"method": "notifications/initialized", "payload": {}})
        for toolset in TOOLSETS:
            try:
                _, next_id = _call(
                    process,
                    next_id,
                    "load_toolset",
                    {"name": toolset},
                    log,
                    LOAD_TOOLSET_TIMEOUT,
                )
            except StepFailure as exc:
                if "timeout waiting for MCP response" not in str(exc):
                    raise
                # A cold KiCad session can overrun even the long budget once;
                # retry the same toolset on a fresh message id before failing.
                _, next_id = _call(
                    process,
                    next_id + 1,
                    "load_toolset",
                    {"name": toolset},
                    log,
                    LOAD_TOOLSET_TIMEOUT,
                )
        return process, next_id
    except Exception:
        process.kill()
        process.wait()
        raise


def _stop_konnect(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_socket(path: Path, process: subprocess.Popen[str], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise StepFailure("circuit_api_server_start", f"KiCad exited with {process.returncode}")
        time.sleep(0.1)
    if not path.exists():
        raise StepFailure("circuit_api_server_start", "timed out waiting for API socket")


def _write_failure(
    output_path: Path,
    result: dict[str, object],
    stage: str,
    exc: BaseException,
) -> int:
    """Record a structured JSON failure and return exit code 1 (fail-closed)."""
    result["verdict"] = "fail"
    result["stage"] = stage
    result["detail"] = str(exc)
    result["error"] = {"step": stage, "message": str(exc)}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"verdict": "fail", "stage": stage, "detail": str(exc)},
            ensure_ascii=False,
        )
    )
    return 1


def _write_provenance(
    workdir: Path,
    brief_path: Path,
    intake_path: Path | None,
    *,
    version_about: str | None,
) -> Path:
    """Emit the shared provenance.json record (inputs + tool versions)."""
    tool_versions = {"python": platform.python_version()}
    if version_about:
        tool_versions["kicad"] = version_about.splitlines()[0].strip()
    provenance = {
        "schema_version": 1,
        "license": "BSD-3-Clause",
        "generator": f"circuit-agent/{_circuit_version}",
        "brief_sha256": hashlib.sha256(brief_path.read_bytes()).hexdigest(),
        "intake_sha256": (
            hashlib.sha256(intake_path.read_bytes()).hexdigest()
            if intake_path is not None
            else None
        ),
        "tool_versions": tool_versions,
    }
    path = workdir / "provenance.json"
    path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--kicad-share", type=Path, default=Path("/usr/share/kicad-nightly"))
    parser.add_argument("--intake", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    args.workdir.mkdir(parents=True, exist_ok=True)
    output_path = args.json or args.workdir / "e2e-authoring.json"
    result: dict[str, object] = {
        "verdict": "fail",
        "brief": str(args.brief),
        "workdir": str(args.workdir),
    }
    try:
        loaded_brief = brief.load_brief(args.brief)
    except (ValueError, OSError) as exc:
        return _write_failure(output_path, result, "load", exc)
    reports_dir = args.workdir / "circuit-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.workdir / "authoring.jsonl"
    project = args.workdir / f"{loaded_brief.name}.kicad_pro"
    schematic = args.workdir / f"{loaded_brief.name}.kicad_sch"
    board = args.workdir / f"{loaded_brief.name}.kicad_pcb"
    socket_path = Path("/tmp/circuit-kicad.sock")
    socket_path.unlink(missing_ok=True)

    process: subprocess.Popen[str] | None = None
    server: subprocess.Popen[str] | None = None
    connectivity: netlist.ConnectivityReport | None = None
    sch_lint_result: sch_lint.SchLintReport | None = None
    erc_result: kicad_cli.Report | None = None
    drc_result: kicad_cli.Report | None = None
    intake_result: intake.IntakeReport | None = None
    library_result: libraries.LibraryReport | None = None
    exports: dict[str, list[str]] = {}
    advisory_results: list[AdvisoryResult] = []
    renders: list[str] = []
    jobset_result: kicad_cli.JobsetResult | None = None
    jobset_consistent: bool | None = None
    diffs: dict[str, kicad_cli.DiffReport] = {}
    version_about: str | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            konnect_exports = args.workdir / "konnect-exports"
            konnect_exports.mkdir(parents=True, exist_ok=True)
            if args.intake is not None:
                intake_result = intake.check_intake(
                    loaded_brief,
                    intake.load_intake(args.intake),
                    brief_path=args.brief,
                    intake_path=args.intake,
                )
                intake_path = reports_dir / f"{loaded_brief.name}.intake.json"
                intake_path.write_text(intake_result.model_dump_json(indent=2), encoding="utf-8")
                _record(
                    log,
                    {
                        "step": "circuit_brief_intake_check",
                        "tool": "circuit_brief_intake_check",
                        "payload": {
                            "brief_path": str(args.brief),
                            "intake_path": str(args.intake),
                            "output_path": str(intake_path),
                        },
                        "result": summarize(intake_result.model_dump(mode="json")),
                    },
                )
                if intake_result.verdict != "ready":
                    raise StepFailure("circuit_brief_intake_check", intake_result.model_dump_json())
                result["intake"] = intake_result.model_dump(mode="json")
            library_result = libraries.check_libraries(
                loaded_brief,
                brief_path=args.brief,
                roots=libraries.LibraryRoots(
                    symbol_dirs=[args.kicad_share / "symbols"],
                    footprint_dirs=[args.kicad_share / "footprints"],
                ),
            )
            library_path = reports_dir / f"{loaded_brief.name}.libraries.json"
            library_path.write_text(library_result.model_dump_json(indent=2), encoding="utf-8")
            _record(
                log,
                {
                    "step": "circuit_brief_library_check",
                    "tool": "circuit_brief_library_check",
                    "payload": {
                        "brief_path": str(args.brief),
                        "output_path": str(library_path),
                    },
                    "result": summarize(library_result.model_dump(mode="json")),
                },
            )
            if library_result.verdict != "pass":
                raise StepFailure("circuit_brief_library_check", library_result.model_dump_json())
            result["libraries"] = library_result.model_dump(mode="json")
            environment = {
                **os.environ,
                "HOME": str(args.workdir / "home"),
                "KICAD10_SYMBOL_DIR": str(args.kicad_share / "symbols"),
            }
            (args.workdir / "home").mkdir(exist_ok=True)
            process, next_id = _start_konnect(environment, log, 1)
            for advisory_name, advisory_args in (
                ("list_template_categories", {}),
                ("search_templates", {"query": loaded_brief.name}),
            ):
                _advise(
                    process,
                    next_id,
                    advisory_name,
                    advisory_args,
                    "intake",
                    log,
                    advisory_results,
                )
                next_id += 1
            _call(
                process,
                next_id,
                "create_project",
                {"path": str(args.workdir), "name": loaded_brief.name},
                log,
            )
            next_id += 1

            symbol_nicknames = sorted({part.lib_id.split(":", 1)[0] for part in loaded_brief.parts})
            footprint_nicknames = sorted(
                {part.footprint.split(":", 1)[0] for part in loaded_brief.parts}
            )
            for nickname in symbol_nicknames:
                path = args.kicad_share / "symbols" / f"{nickname}.kicad_sym"
                if not path.is_file():
                    raise StepFailure("register_symbol_library", f"missing symbol library: {path}")
                _, next_id = _call(
                    process,
                    next_id,
                    "register_symbol_library",
                    {
                        "library_path": str(path),
                        "nickname": nickname,
                        "project": str(project),
                        "scope": "project",
                    },
                    log,
                )
            for nickname in footprint_nicknames:
                path = args.kicad_share / "footprints" / f"{nickname}.pretty"
                if not path.is_dir():
                    raise StepFailure(
                        "register_footprint_library", f"missing footprint library: {path}"
                    )
                _, next_id = _call(
                    process,
                    next_id,
                    "register_footprint_library",
                    {
                        "library_path": str(path),
                        "nickname": nickname,
                        "project": str(project),
                        "scope": "project",
                    },
                    log,
                )

            for lib_id in sorted({part.lib_id for part in loaded_brief.parts}):
                info, next_id = _call(
                    process,
                    next_id,
                    "get_symbol_info",
                    {"lib_id": lib_id, "project_dir": str(args.workdir)},
                    log,
                )
                pins = _pins(info)
                required = {
                    pin.split(".", 1)[1]
                    for net_item in loaded_brief.nets
                    for pin in net_item.pins
                    if pin.split(".", 1)[0]
                    in {part.reference for part in loaded_brief.parts if part.lib_id == lib_id}
                }
                if not required.issubset(pins):
                    raise StepFailure(
                        "get_symbol_info",
                        f"{lib_id} missing pins {sorted(required - pins)}; "
                        f"actual pins {sorted(pins)}",
                    )

            components: list[dict[str, object]] = []
            for index, part in enumerate(loaded_brief.parts):
                placement = loaded_brief.board.placements.get(part.reference)
                x = 50.8 + 38.1 * index
                y = 50.8 + 38.1 * (index // 4)
                component: dict[str, object] = {
                    "lib_id": part.lib_id,
                    "reference": part.reference,
                    "footprint": part.footprint,
                    "x": x,
                    "y": y,
                    "rotation": 0.0,
                }
                if part.value is not None:
                    component["value"] = part.value
                components.append(component)
                if placement is not None:
                    components[-1].update(
                        {
                            "x": placement.x_mm,
                            "y": placement.y_mm,
                            "rotation": placement.rotation_deg,
                        }
                    )
            _, next_id = _call(
                process,
                next_id,
                "batch_place_components",
                {"schematic": str(schematic), "components": components},
                log,
            )
            for net_item in loaded_brief.nets:
                pins = [
                    {"reference": token.split(".", 1)[0], "pin_number": token.split(".", 1)[1]}
                    for token in net_item.pins
                ]
                _, next_id = _call(
                    process,
                    next_id,
                    "batch_connect_to_net",
                    {"schematic": str(schematic), "net_name": net_item.name, "pins": pins},
                    log,
                )
            _, next_id = _call(
                process, next_id, "check_schematic_overlaps", {"schematic": str(schematic)}, log
            )
            shorts, next_id = _call(
                process, next_id, "find_shorted_nets", {"schematic": str(schematic)}, log
            )
            if _has_short(shorts):
                raise StepFailure("find_shorted_nets", f"shorted nets detected: {shorts}")

            title_fields = titleblock.inject_title_block(
                schematic,
                title=loaded_brief.name,
                date=time.strftime("%Y-%m-%d", time.gmtime()),
                rev="1",
                comments=[loaded_brief.description] if loaded_brief.description else None,
                paper=titleblock.paper_for_part_count(len(loaded_brief.parts)),
            )
            _record(
                log,
                {
                    "step": "circuit_title_block",
                    "tool": "circuit.titleblock.inject_title_block",
                    "payload": {"schematic": str(schematic)},
                    "result": title_fields,
                },
            )

            schematic_advisories = [
                ("audit_connections", {"schematic": str(schematic)}),
                ("validate_wire_connections", {"schematic": str(schematic)}),
                ("validate_component_connections", {"schematic": str(schematic)}),
                ("list_schematic_nets", {"schematic": str(schematic)}),
                ("find_single_pin_nets", {"schematic": str(schematic)}),
                ("find_orphan_items", {"schematic": str(schematic)}),
                ("get_schematic_layout", {"schematic": str(schematic)}),
                ("export_netlist_summary", {"schematic": str(schematic)}),
                (
                    "annotate_schematic",
                    {"schematic": str(schematic), "dry_run": True},
                ),
                ("update_symbols_from_library", {"schematic": str(schematic), "dry_run": True}),
                ("reset_schematic_field_positions", {"schematic": str(schematic), "dry_run": True}),
                (
                    "export_bom",
                    {
                        "schematic": str(schematic),
                        "output": str(konnect_exports / "export_bom" / "bom.csv"),
                    },
                ),
                ("check_bom_health", {"schematic": str(schematic)}),
                (
                    "run_erc",
                    {
                        "schematic": str(schematic),
                        "output": str(konnect_exports / "run_erc" / "erc.json"),
                    },
                ),
                (
                    "render_schematic_png",
                    {
                        "schematic": str(schematic),
                        "output": str(konnect_exports / "render_schematic_png" / "schematic.png"),
                    },
                ),
                (
                    "export_schematic_svg",
                    {
                        "schematic": str(schematic),
                        "output": str(konnect_exports / "export_schematic_svg" / "schematic.svg"),
                    },
                ),
                (
                    "export_schematic_pdf",
                    {
                        "schematic": str(schematic),
                        "output": str(konnect_exports / "export_schematic_pdf" / "schematic.pdf"),
                    },
                ),
                (
                    "snapshot_project",
                    {
                        "schematic": str(schematic),
                        "output_dir": str(konnect_exports / "snapshot_project"),
                        "label": "schematic-gate",
                    },
                ),
            ]
            for _, advisory_args in schematic_advisories:
                for key in ("output", "output_dir"):
                    output = advisory_args.get(key)
                    if isinstance(output, str):
                        configured_output = Path(output)
                        (configured_output.parent if key == "output" else configured_output).mkdir(
                            parents=True, exist_ok=True
                        )
            for advisory_name, advisory_args in schematic_advisories:
                _advise(
                    process,
                    next_id,
                    advisory_name,
                    advisory_args,
                    "schematic",
                    log,
                    advisory_results,
                    lambda _result, root=konnect_exports / advisory_name: _artifacts_under(
                        root, args.workdir
                    ),
                )
                next_id += 1

            sch_lint_result = sch_lint.lint_file(
                schematic, reports_dir / f"{loaded_brief.name}.sch_lint.json"
            )
            _record(
                log,
                {
                    "tool": "circuit.sch_lint.lint_file",
                    "payload": {"schematic": str(schematic)},
                    "result": sch_lint_result.model_dump(mode="json"),
                },
            )
            if sch_lint_result.verdict != "pass":
                raise StepFailure("circuit.sch_lint", sch_lint_result.model_dump_json())

            netlist_path = kicad_cli.export_netlist(
                schematic, reports_dir / f"{loaded_brief.name}.net"
            )
            _record(
                log,
                {
                    "tool": "circuit.kicad_cli.export_netlist",
                    "payload": {"schematic": str(schematic), "output": str(netlist_path)},
                    "result": {"path": str(netlist_path)},
                },
            )
            connectivity = netlist.check_connectivity(
                loaded_brief,
                netlist.parse_netlist(netlist_path),
                brief_path=args.brief,
                netlist_path=netlist_path,
            )
            (reports_dir / f"{loaded_brief.name}.connectivity.json").write_text(
                connectivity.model_dump_json(indent=2), encoding="utf-8"
            )
            _record(
                log,
                {
                    "tool": "circuit.connectivity_check",
                    "payload": {"brief": str(args.brief), "netlist": str(netlist_path)},
                    "result": connectivity.model_dump(mode="json"),
                },
            )
            if connectivity.verdict != "pass":
                raise StepFailure("circuit.connectivity_check", connectivity.model_dump_json())

            erc_result = kicad_cli.erc(schematic, reports_dir / f"{loaded_brief.name}.erc.json")
            _advisory_detail(
                advisory_results,
                "run_erc",
                {
                    "kicad_cli_error_count": erc_result.errors,
                    "kicad_cli_warning_count": erc_result.warnings,
                },
            )
            _record(
                log,
                {
                    "tool": "circuit.kicad_cli.erc",
                    "payload": {"schematic": str(schematic)},
                    "result": erc_result.model_dump(mode="json"),
                },
            )
            if erc_result.verdict != "pass":
                raise StepFailure("circuit.kicad_cli.erc", erc_result.model_dump_json())
            schematic_snapshot = reports_dir / "snapshots" / "schematic-gate.kicad_sch"
            schematic_snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(schematic, schematic_snapshot)
            _record(
                log,
                {
                    "tool": "circuit.snapshot.schematic_gate",
                    "payload": {"source": str(schematic), "output": str(schematic_snapshot)},
                    "result": {"path": str(schematic_snapshot)},
                },
            )

            with (args.workdir / "api-server.log").open("w", encoding="utf-8") as api_log:
                server = subprocess.Popen(
                    ["kicad-cli", "api-server", "--socket", str(socket_path), str(board)],
                    stdout=api_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=environment,
                )
            _wait_socket(socket_path, server)
            _stop_konnect(process)
            process, next_id = _start_konnect(
                {**environment, "KICAD_API_SOCKET": f"ipc://{socket_path}"},
                log,
                next_id,
            )
            ui_health, next_id = _call(
                process,
                next_id,
                "check_kicad_ui",
                {"timeout_seconds": 5},
                log,
            )
            ui_health_dict = (
                cast(dict[str, object], ui_health) if isinstance(ui_health, dict) else {}
            )
            if ui_health_dict.get("ipc_responsive") is not True:
                raise StepFailure("check_kicad_ui", f"IPC health check failed: {ui_health}")
            update, next_id = _call(
                process,
                next_id,
                "update_pcb_from_schematic",
                {"schematic": str(schematic), "board": str(board), "dry_run": True},
                log,
            )
            revision = _revision(update)
            if revision is None:
                raise StepFailure("update_pcb_from_schematic", "dry-run returned no plan revision")
            _, next_id = _call(
                process,
                next_id,
                "update_pcb_from_schematic",
                {
                    "schematic": str(schematic),
                    "board": str(board),
                    "dry_run": False,
                    "expected_plan_revision": revision,
                },
                log,
            )
            _, next_id = _call(process, next_id, "save_project", {}, log)
            _, next_id = _call(
                process,
                next_id,
                "add_board_outline",
                {
                    "board": str(board),
                    "x1": 0.0,
                    "y1": 0.0,
                    "x2": loaded_brief.board.width_mm,
                    "y2": loaded_brief.board.height_mm,
                },
                log,
            )
            placement_values = [
                {
                    "reference": reference,
                    "x": value.x_mm,
                    "y": value.y_mm,
                    "rotation": value.rotation_deg,
                }
                for reference, value in loaded_brief.board.placements.items()
            ]
            if placement_values:
                _, next_id = _call(
                    process,
                    next_id,
                    "set_component_placements",
                    {"board": str(board), "placements": placement_values},
                    log,
                )
            else:
                _, next_id = _call(
                    process,
                    next_id,
                    "auto_place_from_schematic",
                    {"board": str(board)},
                    log,
                )
            _, next_id = _call(process, next_id, "save_project", {}, log)
            placement_advisories = [
                ("get_board_info", {"board": str(board)}),
                ("get_board_extents", {"board": str(board)}),
                ("get_layer_list", {"board": str(board)}),
                ("score_placement", {"board": str(board)}),
                (
                    "refine_placement_force_directed",
                    {"board": str(board), "dry_run": True},
                ),
                ("get_design_rules", {"board": str(board)}),
                ("list_design_rules", {"project_dir": str(args.workdir)}),
                ("get_netclasses", {"board": str(board)}),
            ]
            for advisory_name, advisory_args in placement_advisories:
                _advise(
                    process,
                    next_id,
                    advisory_name,
                    advisory_args,
                    "layout",
                    log,
                    advisory_results,
                )
                next_id += 1
            pad_positions: dict[tuple[str, str], Point] = {}
            pad_clearances: dict[tuple[str, str], float] = {}
            for part in loaded_brief.parts:
                pads, next_id = _call(
                    process,
                    next_id,
                    "get_component_pads",
                    {"board": str(board), "reference": part.reference},
                    log,
                )
                if not isinstance(pads, dict):
                    raise StepFailure(
                        "get_component_pads",
                        f"missing pad list for {part.reference}: {pads}",
                    )
                pads_dict = cast(dict[str, Any], pads)
                raw_pads = pads_dict.get("pads")
                if not isinstance(raw_pads, list):
                    raise StepFailure(
                        "get_component_pads",
                        f"missing pad list for {part.reference}: {pads}",
                    )
                for raw_pad in cast(list[Any], raw_pads):
                    if not isinstance(raw_pad, dict):
                        raise StepFailure("get_component_pads", f"malformed pad: {raw_pad}")
                    pad = cast(dict[str, Any], raw_pad)
                    number = pad.get("number")
                    x = pad.get("x")
                    y = pad.get("y")
                    size = pad.get("size")
                    if (
                        not isinstance(number, str)
                        or not isinstance(x, (int, float))
                        or not isinstance(y, (int, float))
                    ):
                        raise StepFailure("get_component_pads", f"malformed pad: {pad}")
                    key = (part.reference, number)
                    pad_positions[key] = (float(x), float(y))
                    if isinstance(size, dict):
                        size_dict = cast(dict[str, Any], size)
                        size_x = size_dict.get("x")
                        size_y = size_dict.get("y")
                        if isinstance(size_x, (int, float)) and isinstance(size_y, (int, float)):
                            pad_clearances[key] = max(float(size_x), float(size_y)) / 2 + 0.325
                            continue
                    pad_clearances[key] = 0.325

            occupied: list[tuple[str, Segment]] = []
            for net_item in loaded_brief.nets:
                points: list[Point] = []
                for token in net_item.pins:
                    reference, pin = token.split(".", 1)
                    try:
                        points.append(pad_positions[(reference, pin)])
                    except KeyError as exc:
                        raise StepFailure(
                            "get_component_pads", f"missing pad position for {token}"
                        ) from exc
                for start, end in pairwise(points):
                    route_points = _route_path(
                        start,
                        end,
                        net_item.name,
                        pad_positions,
                        pad_clearances,
                        occupied,
                        loaded_brief.board.width_mm,
                        loaded_brief.board.height_mm,
                    )
                    for (x1, y1), (x2, y2) in pairwise(route_points):
                        _, next_id = _call(
                            process,
                            next_id,
                            "route_trace",
                            {
                                "board": str(board),
                                "net_name": f"/{net_item.name}",
                                "layer": "F.Cu",
                                "x1": x1,
                                "y1": y1,
                                "x2": x2,
                                "y2": y2,
                                "width": 0.25,
                            },
                            log,
                        )
                        occupied.append((net_item.name, ((x1, y1), (x2, y2))))
            _, next_id = _call(process, next_id, "save_project", {}, log)
            first_net_name = loaded_brief.nets[0].name if loaded_brief.nets else "GND"
            first_reference = loaded_brief.parts[0].reference if loaded_brief.parts else "R1"
            review_advisories = [
                (
                    "fix_connectivity",
                    {"schematic": str(schematic), "dry_run": True},
                ),
                (
                    "query_traces",
                    {
                        "board": str(board),
                        "layer": "F.Cu",
                        "net_name": f"/{first_net_name}",
                    },
                ),
                (
                    "get_connected_items",
                    {"schematic": str(schematic), "reference": first_reference},
                ),
                (
                    "run_drc",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "run_drc" / "drc.json"),
                    },
                ),
                (
                    "get_drc_violations",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "get_drc_violations" / "violations.json"),
                    },
                ),
                (
                    "run_design_review",
                    {"schematic": str(schematic), "board": str(board)},
                ),
                ("audit_power_rails", {"schematic": str(schematic)}),
                ("audit_decoupling", {"schematic": str(schematic)}),
                ("audit_manufacturing", {"board": str(board)}),
                ("validate_for_manufacturing", {"board": str(board)}),
                ("estimate_cost", {"board": str(board)}),
                ("get_board_2d_view", {"board": str(board)}),
                ("set_visual_baseline", {"schematic": str(schematic)}),
                ("compare_visual_baseline", {"schematic": str(schematic)}),
                (
                    "snapshot_project",
                    {
                        "schematic": str(schematic),
                        "pcb": str(board),
                        "output_dir": str(konnect_exports / "snapshot_project_drc"),
                        "label": "drc-gate",
                    },
                ),
            ]
            for advisory_name, advisory_args in review_advisories:
                _advise(
                    process,
                    next_id,
                    advisory_name,
                    advisory_args,
                    "review",
                    log,
                    advisory_results,
                    lambda _result, name=advisory_name: _artifacts_under(
                        konnect_exports / name, args.workdir
                    ),
                )
                next_id += 1
            _stop_konnect(process)
            process = None
            apiserver.stop()
            socket_path.unlink(missing_ok=True)

            drc_result = kicad_cli.drc(board, reports_dir / f"{loaded_brief.name}.drc.json")
            _advisory_detail(
                advisory_results,
                "run_drc",
                {
                    "kicad_cli_error_count": drc_result.errors,
                    "kicad_cli_warning_count": drc_result.warnings,
                },
            )
            _advisory_detail(
                advisory_results,
                "get_drc_violations",
                {
                    "kicad_cli_error_count": drc_result.errors,
                    "kicad_cli_warning_count": drc_result.warnings,
                },
            )
            _record(
                log,
                {
                    "tool": "circuit.kicad_cli.drc",
                    "payload": {"board": str(board)},
                    "result": drc_result.model_dump(mode="json"),
                },
            )
            if drc_result.verdict != "pass":
                raise StepFailure("circuit.kicad_cli.drc", drc_result.model_dump_json())
            board_snapshot = reports_dir / "snapshots" / "drc-gate.kicad_pcb"
            board_snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(board, board_snapshot)
            _record(
                log,
                {
                    "tool": "circuit.snapshot.drc_gate",
                    "payload": {"source": str(board), "output": str(board_snapshot)},
                    "result": {"path": str(board_snapshot)},
                },
            )
            render_dir = reports_dir / "render"
            for side in ("top", "bottom"):
                render_path = kicad_cli.render(
                    board,
                    render_dir / f"{loaded_brief.name}-{side}.png",
                    side=side,
                )
                renders.append(str(render_path))
                _record(
                    log,
                    {
                        "tool": f"circuit.kicad_cli.render:{side}",
                        "payload": {"board": str(board), "output": str(render_path), "side": side},
                        "result": {"path": str(render_path)},
                    },
                )
            # Rendering can materialize project library tables in the board file.
            shutil.copy2(board, board_snapshot)
            for table_name in ("fp-lib-table", "sym-lib-table"):
                table = args.workdir / table_name
                if table.is_file():
                    shutil.copy2(table, board_snapshot.parent / table_name)
            jobset_result = kicad_cli.jobset_run(project, args.workdir / "jobset-out")
            jobset_consistent = (
                jobset_result.erc_report is not None
                and jobset_result.drc_report is not None
                and kicad_cli.reports_equivalent(
                    reports_dir / f"{loaded_brief.name}.erc.json",
                    jobset_result.erc_report,
                )
                and kicad_cli.reports_equivalent(
                    reports_dir / f"{loaded_brief.name}.drc.json",
                    jobset_result.drc_report,
                )
            )
            _record(
                log,
                {
                    "tool": "circuit.kicad_cli.jobset_run",
                    "payload": {
                        "project": str(project),
                        "output_dir": str(jobset_result.output_dir),
                    },
                    "result": jobset_result.model_dump(mode="json"),
                },
            )
            if not jobset_consistent:
                raise StepFailure("circuit.kicad_cli.jobset_consistency", "jobset reports disagree")
            diffs["schematic"] = kicad_cli.diff(
                "sch",
                schematic_snapshot,
                schematic,
                reports_dir / "diffs" / "schematic.json",
            )
            diffs["pcb"] = kicad_cli.diff(
                "pcb",
                board_snapshot,
                board,
                reports_dir / "diffs" / "pcb.json",
            )
            version_about = kicad_cli.version_about()
            _record(
                log,
                {
                    "tool": "circuit.kicad_cli.version_about",
                    "result": {"about": version_about},
                },
            )
            for kind, source, directory in (
                ("gerbers", board, args.workdir / "exports" / "gerbers"),
                ("drill", board, args.workdir / "exports" / "drill"),
                ("bom", schematic, args.workdir / "exports" / "bom"),
                ("pos", board, args.workdir / "exports" / "pos"),
                ("sch_pdf", schematic, args.workdir / "exports" / "sch_pdf"),
                ("sch_svg", schematic, args.workdir / "exports" / "sch_svg"),
                ("pcb_pdf", board, args.workdir / "exports" / "pcb_pdf"),
                ("pcb_svg", board, args.workdir / "exports" / "pcb_svg"),
                ("dxf", board, args.workdir / "exports" / "dxf"),
                ("ipc2581", board, args.workdir / "exports" / "ipc2581"),
                ("odb", board, args.workdir / "exports" / "odb"),
                ("gencad", board, args.workdir / "exports" / "gencad"),
                ("vrml", board, args.workdir / "exports" / "vrml"),
                ("glb", board, args.workdir / "exports" / "glb"),
            ):
                paths = kicad_cli.export(cast(kicad_cli.ExportKind, kind), source, directory)
                exports[kind] = [str(path) for path in paths]
                _record(
                    log,
                    {
                        "tool": f"circuit.kicad_cli.export:{kind}",
                        "payload": {"source": str(source), "output_dir": str(directory)},
                        "result": exports[kind],
                    },
                )
            process, next_id = _start_konnect(environment, log, next_id)
            manufacturing_advisories = [
                (
                    "export_manufacturing_package",
                    {
                        "board": str(board),
                        "schematic": str(schematic),
                        "output_dir": str(konnect_exports / "export_manufacturing_package"),
                    },
                ),
                (
                    "export_gerber",
                    {
                        "board": str(board),
                        "output_dir": str(konnect_exports / "export_gerber"),
                    },
                ),
                (
                    "export_position_file",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "export_position_file" / "positions.csv"),
                    },
                ),
                (
                    "export_ipc2581",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "export_ipc2581" / "board.xml"),
                    },
                ),
                (
                    "export_odb",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "export_odb" / "board.zip"),
                    },
                ),
                (
                    "export_gencad",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "export_gencad" / "board.cad"),
                    },
                ),
                (
                    "export_dxf",
                    {
                        "board": str(board),
                        "output_dir": str(konnect_exports / "export_dxf"),
                        "layers": ["Edge.Cuts"],
                    },
                ),
                (
                    "export_svg",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "export_svg" / "board.svg"),
                        "layers": ["Edge.Cuts", "F.Cu", "B.Cu"],
                    },
                ),
                (
                    "export_pdf",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "export_pdf" / "board.pdf"),
                        "layers": ["Edge.Cuts", "F.Cu", "B.Cu"],
                    },
                ),
                (
                    "export_3d",
                    {
                        "board": str(board),
                        "output": str(konnect_exports / "export_3d" / "board.step"),
                        "format": "step",
                    },
                ),
            ]
            for advisory_name, advisory_args in manufacturing_advisories:
                _advise(
                    process,
                    next_id,
                    advisory_name,
                    advisory_args,
                    "manufacturing",
                    log,
                    advisory_results,
                    lambda _result, name=advisory_name: _artifacts_under(
                        konnect_exports / name, args.workdir
                    ),
                )
                next_id += 1
            _stop_konnect(process)
            process = None

        design_report = report.build_design_report(
            loaded_brief,
            brief_path=args.brief,
            kicad_version=erc_result.kicad_version,
            connectivity=connectivity,
            sch_lint=sch_lint_result,
            erc=erc_result,
            drc=drc_result,
            exports=exports,
            advisory=advisory_results,
            renders=renders,
            jobset=jobset_result,
            jobset_consistent=jobset_consistent,
            diffs=diffs,
        )
        report_path = reports_dir / "design-report.json"
        report.write_report(design_report, report_path)
        provenance_path = _write_provenance(
            args.workdir,
            args.brief,
            args.intake,
            version_about=version_about,
        )
        result.update(
            {
                "verdict": design_report.verdict,
                "design_report": str(report_path),
                "provenance": str(provenance_path),
                "intake": (
                    intake_result.model_dump(mode="json") if intake_result is not None else None
                ),
                "libraries": library_result.model_dump(mode="json"),
                "connectivity": connectivity.model_dump(mode="json"),
                "sch_lint": sch_lint_result.model_dump(mode="json"),
                "erc": erc_result.model_dump(mode="json"),
                "drc": drc_result.model_dump(mode="json"),
                "renders": renders,
                "jobset": jobset_result.model_dump(mode="json") if jobset_result else None,
                "jobset_consistent": jobset_consistent,
                "diffs": {name: item.model_dump(mode="json") for name, item in diffs.items()},
                "version_about": version_about,
                "advisory_counts": {
                    status: sum(item.status == status for item in advisory_results)
                    for status in ("ok", "error", "not_applicable")
                },
                "advisory": [item.model_dump(mode="json") for item in advisory_results],
            }
        )
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "result": str(output_path),
                    "design_report": str(report_path),
                    "provenance": str(provenance_path),
                    "sch_lint_verdict": sch_lint_result.verdict,
                    "sch_lint_warnings": sch_lint_result.warnings,
                    "drc_verdict": drc_result.verdict,
                },
                ensure_ascii=False,
            )
        )
        return 0 if design_report.verdict == "pass" else 1
    except StepFailure as exc:
        return _write_failure(output_path, result, exc.step, exc)
    except Exception as exc:
        return _write_failure(output_path, result, "unexpected", exc)
    finally:
        _stop_konnect(process)
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        apiserver.stop()
        socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
