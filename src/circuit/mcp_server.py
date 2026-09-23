"""Low-level stdio MCP server for circuit operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any, Literal, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ContentBlock,
    ImageContent,
    ServerCapabilities,
    TextContent,
    Tool,
    ToolsCapability,
)
from pydantic import BaseModel, ValidationError

from . import __version__, apiserver, brief, intake, kicad_cli, libraries, netlist, report, sch_lint
from .advisory import AdvisoryResult
from .paths import KONNECT_SOCKET_URL

server = Server("circuit", version=__version__)

_TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "circuit_api_server_start",
        "Start KiCad API server",
        {
            "type": "object",
            "properties": {"board_path": {"type": "string"}},
            "required": ["board_path"],
        },
    ),
    ("circuit_api_server_status", "Get API server status", {"type": "object", "properties": {}}),
    ("circuit_api_server_stop", "Stop KiCad API server", {"type": "object", "properties": {}}),
    (
        "circuit_brief_validate",
        "Validate a machine-readable design brief",
        {
            "type": "object",
            "properties": {"brief_path": {"type": "string"}},
            "required": ["brief_path"],
        },
    ),
    (
        "circuit_brief_intake_check",
        "Check design brief conversation provenance",
        {
            "type": "object",
            "properties": {
                "brief_path": {"type": "string"},
                "intake_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["brief_path", "intake_path"],
        },
    ),
    (
        "circuit_brief_library_check",
        "Resolve design brief libraries and pins",
        {
            "type": "object",
            "properties": {
                "brief_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["brief_path"],
        },
    ),
    (
        "circuit_netlist_export",
        "Export a KiCad schematic netlist",
        {
            "type": "object",
            "properties": {
                "schematic_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["schematic_path"],
        },
    ),
    (
        "circuit_connectivity_check",
        "Check authoritative schematic connectivity against a design brief",
        {
            "type": "object",
            "properties": {
                "brief_path": {"type": "string"},
                "schematic_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["brief_path", "schematic_path"],
        },
    ),
    (
        "circuit_design_report",
        "Build a fail-closed design report from gate reports, exports, renders, jobset and diffs",
        {
            "type": "object",
            "properties": {
                "brief_path": {"type": "string"},
                "schematic_path": {"type": "string"},
                "board_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["brief_path", "schematic_path", "board_path"],
        },
    ),
    (
        "circuit_sch_lint",
        "Lint schematic readability (label placement, bounds)",
        {
            "type": "object",
            "properties": {
                "schematic_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["schematic_path"],
        },
    ),
    (
        "circuit_erc",
        "Run KiCad ERC",
        {
            "type": "object",
            "properties": {
                "schematic_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["schematic_path"],
        },
    ),
    (
        "circuit_drc",
        "Run KiCad DRC",
        {
            "type": "object",
            "properties": {
                "board_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["board_path"],
        },
    ),
    (
        "circuit_render",
        "Render board or schematic views for visual review; board3d writes one "
        "PNG/JPEG to output_path, schematic and layers kinds plot PNG pages "
        "into output_dir and attach up to 4 images inline",
        {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["board3d", "schematic", "layers"],
                    "default": "board3d",
                },
                "board_path": {
                    "type": "string",
                    "description": "input .kicad_pcb (board3d, layers)",
                },
                "schematic_path": {
                    "type": "string",
                    "description": "input .kicad_sch (schematic)",
                },
                "output_path": {
                    "type": "string",
                    "description": "image file path (board3d)",
                },
                "output_dir": {
                    "type": "string",
                    "description": "PNG output directory (schematic, layers)",
                },
                "side": {
                    "type": "string",
                    "enum": ["top", "bottom", "left", "right", "front", "back"],
                    "default": "top",
                },
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 720},
                "rotate": {
                    "type": "string",
                    "description": "camera rotation 'X,Y,Z' degrees, e.g. '-45,0,45'",
                },
                "zoom": {"type": "number"},
                "pan": {"type": "string", "description": "camera pan 'X,Y,Z'"},
                "pivot": {
                    "type": "string",
                    "description": "pivot point in cm from board center, 'X,Y,Z'",
                },
                "perspective": {"type": "boolean"},
                "floor": {"type": "boolean"},
                "background": {
                    "type": "string",
                    "enum": ["default", "transparent", "opaque"],
                },
                "quality": {
                    "type": "string",
                    "enum": ["basic", "high", "user", "job_settings"],
                },
                "pages": {
                    "type": "string",
                    "description": "schematic page list, e.g. '1,3'",
                },
                "black_and_white": {"type": "boolean"},
                "exclude_drawing_sheet": {"type": "boolean"},
                "dpi": {"type": "integer", "default": 300},
                "layers": {
                    "type": "string",
                    "description": "comma-separated layer names, e.g. 'F.Cu,F.Fab,Edge.Cuts'",
                },
                "common_layers": {
                    "type": "string",
                    "description": "layers drawn on every plot, e.g. 'Edge.Cuts'",
                },
                "mirror": {"type": "boolean"},
                "scale": {"type": "integer", "description": "plot scale; 0 = autoscale"},
                "sketch_pads_on_fab_layers": {"type": "boolean"},
                "sketch_pad_numbers": {"type": "boolean"},
                "include_border_title": {"type": "boolean"},
                "theme": {"type": "string"},
            },
        },
    ),
    (
        "circuit_diff",
        "Compare two KiCad schematic or PCB files; png/svg formats produce a visual diff artifact",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["sch", "pcb"]},
                "left_path": {"type": "string"},
                "right_path": {"type": "string"},
                "output_path": {"type": "string"},
                "format": {
                    "type": "string",
                    "enum": ["json", "png", "svg"],
                    "default": "json",
                },
            },
            "required": ["kind", "left_path", "right_path", "output_path"],
        },
    ),
    (
        "circuit_jobset_run",
        "Run the declarative KiCad jobset",
        {
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
                "output_dir": {"type": "string"},
                "jobset_path": {"type": "string"},
            },
            "required": ["project_path", "output_dir"],
        },
    ),
    (
        "circuit_export",
        "Export KiCad artifacts",
        {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
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
                    ],
                },
                "source_path": {
                    "type": "string",
                    "description": "KiCad source path; for fp_svg a footprint "
                    "library directory containing .kicad_mod files",
                },
                "output_dir": {"type": "string"},
            },
            "required": ["kind", "source_path", "output_dir"],
        },
    ),
    (
        "circuit_konnect_call",
        "Invoke Konnect operations through a managed stdio session when "
        "dynamically loaded toolsets are not visible to the harness; "
        "pass ops to run several calls (including load_toolset) in one session",
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "arguments": {"type": "object"},
                "socket": {"type": "string"},
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                },
            },
        },
    ),
    ("circuit_kicad_version", "Get KiCad version", {"type": "object", "properties": {}}),
]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _output_path(source: Path, output: str | None, kind: str) -> Path:
    path = (
        Path(output) if output else source.parent / "circuit-reports" / f"{source.stem}.{kind}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _netlist_path(source: Path, output: str | None) -> Path:
    path = Path(output) if output else source.parent / "circuit-reports" / f"{source.stem}.net"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _required_string(args: dict[str, Any], name: str, context: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} requires '{name}'")
    return value


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


_BASE64_MIN_LENGTH = 1024


def _konnect_image_dir() -> Path:
    override = os.environ.get("CIRCUIT_KONNECT_IMAGE_DIR")
    if override:
        return Path(override)
    return Path.cwd() / "circuit-reports" / "konnect-images"


def _decode_image_payload(value: str) -> tuple[bytes, str] | None:
    candidate = value
    if candidate.startswith("data:"):
        prefix, separator, candidate = candidate.partition(",")
        if not separator or not prefix.startswith("data:image/"):
            return None
    if len(candidate) < _BASE64_MIN_LENGTH:
        return None
    try:
        data = base64.b64decode(candidate, validate=True)
    except ValueError:
        return None
    if data.startswith(b"\x89PNG"):
        return data, ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return data, ".jpg"
    return None


def _rewrite_base64_images(value: Any, image_dir: Path, counter: list[int]) -> Any:
    if isinstance(value, str):
        decoded = _decode_image_payload(value)
        if decoded is None:
            return value
        data, suffix = decoded
        image_dir.mkdir(parents=True, exist_ok=True)
        counter[0] += 1
        path = image_dir / f"{counter[0]:03d}{suffix}"
        path.write_bytes(data)
        return {
            "image_path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    if isinstance(value, dict):
        return {
            key: _rewrite_base64_images(item, image_dir, counter)
            for key, item in cast(dict[str, Any], value).items()
        }
    if isinstance(value, list):
        return [_rewrite_base64_images(item, image_dir, counter) for item in cast(list[Any], value)]
    return value


def _rewrite_text_block_images(text: str, image_dir: Path, counter: list[int]) -> str:
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(_rewrite_base64_images(parsed, image_dir, counter), ensure_ascii=False)


async def _konnect_call(
    tool: str,
    arguments: dict[str, Any],
    socket: str | None,
    ops: list[dict[str, Any]] | None = None,
) -> CallToolResult:
    command = shlex.split(os.environ.get("CIRCUIT_KONNECT", "konnect"))
    env = {
        **os.environ,
        "KICAD_API_SOCKET": socket or os.environ.get("KICAD_API_SOCKET") or KONNECT_SOCKET_URL,
    }
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    counter = [0]
    image_dir = _konnect_image_dir()
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        if ops is None:
            result = await session.call_tool(tool, arguments)
            for index, block in enumerate(result.content):
                text = getattr(block, "text", None)
                if text is not None:
                    result.content[index] = TextContent(
                        type="text",
                        text=_rewrite_text_block_images(text, image_dir, counter),
                    )
            return result
        results: list[dict[str, Any]] = []
        for op in ops:
            op_tool = str(op.get("tool", ""))
            op_arguments_value = op.get("arguments")
            op_arguments: dict[str, Any] = (
                cast(dict[str, Any], op_arguments_value)
                if isinstance(op_arguments_value, dict)
                else {}
            )
            op_result = await session.call_tool(op_tool, op_arguments)
            content: list[Any] = []
            for block in op_result.content:
                text = getattr(block, "text", None)
                if text is not None:
                    content.append(_rewrite_text_block_images(text, image_dir, counter))
                else:
                    content.append(
                        _rewrite_base64_images(block.model_dump(mode="json"), image_dir, counter)
                    )
            results.append(
                {
                    "tool": op_tool,
                    "isError": bool(op_result.isError),
                    "content": content,
                }
            )
        return CallToolResult(
            content=[TextContent(type="text", text=_json({"results": results}))],
            isError=any(item["isError"] for item in results),
        )


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name=name, description=description, inputSchema=schema)
        for name, description, schema in _TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    args = arguments or {}
    image_paths: list[Path] = []
    try:
        if name == "circuit_api_server_start":
            result = apiserver.start(Path(str(args["board_path"])))
        elif name == "circuit_api_server_status":
            result = apiserver.status()
        elif name == "circuit_api_server_stop":
            result = apiserver.stop()
        elif name == "circuit_brief_validate":
            path = Path(str(args["brief_path"]))
            loaded = brief.load_brief(path)
            result = {
                "brief": loaded.model_dump(mode="json"),
                "brief_sha256": brief.brief_sha256(path),
            }
        elif name == "circuit_brief_intake_check":
            brief_path = Path(str(args["brief_path"]))
            intake_path = Path(str(args["intake_path"]))
            result = intake.check_intake(
                brief.load_brief(brief_path),
                intake.load_intake(intake_path),
                brief_path=brief_path,
                intake_path=intake_path,
            )
            output = _output_path(
                brief_path,
                _optional_string(args.get("output_path")),
                "intake",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_brief_library_check":
            brief_path = Path(str(args["brief_path"]))
            result = libraries.check_libraries(
                brief.load_brief(brief_path),
                brief_path=brief_path,
            )
            output = _output_path(
                brief_path,
                _optional_string(args.get("output_path")),
                "libraries",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_netlist_export":
            source = Path(str(args["schematic_path"]))
            result = str(
                kicad_cli.export_netlist(
                    source, _netlist_path(source, _optional_string(args.get("output_path")))
                )
            )
        elif name == "circuit_connectivity_check":
            brief_path = Path(str(args["brief_path"]))
            source = Path(str(args["schematic_path"]))
            netlist_path = kicad_cli.export_netlist(source, _netlist_path(source, None))
            connectivity = netlist.check_connectivity(
                brief.load_brief(brief_path),
                netlist.parse_netlist(netlist_path),
                brief_path=brief_path,
                netlist_path=netlist_path,
            )
            output = _output_path(source, args.get("output_path"), "connectivity")
            output.write_text(connectivity.model_dump_json(indent=2), encoding="utf-8")
            result = connectivity
        elif name == "circuit_design_report":
            brief_path = Path(str(args["brief_path"]))
            schematic = Path(str(args["schematic_path"]))
            board = Path(str(args["board_path"]))
            reports = schematic.parent / "circuit-reports"
            connectivity_path = reports / f"{schematic.stem}.connectivity.json"
            sch_lint_path = reports / f"{schematic.stem}.sch_lint.json"
            erc_path = reports / f"{schematic.stem}.erc.json"
            drc_path = reports / f"{board.stem}.drc.json"
            connectivity = (
                netlist.ConnectivityReport.model_validate_json(
                    connectivity_path.read_text(encoding="utf-8")
                )
                if connectivity_path.is_file()
                else None
            )
            sch_lint_report = (
                sch_lint.SchLintReport.model_validate_json(
                    sch_lint_path.read_text(encoding="utf-8")
                )
                if sch_lint_path.is_file()
                else None
            )
            erc_report = (
                _load_report(erc_path, kind="erc", source=schematic) if erc_path.is_file() else None
            )
            drc_report = (
                _load_report(drc_path, kind="drc", source=board) if drc_path.is_file() else None
            )
            reports_dirs = _reports_dirs(schematic, board)
            jobset = _collect_jobset(reports_dirs)
            result = report.build_design_report(
                brief.load_brief(brief_path),
                brief_path=brief_path,
                kicad_version=(
                    erc_report.kicad_version
                    if erc_report is not None
                    else drc_report.kicad_version
                    if drc_report is not None
                    else "unknown"
                ),
                connectivity=connectivity,
                sch_lint=sch_lint_report,
                erc=erc_report,
                drc=drc_report,
                exports=_collect_exports([schematic.parent, board.parent]),
                advisory=_collect_advisory(reports_dirs),
                renders=_collect_renders(reports_dirs),
                jobset=jobset,
                jobset_consistent=_jobset_consistent(jobset, erc_report, drc_report),
                diffs=_collect_diffs(reports_dirs),
            )
            output = (
                Path(str(args["output_path"]))
                if args.get("output_path")
                else reports / f"{schematic.stem}.design-report.json"
            )
            report.write_report(result, output)
        elif name == "circuit_sch_lint":
            source = Path(str(args["schematic_path"]))
            result = sch_lint.lint_file(
                source, _output_path(source, args.get("output_path"), "sch_lint")
            )
        elif name == "circuit_erc":
            source = Path(str(args["schematic_path"]))
            result = kicad_cli.erc(source, _output_path(source, args.get("output_path"), "erc"))
        elif name == "circuit_drc":
            source = Path(str(args["board_path"]))
            result = kicad_cli.drc(source, _output_path(source, args.get("output_path"), "drc"))
        elif name == "circuit_render":
            render_kind = str(args.get("kind", "board3d"))
            if render_kind == "board3d":
                background_arg = _optional_string(args.get("background"))
                quality_arg = _optional_string(args.get("quality"))
                zoom_arg = args.get("zoom")
                result = str(
                    kicad_cli.render(
                        Path(_required_string(args, "board_path", "kind 'board3d'")),
                        Path(_required_string(args, "output_path", "kind 'board3d'")),
                        side=cast(kicad_cli.CameraSide, str(args.get("side", "top"))),
                        width=int(args.get("width", 1280)),
                        height=int(args.get("height", 720)),
                        rotate=_optional_string(args.get("rotate")),
                        zoom=float(cast(float, zoom_arg)) if zoom_arg is not None else None,
                        pan=_optional_string(args.get("pan")),
                        pivot=_optional_string(args.get("pivot")),
                        perspective=bool(args.get("perspective", False)),
                        floor=bool(args.get("floor", False)),
                        background=(
                            cast(kicad_cli.RenderBackground, background_arg)
                            if background_arg
                            else None
                        ),
                        quality=(
                            cast(kicad_cli.RenderQuality, quality_arg) if quality_arg else None
                        ),
                    )
                )
                image_paths = [Path(str(result))]
            elif render_kind == "schematic":
                images = kicad_cli.render_schematic(
                    Path(_required_string(args, "schematic_path", "kind 'schematic'")),
                    Path(_required_string(args, "output_dir", "kind 'schematic'")),
                    pages=_optional_string(args.get("pages")),
                    dpi=int(args.get("dpi", 300)),
                    black_and_white=bool(args.get("black_and_white", False)),
                    exclude_drawing_sheet=bool(args.get("exclude_drawing_sheet", False)),
                    theme=_optional_string(args.get("theme")),
                )
                result = {
                    "output_dir": str(Path(str(args["output_dir"]))),
                    "images": [str(path) for path in images],
                }
                image_paths = images
            elif render_kind == "layers":
                scale_arg = args.get("scale")
                images = kicad_cli.render_layers(
                    Path(_required_string(args, "board_path", "kind 'layers'")),
                    Path(_required_string(args, "output_dir", "kind 'layers'")),
                    layers=_required_string(args, "layers", "kind 'layers'"),
                    common_layers=_optional_string(args.get("common_layers")),
                    mirror=bool(args.get("mirror", False)),
                    scale=int(cast(int, scale_arg)) if scale_arg is not None else None,
                    sketch_pads_on_fab_layers=bool(args.get("sketch_pads_on_fab_layers", False)),
                    sketch_pad_numbers=bool(args.get("sketch_pad_numbers", False)),
                    black_and_white=bool(args.get("black_and_white", False)),
                    include_border_title=bool(args.get("include_border_title", False)),
                    dpi=int(args.get("dpi", 300)),
                    theme=_optional_string(args.get("theme")),
                )
                result = {
                    "output_dir": str(Path(str(args["output_dir"]))),
                    "images": [str(path) for path in images],
                }
                image_paths = images
            else:
                raise ValueError(f"unknown circuit_render kind: {render_kind}")
        elif name == "circuit_diff":
            diff_format = cast(kicad_cli.DiffFormat, str(args.get("format", "json")))
            result = kicad_cli.diff(
                cast(Literal["sch", "pcb"], str(args["kind"])),
                Path(str(args["left_path"])),
                Path(str(args["right_path"])),
                Path(str(args["output_path"])),
                format=diff_format,
            )
            if diff_format == "png":
                image_paths = [Path(str(args["output_path"]))]
        elif name == "circuit_jobset_run":
            jobset_arg = args.get("jobset_path")
            project = Path(str(args["project_path"]))
            result = kicad_cli.jobset_run(
                project,
                Path(str(args["output_dir"])),
                Path(str(jobset_arg)) if isinstance(jobset_arg, str) else None,
            )
            record = _output_path(project, None, "jobset")
            record.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_export":
            result = kicad_cli.export(
                cast(kicad_cli.ExportKind, str(args["kind"])),
                Path(str(args["source_path"])),
                Path(str(args["output_dir"])),
            )
        elif name == "circuit_konnect_call":
            konnect_arguments = args.get("arguments")
            ops = args.get("ops")
            if ops is not None:
                if not isinstance(ops, list) or not all(
                    isinstance(item, dict) for item in cast(list[Any], ops)
                ):
                    raise ValueError("circuit_konnect_call 'ops' must be a list of objects")
                return await _konnect_call(
                    "",
                    {},
                    _optional_string(args.get("socket")),
                    ops=cast(list[dict[str, Any]], ops),
                )
            tool = args.get("tool")
            if not isinstance(tool, str) or not tool:
                raise ValueError("circuit_konnect_call requires 'tool' or 'ops'")
            return await _konnect_call(
                tool,
                cast(dict[str, Any], konnect_arguments)
                if isinstance(konnect_arguments, dict)
                else {},
                _optional_string(args.get("socket")),
            )
        elif name == "circuit_kicad_version":
            result = kicad_cli.version()
        else:
            raise ValueError(f"unknown tool: {name}")
        value = result.model_dump() if isinstance(result, BaseModel) else result
        content: list[ContentBlock] = [TextContent(type="text", text=_json(value))]
        for image_path in image_paths[:_MAX_INLINE_IMAGES]:
            image = _image_content(image_path)
            if image is not None:
                content.append(image)
        return CallToolResult(content=content)
    except Exception as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=_json({"error": str(exc)}))],
            isError=True,
        )


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="circuit",
                server_version=__version__,
                capabilities=ServerCapabilities(tools=ToolsCapability()),
            ),
        )


if __name__ == "__main__":
    asyncio.run(_run())
