"""Low-level stdio MCP server for circuit operations."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Literal, cast

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
from pydantic import BaseModel

from . import __version__, apiserver, brief, intake, kicad_cli, libraries, netlist, report

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
        "Build a fail-closed design report from existing gate reports",
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
        "Render a PCB for visual review",
        {
            "type": "object",
            "properties": {
                "board_path": {"type": "string"},
                "output_path": {"type": "string"},
                "side": {"type": "string", "enum": ["top", "bottom"]},
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 720},
            },
            "required": ["board_path", "output_path", "side"],
        },
    ),
    (
        "circuit_diff",
        "Compare two KiCad schematic or PCB files",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["sch", "pcb"]},
                "left_path": {"type": "string"},
                "right_path": {"type": "string"},
                "output_path": {"type": "string"},
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
                    ],
                },
                "source_path": {"type": "string"},
                "output_dir": {"type": "string"},
            },
            "required": ["kind", "source_path", "output_dir"],
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


def _image_content(path: Path) -> ImageContent | None:
    if path.suffix.lower() != ".png" or not path.is_file():
        return None
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return ImageContent(type="image", data=data, mimeType="image/png")


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


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name=name, description=description, inputSchema=schema)
        for name, description, schema in _TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    args = arguments or {}
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
            erc_path = reports / f"{schematic.stem}.erc.json"
            drc_path = reports / f"{board.stem}.drc.json"
            connectivity = (
                netlist.ConnectivityReport.model_validate_json(
                    connectivity_path.read_text(encoding="utf-8")
                )
                if connectivity_path.is_file()
                else None
            )
            erc_report = (
                _load_report(erc_path, kind="erc", source=schematic) if erc_path.is_file() else None
            )
            drc_report = (
                _load_report(drc_path, kind="drc", source=board) if drc_path.is_file() else None
            )
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
                erc=erc_report,
                drc=drc_report,
                exports={},
            )
            output = (
                Path(str(args["output_path"]))
                if args.get("output_path")
                else reports / f"{schematic.stem}.design-report.json"
            )
            report.write_report(result, output)
        elif name == "circuit_erc":
            source = Path(str(args["schematic_path"]))
            result = kicad_cli.erc(source, _output_path(source, args.get("output_path"), "erc"))
        elif name == "circuit_drc":
            source = Path(str(args["board_path"]))
            result = kicad_cli.drc(source, _output_path(source, args.get("output_path"), "drc"))
        elif name == "circuit_render":
            result = kicad_cli.render(
                Path(str(args["board_path"])),
                Path(str(args["output_path"])),
                side=cast(Literal["top", "bottom"], str(args["side"])),
                width=int(args.get("width", 1280)),
                height=int(args.get("height", 720)),
            )
        elif name == "circuit_diff":
            result = kicad_cli.diff(
                cast(Literal["sch", "pcb"], str(args["kind"])),
                Path(str(args["left_path"])),
                Path(str(args["right_path"])),
                Path(str(args["output_path"])),
            )
        elif name == "circuit_jobset_run":
            jobset_arg = args.get("jobset_path")
            result = kicad_cli.jobset_run(
                Path(str(args["project_path"])),
                Path(str(args["output_dir"])),
                Path(str(jobset_arg)) if isinstance(jobset_arg, str) else None,
            )
        elif name == "circuit_export":
            result = kicad_cli.export(
                cast(kicad_cli.ExportKind, str(args["kind"])),
                Path(str(args["source_path"])),
                Path(str(args["output_dir"])),
            )
        elif name == "circuit_kicad_version":
            result = kicad_cli.version()
        else:
            raise ValueError(f"unknown tool: {name}")
        value = result.model_dump() if isinstance(result, BaseModel) else result
        content: list[ContentBlock] = [TextContent(type="text", text=_json(value))]
        if name == "circuit_render":
            image = _image_content(Path(str(args["output_path"])))
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
