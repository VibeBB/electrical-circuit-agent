#!/usr/bin/env python3
"""Run the deterministic design-brief authoring flow."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from circuit import __version__ as _circuit_version
from circuit import (
    apiserver,
    brief,
    drawing_sheet,
    fit_sheet,
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

# Advisory render/export ops drive a cold KiCad session and have timed out
# at the 30s default in the field; give them a wider budget plus one retry
# before recording an error.
ADVISORY_TIMEOUT = 120.0
ADVISORY_RETRIES = 2

# Autorouter DFM geometry: keep-out margin added around every pad (half the
# pad's larger dimension plus this margin, or the bare margin when the pad
# size is unknown) and the F.Cu trace width.
PAD_CLEARANCE_MARGIN_MM = 0.325
TRACE_WIDTH_MM = 0.25

TOOLSETS = [
    "project",
    "library",
    "editor_navigation",
    "sch_components",
    "sch_wiring",
    "sch_bus",
    "sch_analysis",
    "sch_batch",
    "sch_export",
    "sch_hierarchy",
    "pcb_board",
    "pcb_components",
    "pcb_routing",
    "placement",
    "pcb_export",
    "verification",
    "integration",
    "config",
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
    # One schema per line: every record names its pipeline step.
    if entry.get("step") is None:
        step = entry.get("tool") or entry.get("method")
        entry["step"] = step if isinstance(step, str) else "unknown"
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
    result_value: object | None = None
    last_error: BaseException = RuntimeError(f"{name}: advisory call did not run")
    for _attempt in range(ADVISORY_RETRIES):
        try:
            raw_result, next_id = call_tool(
                process, next_id, name, dict(arguments), ADVISORY_TIMEOUT
            )
            result_value = cast(object, raw_result)
            break
        except Exception as exc:
            last_error = exc
            if "timeout" not in str(exc).lower() or _attempt == ADVISORY_RETRIES - 1:
                result_value = None
                break
            _record(
                log,
                {
                    "step": name,
                    "tool": name,
                    "advisory": True,
                    "payload": dict(arguments),
                    "result": {"retry": True, "after": str(exc)},
                },
            )
    try:
        if result_value is None:
            raise last_error
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


def _prune_empty_dirs(root: Path) -> None:
    """Drop empty export dirs left behind when Konnect ops timed out."""
    if not root.is_dir():
        return
    for child in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir():
            with contextlib.suppress(OSError):
                child.rmdir()
    with contextlib.suppress(OSError):
        root.rmdir()


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


class KonnectSession:
    """A live Konnect MCP process plus its JSON-RPC message-id counter."""

    def __init__(self, process: subprocess.Popen[str], next_id: int, log: TextIO) -> None:
        self.process = process
        self.next_id = next_id
        self.log = log

    def call(self, name: str, arguments: Mapping[str, object], timeout: float = 30.0) -> object:
        result, self.next_id = _call(self.process, self.next_id, name, arguments, self.log, timeout)
        return result

    def advise(
        self,
        name: str,
        arguments: Mapping[str, object],
        stage: AdvisoryStage,
        results: list[AdvisoryResult],
        artifacts_fn: Callable[[object], list[str]] | None = None,
    ) -> object | None:
        # Advisory calls consume exactly one message id whether or not they
        # succeed (a timed-out retry reuses the same id).
        result, _ = _advise(
            self.process, self.next_id, name, arguments, stage, self.log, results, artifacts_fn
        )
        self.next_id += 1
        return result

    def close(self) -> None:
        _stop_konnect(self.process)


def _start_konnect(
    env: dict[str, str],
    log: TextIO,
    next_id: int,
) -> KonnectSession:
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
        return KonnectSession(process, next_id, log)
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


@dataclass
class AuthoringRun:
    """Paths, processes, and accumulated observations shared by the pipeline stages."""

    brief_path: Path
    intake_path: Path | None
    workdir: Path
    kicad_share: Path
    loaded_brief: brief.DesignBrief
    log: TextIO
    reports_dir: Path
    konnect_exports: Path
    project: Path
    schematic: Path
    board: Path
    socket_path: Path
    environment: dict[str, str]
    advisory_results: list[AdvisoryResult] = field(default_factory=lambda: [])
    exports: dict[str, list[str]] = field(default_factory=lambda: {})
    konnect: KonnectSession | None = None
    server: subprocess.Popen[str] | None = None
    # JSON-RPC message ids keep counting across the successive Konnect sessions.
    next_id: int = 1

    @property
    def session(self) -> KonnectSession:
        if self.konnect is None:
            raise StepFailure("konnect", "no active Konnect session")
        return self.konnect

    def start_konnect(self, env: dict[str, str]) -> None:
        self.stop_konnect()
        self.konnect = _start_konnect(env, self.log, self.next_id)

    def stop_konnect(self) -> None:
        if self.konnect is not None:
            self.next_id = self.konnect.next_id
            self.konnect.close()
            self.konnect = None

    def stop_server(self) -> None:
        if self.server is not None and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()

    def artifacts_fn(self, tool: str) -> Callable[[object], list[str]]:
        return lambda _result: _artifacts_under(self.konnect_exports / tool, self.workdir)

    def advise_all(
        self,
        stage: AdvisoryStage,
        advisories: list[tuple[str, dict[str, object]]],
        *,
        with_artifacts: bool,
    ) -> None:
        for name, arguments in advisories:
            self.session.advise(
                name,
                arguments,
                stage,
                self.advisory_results,
                self.artifacts_fn(name) if with_artifacts else None,
            )


@dataclass(frozen=True)
class SchematicGate:
    sch_lint: sch_lint.SchLintReport
    connectivity: netlist.ConnectivityReport
    erc: kicad_cli.Report
    snapshot: Path


@dataclass(frozen=True)
class BoardGate:
    drc: kicad_cli.Report
    snapshot: Path
    renders: list[str]
    jobset: kicad_cli.JobsetResult
    jobset_consistent: bool
    diffs: dict[str, kicad_cli.DiffReport]
    version_about: str


@dataclass(frozen=True)
class PipelineOutput:
    intake: intake.IntakeReport | None
    libraries: libraries.LibraryReport
    schematic: SchematicGate
    board: BoardGate


def _stage_intake(run: AuthoringRun, result: dict[str, object]) -> intake.IntakeReport | None:
    if run.intake_path is None:
        return None
    intake_result = intake.check_intake(
        run.loaded_brief,
        intake.load_intake(run.intake_path),
        brief_path=run.brief_path,
        intake_path=run.intake_path,
    )
    intake_report = run.reports_dir / f"{run.loaded_brief.name}.intake.json"
    intake_report.write_text(intake_result.model_dump_json(indent=2), encoding="utf-8")
    _record(
        run.log,
        {
            "step": "circuit_brief_intake_check",
            "tool": "circuit_brief_intake_check",
            "payload": {
                "brief_path": str(run.brief_path),
                "intake_path": str(run.intake_path),
                "output_path": str(intake_report),
            },
            "result": summarize(intake_result.model_dump(mode="json")),
        },
    )
    if intake_result.verdict != "ready":
        raise StepFailure("circuit_brief_intake_check", intake_result.model_dump_json())
    result["intake"] = intake_result.model_dump(mode="json")
    return intake_result


def _stage_library_gate(run: AuthoringRun, result: dict[str, object]) -> libraries.LibraryReport:
    library_result = libraries.check_libraries(
        run.loaded_brief,
        brief_path=run.brief_path,
        roots=libraries.LibraryRoots(
            symbol_dirs=[run.kicad_share / "symbols"],
            footprint_dirs=[run.kicad_share / "footprints"],
        ),
    )
    library_path = run.reports_dir / f"{run.loaded_brief.name}.libraries.json"
    library_path.write_text(library_result.model_dump_json(indent=2), encoding="utf-8")
    _record(
        run.log,
        {
            "step": "circuit_brief_library_check",
            "tool": "circuit_brief_library_check",
            "payload": {
                "brief_path": str(run.brief_path),
                "output_path": str(library_path),
            },
            "result": summarize(library_result.model_dump(mode="json")),
        },
    )
    if library_result.verdict != "pass":
        raise StepFailure("circuit_brief_library_check", library_result.model_dump_json())
    result["libraries"] = library_result.model_dump(mode="json")
    return library_result


def _stage_create_project(run: AuthoringRun) -> None:
    """Start Konnect, create the project, and register/verify the part libraries."""
    (run.workdir / "home").mkdir(exist_ok=True)
    run.start_konnect(run.environment)
    session = run.session
    loaded_brief = run.loaded_brief
    run.advise_all(
        "intake",
        [
            ("list_template_categories", {}),
            ("search_templates", {"query": loaded_brief.name}),
        ],
        with_artifacts=False,
    )
    session.call("create_project", {"path": str(run.workdir), "name": loaded_brief.name})

    symbol_nicknames = sorted({part.lib_id.split(":", 1)[0] for part in loaded_brief.parts})
    footprint_nicknames = sorted({part.footprint.split(":", 1)[0] for part in loaded_brief.parts})
    for nickname in symbol_nicknames:
        path = run.kicad_share / "symbols" / f"{nickname}.kicad_sym"
        if not path.is_file():
            raise StepFailure("register_symbol_library", f"missing symbol library: {path}")
        session.call(
            "register_symbol_library",
            {
                "library_path": str(path),
                "nickname": nickname,
                "project": str(run.project),
                "scope": "project",
            },
        )
    for nickname in footprint_nicknames:
        path = run.kicad_share / "footprints" / f"{nickname}.pretty"
        if not path.is_dir():
            raise StepFailure("register_footprint_library", f"missing footprint library: {path}")
        session.call(
            "register_footprint_library",
            {
                "library_path": str(path),
                "nickname": nickname,
                "project": str(run.project),
                "scope": "project",
            },
        )

    for lib_id in sorted({part.lib_id for part in loaded_brief.parts}):
        info = session.call("get_symbol_info", {"lib_id": lib_id, "project_dir": str(run.workdir)})
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
                f"{lib_id} missing pins {sorted(required - pins)}; actual pins {sorted(pins)}",
            )


# Readable schematic placement: connectors enter top-left and the signal
# chain snakes left-to-right down the sheet, spread over the usable area so
# sch_lint's sheet-usage check passes. Board placements are PCB coordinates
# and are never reused here.
_SCHEMATIC_GRID_MAX_COLUMNS = 4
_SCHEMATIC_MARGIN_X_MM = 20.0
_SCHEMATIC_MARGIN_TOP_MM = 25.4
# The ISO 7200 title block reaches ~45 mm up from the bottom edge; keep the
# bottom row's symbols and their downward field text (≤ ~8 mm) clear of it.
_SCHEMATIC_MARGIN_BOTTOM_MM = 60.0
# Distance Reference/Value text is offset above the topmost / below the
# bottom-most pin of a symbol so it clears the body (sch_lint flags any
# property within 2.5 mm of the symbol anchor).
_FIELD_CLEARANCE_MM = 3.81


def _schematic_part_order(loaded_brief: brief.DesignBrief) -> list[str]:
    """Order parts along the signal chain: connectors first, then net adjacency."""
    seen: set[str] = set()
    order: list[str] = []
    for part in loaded_brief.parts:
        if part.connector and part.reference not in seen:
            seen.add(part.reference)
            order.append(part.reference)
    progressed = True
    while progressed:
        progressed = False
        for net_item in loaded_brief.nets:
            references = [token.split(".", 1)[0] for token in net_item.pins]
            if not any(reference in seen for reference in references):
                continue
            for reference in references:
                if reference not in seen:
                    seen.add(reference)
                    order.append(reference)
                    progressed = True
    for part in loaded_brief.parts:
        if part.reference not in seen:
            order.append(part.reference)
    return order


def _schematic_layout(loaded_brief: brief.DesignBrief, paper: str) -> dict[str, Point]:
    """Return signal-chain positions waved across the usable sheet area.

    Each successive part steps one column right and one row down, wrapping on
    a cell-staggered diagonal, so any count ≥ 2 spans both axes (a single row
    or column can never satisfy sch_lint's sheet-usage floor)."""
    order = _schematic_part_order(loaded_brief)
    count = len(order)
    width, height = titleblock.PAPER_SIZES[paper]
    rows = max(2, math.ceil(count / _SCHEMATIC_GRID_MAX_COLUMNS))
    columns = min(count, max(2, math.ceil(count / rows)))
    usable_x = width - 2 * _SCHEMATIC_MARGIN_X_MM
    usable_y = height - _SCHEMATIC_MARGIN_TOP_MM - _SCHEMATIC_MARGIN_BOTTOM_MM
    xs = (
        [_SCHEMATIC_MARGIN_X_MM + usable_x / 2]
        if columns == 1
        else [_SCHEMATIC_MARGIN_X_MM + usable_x * index / (columns - 1) for index in range(columns)]
    )
    ys = [_SCHEMATIC_MARGIN_TOP_MM + usable_y * index / (rows - 1) for index in range(rows)]
    positions: dict[str, Point] = {}
    for index, reference in enumerate(order):
        column = index % columns
        row = (index // columns + column) % rows
        positions[reference] = (xs[column], ys[row])
    return positions


def _place_symbol_fields(run: AuthoringRun) -> None:
    """Move Reference/Value text off symbol bodies using pin geometry."""
    references = [part.reference for part in run.loaded_brief.parts]
    locations = run.session.call(
        "batch_get_schematic_pin_locations",
        {"schematic": str(run.schematic), "references": references},
    )
    components: object = None
    if isinstance(locations, list):
        components = cast(list[Any], locations)
    elif isinstance(locations, dict):
        components = cast(dict[str, Any], locations).get("components")
    if not isinstance(components, list):
        raise StepFailure("batch_get_schematic_pin_locations", f"unexpected result: {locations}")
    for component in cast(list[Any], components):
        if not isinstance(component, dict):
            continue
        item = cast(dict[str, Any], component)
        reference = item.get("reference")
        pins = item.get("pins")
        x = item.get("x")
        if not (
            isinstance(reference, str) and isinstance(pins, list) and isinstance(x, (int, float))
        ):
            continue
        ys: list[float] = []
        for pin in cast(list[Any], pins):
            if isinstance(pin, dict):
                pin_y = cast(dict[str, Any], pin).get("y")
                if isinstance(pin_y, (int, float)):
                    ys.append(float(pin_y))
        if not ys:
            continue
        arguments: dict[str, object] = {
            "schematic": str(run.schematic),
            "reference": reference,
            "field_placements": {
                "Reference": {"x": x, "y": min(ys) - _FIELD_CLEARANCE_MM},
                "Value": {"x": x, "y": max(ys) + _FIELD_CLEARANCE_MM},
            },
        }
        try:
            run.session.call("edit_schematic_component", arguments)
        except StepFailure:
            # Multi-unit placements need an explicit unit; keep going when the
            # op cannot move the fields — sch_lint judges the result.
            with contextlib.suppress(StepFailure):
                run.session.call("edit_schematic_component", {**arguments, "unit": 1})


def _wire_schematic_nets(run: AuthoringRun) -> None:
    """Draw real wires along each net's pin chain; labels still name the net."""
    schematic = str(run.schematic)
    pending: list[dict[str, str]] = []
    for net_item in run.loaded_brief.nets:
        endpoints = [token.split(".", 1) for token in net_item.pins]
        for (ref1, pin1), (ref2, pin2) in pairwise(endpoints):
            if ref1 == ref2:
                continue  # a same-symbol shunt stays label-only
            pending.append({"ref1": ref1, "pin1": pin1, "ref2": ref2, "pin2": pin2})
    if not pending:
        return
    try:
        run.session.call("batch_connect_pins", {"schematic": schematic, "connections": pending})
        return
    except StepFailure:
        pass
    for connection in pending:
        # Fall back to one call per connection; whatever still fails keeps
        # label-only connectivity for that path.
        with contextlib.suppress(StepFailure):
            run.session.call("connect_pins", {"schematic": schematic, **connection})


def _stage_author_schematic(run: AuthoringRun) -> None:
    """Place symbols, wire nets, and dress the sheet (title block, label clamp)."""
    session = run.session
    loaded_brief = run.loaded_brief
    schematic = run.schematic
    paper = titleblock.paper_for_part_count(len(loaded_brief.parts))
    positions = _schematic_layout(loaded_brief, paper)
    components: list[dict[str, object]] = []
    for part in loaded_brief.parts:
        x, y = positions[part.reference]
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
    session.call("batch_place_components", {"schematic": str(schematic), "components": components})
    _place_symbol_fields(run)
    _wire_schematic_nets(run)
    for net_item in loaded_brief.nets:
        pins = [
            {"reference": token.split(".", 1)[0], "pin_number": token.split(".", 1)[1]}
            for token in net_item.pins
        ]
        session.call(
            "batch_connect_to_net",
            {"schematic": str(schematic), "net_name": net_item.name, "pins": pins},
        )
    session.call("check_schematic_overlaps", {"schematic": str(schematic)})
    shorts = session.call("find_shorted_nets", {"schematic": str(schematic)})
    if _has_short(shorts):
        raise StepFailure("find_shorted_nets", f"shorted nets detected: {shorts}")

    drawing = loaded_brief.drawing
    title_fields = titleblock.inject_title_block(
        schematic,
        title=loaded_brief.name,
        date=drawing.date_of_issue or "",
        rev=drawing.revision,
        company=drawing.legal_owner,
        comments=[loaded_brief.description] if loaded_brief.description else None,
        paper=titleblock.paper_for_part_count(len(loaded_brief.parts)),
    )
    _record(
        run.log,
        {
            "step": "circuit_title_block",
            "tool": "circuit.titleblock.inject_title_block",
            "payload": {"schematic": str(schematic)},
            "result": title_fields,
        },
    )
    _apply_drawing_sheet(run)

    # Konnect ops can leave net labels outside the sheet bounds
    # (sch_lint reports them as item_out_of_bounds errors). Clamp
    # labels — never symbols or wires — back inside the frame so
    # the lint gate below sees a readable sheet.
    sheet_moves = fit_sheet.clamp_labels(schematic)
    _record(
        run.log,
        {
            "step": "circuit_fit_sheet",
            "tool": "circuit.fit_sheet.clamp_labels",
            "payload": {"schematic": str(schematic)},
            "result": {"clamped": len(sheet_moves), "items": sheet_moves},
        },
    )


def _stage_schematic_advisories(run: AuthoringRun) -> None:
    schematic = run.schematic
    konnect_exports = run.konnect_exports
    schematic_advisories: list[tuple[str, dict[str, object]]] = [
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
    run.advise_all("schematic", schematic_advisories, with_artifacts=True)


# Readability findings that fail the authoring verdict even though sch_lint
# reports them as warnings: an unreadable sheet fails, never relaxes.
_READABILITY_FAILURE_TYPES = frozenset(
    {
        "property_on_symbol",
        "sheet_underutilized",
        "item_out_of_bounds",
        "label_only_connectivity",
    }
)


def _readability_failures(
    report: sch_lint.SchLintReport,
) -> list[sch_lint.SchLintFinding]:
    return [finding for finding in report.findings if finding.type in _READABILITY_FAILURE_TYPES]


def _stage_schematic_gate(run: AuthoringRun) -> SchematicGate:
    """Blocking schematic gates: sch_lint, netlist connectivity, kicad-cli ERC."""
    loaded_brief = run.loaded_brief
    schematic = run.schematic
    reports_dir = run.reports_dir
    sch_lint_result = sch_lint.lint_file(
        schematic, reports_dir / f"{loaded_brief.name}.sch_lint.json"
    )
    _record(
        run.log,
        {
            "tool": "circuit.sch_lint.lint_file",
            "payload": {"schematic": str(schematic)},
            "result": sch_lint_result.model_dump(mode="json"),
        },
    )
    if sch_lint_result.verdict != "pass":
        raise StepFailure("circuit.sch_lint", sch_lint_result.model_dump_json())
    readability_failures = _readability_failures(sch_lint_result)
    if readability_failures:
        raise StepFailure(
            "circuit.sch_lint.readability",
            json.dumps([finding.model_dump(mode="json") for finding in readability_failures]),
        )

    netlist_path = kicad_cli.export_netlist(schematic, reports_dir / f"{loaded_brief.name}.net")
    _record(
        run.log,
        {
            "tool": "circuit.kicad_cli.export_netlist",
            "payload": {"schematic": str(schematic), "output": str(netlist_path)},
            "result": {"path": str(netlist_path)},
        },
    )
    connectivity = netlist.check_connectivity(
        loaded_brief,
        netlist.parse_netlist(netlist_path),
        brief_path=run.brief_path,
        netlist_path=netlist_path,
    )
    (reports_dir / f"{loaded_brief.name}.connectivity.json").write_text(
        connectivity.model_dump_json(indent=2), encoding="utf-8"
    )
    _record(
        run.log,
        {
            "tool": "circuit.connectivity_check",
            "payload": {"brief": str(run.brief_path), "netlist": str(netlist_path)},
            "result": connectivity.model_dump(mode="json"),
        },
    )
    if connectivity.verdict != "pass":
        raise StepFailure("circuit.connectivity_check", connectivity.model_dump_json())

    erc_result = kicad_cli.erc(schematic, reports_dir / f"{loaded_brief.name}.erc.json")
    _advisory_detail(
        run.advisory_results,
        "run_erc",
        {
            "kicad_cli_error_count": erc_result.errors,
            "kicad_cli_warning_count": erc_result.warnings,
        },
    )
    _record(
        run.log,
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
    _copy_project_context(run, schematic_snapshot)
    _record(
        run.log,
        {
            "tool": "circuit.snapshot.schematic_gate",
            "payload": {"source": str(schematic), "output": str(schematic_snapshot)},
            "result": {"path": str(schematic_snapshot)},
        },
    )
    return SchematicGate(sch_lint_result, connectivity, erc_result, schematic_snapshot)


def _stage_start_board_session(run: AuthoringRun) -> None:
    """Launch the KiCad API server and reconnect Konnect to it over IPC."""
    with (run.workdir / "api-server.log").open("w", encoding="utf-8") as api_log:
        run.server = subprocess.Popen(
            ["kicad-cli", "api-server", "--socket", str(run.socket_path), str(run.board)],
            stdout=api_log,
            stderr=subprocess.STDOUT,
            text=True,
            env=run.environment,
        )
    _wait_socket(run.socket_path, run.server)
    run.start_konnect({**run.environment, "KICAD_API_SOCKET": f"ipc://{run.socket_path}"})
    # The socket file appears before the KiCad IPC server answers
    # requests (AS_NOT_READY until the board finishes loading), so
    # poll check_kicad_ui until it reports responsive.
    ui_health: object = {}
    ui_health_dict: dict[str, object] = {}
    for attempt in range(10):
        ui_health = run.session.call("check_kicad_ui", {"timeout_seconds": 5})
        ui_health_dict = cast(dict[str, object], ui_health) if isinstance(ui_health, dict) else {}
        if ui_health_dict.get("ipc_responsive") is True:
            break
        if attempt < 9:
            time.sleep(2.0)
    if ui_health_dict.get("ipc_responsive") is not True:
        raise StepFailure("check_kicad_ui", f"IPC health check failed: {ui_health}")


def _stage_layout(run: AuthoringRun) -> None:
    """Sync the board from the schematic, draw the outline, and place footprints."""
    session = run.session
    loaded_brief = run.loaded_brief
    board = run.board
    update = session.call(
        "update_pcb_from_schematic",
        {"schematic": str(run.schematic), "board": str(board), "dry_run": True},
    )
    revision = _revision(update)
    if revision is None:
        raise StepFailure("update_pcb_from_schematic", "dry-run returned no plan revision")
    session.call(
        "update_pcb_from_schematic",
        {
            "schematic": str(run.schematic),
            "board": str(board),
            "dry_run": False,
            "expected_plan_revision": revision,
        },
    )
    session.call("save_project", {})
    session.call(
        "add_board_outline",
        {
            "board": str(board),
            "x1": 0.0,
            "y1": 0.0,
            "x2": loaded_brief.board.width_mm,
            "y2": loaded_brief.board.height_mm,
        },
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
        session.call(
            "set_component_placements", {"board": str(board), "placements": placement_values}
        )
    else:
        session.call("auto_place_from_schematic", {"board": str(board)})
    session.call("save_project", {})
    run.advise_all(
        "layout",
        [
            ("get_board_info", {"board": str(board)}),
            ("get_board_extents", {"board": str(board)}),
            ("get_layer_list", {"board": str(board)}),
            ("score_placement", {"board": str(board)}),
            (
                "refine_placement_force_directed",
                {"board": str(board), "dry_run": True},
            ),
            ("get_design_rules", {"board": str(board)}),
            ("list_design_rules", {"project_dir": str(run.workdir)}),
            ("get_netclasses", {"board": str(board)}),
        ],
        with_artifacts=False,
    )


def _collect_pads(
    run: AuthoringRun,
) -> tuple[dict[tuple[str, str], Point], dict[tuple[str, str], float]]:
    pad_positions: dict[tuple[str, str], Point] = {}
    pad_clearances: dict[tuple[str, str], float] = {}
    for part in run.loaded_brief.parts:
        pads = run.session.call(
            "get_component_pads", {"board": str(run.board), "reference": part.reference}
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
                    pad_clearances[key] = (
                        max(float(size_x), float(size_y)) / 2 + PAD_CLEARANCE_MARGIN_MM
                    )
                    continue
            pad_clearances[key] = PAD_CLEARANCE_MARGIN_MM
    return pad_positions, pad_clearances


def _stage_route(run: AuthoringRun) -> None:
    """Route every brief net on F.Cu with the axis-aligned autorouter."""
    loaded_brief = run.loaded_brief
    pad_positions, pad_clearances = _collect_pads(run)
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
                run.session.call(
                    "route_trace",
                    {
                        "board": str(run.board),
                        "net_name": f"/{net_item.name}",
                        "layer": "F.Cu",
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "width": TRACE_WIDTH_MM,
                    },
                )
                occupied.append((net_item.name, ((x1, y1), (x2, y2))))
    run.session.call("save_project", {})


def _stage_review(run: AuthoringRun) -> None:
    """Non-blocking Konnect design review; the IPC session stays up for manufacturing."""
    loaded_brief = run.loaded_brief
    schematic = run.schematic
    board = run.board
    konnect_exports = run.konnect_exports
    first_net_name = loaded_brief.nets[0].name if loaded_brief.nets else "GND"
    first_reference = loaded_brief.parts[0].reference if loaded_brief.parts else "R1"
    review_advisories: list[tuple[str, dict[str, object]]] = [
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
    run.advise_all("review", review_advisories, with_artifacts=True)
    run.stop_konnect()


def _stage_board_gate(run: AuthoringRun, schematic_snapshot: Path) -> BoardGate:
    """Blocking board gates: kicad-cli DRC, renders, jobset consistency, diffs."""
    loaded_brief = run.loaded_brief
    board = run.board
    reports_dir = run.reports_dir
    drc_result = kicad_cli.drc(board, reports_dir / f"{loaded_brief.name}.drc.json")
    for tool in ("run_drc", "get_drc_violations"):
        _advisory_detail(
            run.advisory_results,
            tool,
            {
                "kicad_cli_error_count": drc_result.errors,
                "kicad_cli_warning_count": drc_result.warnings,
            },
        )
    _record(
        run.log,
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
        run.log,
        {
            "tool": "circuit.snapshot.drc_gate",
            "payload": {"source": str(board), "output": str(board_snapshot)},
            "result": {"path": str(board_snapshot)},
        },
    )
    render_dir = reports_dir / "render"
    renders: list[str] = []
    for side in ("top", "bottom"):
        render_path = kicad_cli.render(
            board,
            render_dir / f"{loaded_brief.name}-{side}.png",
            side=side,
        )
        renders.append(str(render_path))
        _record(
            run.log,
            {
                "tool": f"circuit.kicad_cli.render:{side}",
                "payload": {"board": str(board), "output": str(render_path), "side": side},
                "result": {"path": str(render_path)},
            },
        )
    # Rendering can materialize project library tables in the board file.
    shutil.copy2(board, board_snapshot)
    _copy_project_context(run, board_snapshot)
    for table_name in ("fp-lib-table", "sym-lib-table"):
        table = run.workdir / table_name
        if table.is_file():
            shutil.copy2(table, board_snapshot.parent / table_name)
    jobset_result = kicad_cli.jobset_run(run.project, run.workdir / "jobset-out")
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
        run.log,
        {
            "tool": "circuit.kicad_cli.jobset_run",
            "payload": {
                "project": str(run.project),
                "output_dir": str(jobset_result.output_dir),
            },
            "result": jobset_result.model_dump(mode="json"),
        },
    )
    if not jobset_consistent:
        raise StepFailure("circuit.kicad_cli.jobset_consistency", "jobset reports disagree")
    diffs = {
        "schematic": kicad_cli.diff(
            "sch",
            schematic_snapshot,
            run.schematic,
            reports_dir / "diffs" / "schematic.json",
        ),
        "pcb": kicad_cli.diff(
            "pcb",
            board_snapshot,
            board,
            reports_dir / "diffs" / "pcb.json",
        ),
    }
    version_about = kicad_cli.version_about()
    _record(
        run.log,
        {
            "tool": "circuit.kicad_cli.version_about",
            "result": {"about": version_about},
        },
    )
    return BoardGate(
        drc_result,
        board_snapshot,
        renders,
        jobset_result,
        jobset_consistent,
        diffs,
        version_about,
    )


def _copy_project_context(run: AuthoringRun, snapshot: Path) -> None:
    """Give a gate snapshot the project and drawing sheet it was gated with.

    kicad-cli resolves the drawing sheet through the sibling project file;
    without it a later ``diff`` reports a changed "Drawing Sheet File".
    """
    shutil.copy2(run.project, snapshot.with_suffix(".kicad_pro"))
    sheet = run.project.with_suffix(".kicad_wks")
    if sheet.is_file():
        shutil.copy2(sheet, snapshot.parent / sheet.name)


def _apply_drawing_sheet(run: AuthoringRun) -> None:
    """Point the project at the ISO 7200 sheet and refresh its variables."""
    text_variables = drawing_sheet.variables(
        run.loaded_brief, brief_sha256=brief.brief_sha256(run.brief_path)
    )
    try:
        sheet = drawing_sheet.apply(run.project, text_variables=text_variables)
    except ValueError as exc:
        raise StepFailure("circuit_drawing_sheet", str(exc)) from exc
    _record(
        run.log,
        {
            "step": "circuit_drawing_sheet",
            "tool": "circuit.drawing_sheet.apply",
            "payload": {"project": str(run.project)},
            "result": {"sheet": str(sheet), "text_variables": text_variables},
        },
    )


def _stage_exports(run: AuthoringRun) -> None:
    # Re-apply right before plotting: a KiCad session that saved the
    # project during layout must not leave the exports on the default sheet.
    _apply_drawing_sheet(run)
    board = run.board
    schematic = run.schematic
    exports_dir = run.workdir / "exports"
    for kind, source in (
        ("gerbers", board),
        ("drill", board),
        ("bom", schematic),
        ("pos", board),
        ("sch_pdf", schematic),
        ("sch_svg", schematic),
        ("pcb_pdf", board),
        ("pcb_svg", board),
        ("dxf", board),
        ("ipc2581", board),
        ("odb", board),
        ("gencad", board),
        ("vrml", board),
        ("glb", board),
    ):
        directory = exports_dir / kind
        paths = kicad_cli.export(cast(kicad_cli.ExportKind, kind), source, directory)
        run.exports[kind] = [str(path) for path in paths]
        _record(
            run.log,
            {
                "tool": f"circuit.kicad_cli.export:{kind}",
                "payload": {"source": str(source), "output_dir": str(directory)},
                "result": run.exports[kind],
            },
        )


def _stage_manufacturing_advisories(run: AuthoringRun) -> None:
    board = run.board
    konnect_exports = run.konnect_exports
    # Konnect 0.13.0 export_manufacturing_package requires native midpoint
    # geometry over IPC; reuse the board session started for layout/review.
    run.start_konnect({**run.environment, "KICAD_API_SOCKET": f"ipc://{run.socket_path}"})
    manufacturing_advisories: list[tuple[str, dict[str, object]]] = [
        (
            "export_manufacturing_package",
            {
                "board": str(board),
                "schematic": str(run.schematic),
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
    run.advise_all("manufacturing", manufacturing_advisories, with_artifacts=True)
    run.stop_konnect()
    apiserver.stop()
    run.socket_path.unlink(missing_ok=True)


def _run_pipeline(run: AuthoringRun, result: dict[str, object]) -> PipelineOutput:
    intake_result = _stage_intake(run, result)
    library_result = _stage_library_gate(run, result)
    _stage_create_project(run)
    _stage_author_schematic(run)
    _stage_schematic_advisories(run)
    schematic_gate = _stage_schematic_gate(run)
    _stage_start_board_session(run)
    _stage_layout(run)
    _stage_route(run)
    _stage_review(run)
    board_gate = _stage_board_gate(run, schematic_gate.snapshot)
    _stage_exports(run)
    _stage_manufacturing_advisories(run)
    return PipelineOutput(intake_result, library_result, schematic_gate, board_gate)


def _write_success(
    run: AuthoringRun,
    output: PipelineOutput,
    result: dict[str, object],
    output_path: Path,
) -> int:
    """Build the design report + provenance and emit the run result JSON."""
    schematic_gate = output.schematic
    board_gate = output.board
    design_report = report.build_design_report(
        run.loaded_brief,
        brief_path=run.brief_path,
        kicad_version=schematic_gate.erc.kicad_version,
        connectivity=schematic_gate.connectivity,
        sch_lint=schematic_gate.sch_lint,
        erc=schematic_gate.erc,
        drc=board_gate.drc,
        exports=run.exports,
        advisory=run.advisory_results,
        renders=board_gate.renders,
        jobset=board_gate.jobset,
        jobset_consistent=board_gate.jobset_consistent,
        diffs=board_gate.diffs,
    )
    report_path = run.reports_dir / "design-report.json"
    report.write_report(design_report, report_path)
    provenance_path = _write_provenance(
        run.workdir,
        run.brief_path,
        run.intake_path,
        version_about=board_gate.version_about,
    )
    result.update(
        {
            "verdict": design_report.verdict,
            "design_report": str(report_path),
            "provenance": str(provenance_path),
            "intake": (
                output.intake.model_dump(mode="json") if output.intake is not None else None
            ),
            "libraries": output.libraries.model_dump(mode="json"),
            "connectivity": schematic_gate.connectivity.model_dump(mode="json"),
            "sch_lint": schematic_gate.sch_lint.model_dump(mode="json"),
            "erc": schematic_gate.erc.model_dump(mode="json"),
            "drc": board_gate.drc.model_dump(mode="json"),
            "renders": board_gate.renders,
            "jobset": board_gate.jobset.model_dump(mode="json"),
            "jobset_consistent": board_gate.jobset_consistent,
            "diffs": {
                name: item.model_dump(mode="json") for name, item in board_gate.diffs.items()
            },
            "version_about": board_gate.version_about,
            "advisory_counts": {
                status: sum(item.status == status for item in run.advisory_results)
                for status in ("ok", "error", "not_applicable")
            },
            "advisory": [item.model_dump(mode="json") for item in run.advisory_results],
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
                "sch_lint_verdict": schematic_gate.sch_lint.verdict,
                "sch_lint_warnings": schematic_gate.sch_lint.warnings,
                "drc_verdict": board_gate.drc.verdict,
            },
            ensure_ascii=False,
        )
    )
    return 0 if design_report.verdict == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--kicad-share", type=Path, default=Path("/usr/share/kicad-nightly"))
    parser.add_argument("--intake", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    brief_path: Path = args.brief
    workdir: Path = args.workdir
    kicad_share: Path = args.kicad_share
    intake_path: Path | None = args.intake
    json_path: Path | None = args.json

    workdir.mkdir(parents=True, exist_ok=True)
    output_path = json_path or workdir / "e2e-authoring.json"
    result: dict[str, object] = {
        "verdict": "fail",
        "brief": str(brief_path),
        "workdir": str(workdir),
    }
    try:
        loaded_brief = brief.load_brief(brief_path)
    except (ValueError, OSError) as exc:
        return _write_failure(output_path, result, "load", exc)
    reports_dir = workdir / "circuit-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / "authoring.jsonl"
    socket_path = Path("/tmp/circuit-kicad.sock")
    socket_path.unlink(missing_ok=True)

    run: AuthoringRun | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            konnect_exports = workdir / "konnect-exports"
            konnect_exports.mkdir(parents=True, exist_ok=True)
            run = AuthoringRun(
                brief_path=brief_path,
                intake_path=intake_path,
                workdir=workdir,
                kicad_share=kicad_share,
                loaded_brief=loaded_brief,
                log=log,
                reports_dir=reports_dir,
                konnect_exports=konnect_exports,
                project=workdir / f"{loaded_brief.name}.kicad_pro",
                schematic=workdir / f"{loaded_brief.name}.kicad_sch",
                board=workdir / f"{loaded_brief.name}.kicad_pcb",
                socket_path=socket_path,
                environment={
                    **os.environ,
                    "HOME": str(workdir / "home"),
                    "KICAD10_SYMBOL_DIR": str(kicad_share / "symbols"),
                },
            )
            output = _run_pipeline(run, result)
        return _write_success(run, output, result, output_path)
    except StepFailure as exc:
        return _write_failure(output_path, result, exc.step, exc)
    except Exception as exc:
        return _write_failure(output_path, result, "unexpected", exc)
    finally:
        if run is not None:
            run.stop_konnect()
            run.stop_server()
        apiserver.stop()
        socket_path.unlink(missing_ok=True)
        _prune_empty_dirs(workdir / "konnect-exports")


if __name__ == "__main__":
    raise SystemExit(main())
