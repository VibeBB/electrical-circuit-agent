"""Low-level stdio MCP server for circuit operations."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any, cast

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ContentBlock,
    ServerCapabilities,
    TextContent,
    Tool,
    ToolAnnotations,
    ToolsCapability,
)
from pydantic import BaseModel

from . import (
    __version__,
    advisory,
    apiserver,
    authoring,
    brief,
    connectivity,
    datasheet,
    doctor,
    firmware,
    fit_sheet,
    intake,
    kicad_cli,
    landpattern,
    libraries,
    libraryvision,
    libreuse,
    libreview,
    libsource,
    libverify,
    netlist,
    partspec,
    raster,
    report,
    ruleprofile,
    sch_lint,
    stackup,
    visionread,
)
from . import mcp_konnect as _mcp_konnect
from .mcp_args import (
    _json,
    _literal,
    _netlist_path,
    _optional_literal,
    _optional_string,
    _output_path,
    _required_string,
    _socket_url,
)
from .mcp_args import (
    _workspace_arguments as _workspace_arguments_from_args,
)
from .mcp_collect import (
    _MAX_INLINE_IMAGES,
    _collect_advisory,
    _collect_diffs,
    _collect_exports,
    _collect_jobset,
    _collect_renders,
    _image_content,
    _jobset_consistent,
    _load_report,
    _reports_dirs,
)
from .mcp_konnect import _konnect_call, _rewrite_base64_images

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
        "circuit_connectivity_export",
        "Emit the wire-agent ConnectivitySource contract (*.connectivity.json)",
        {
            "type": "object",
            "properties": {
                "brief_path": {"type": "string"},
                "netlist_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["brief_path"],
        },
    ),
    (
        "circuit_firmware_export",
        "Emit MCU pin connectivity for firmware-agent (*.firmware.json)",
        {
            "type": "object",
            "properties": {
                "brief_path": {"type": "string"},
                "netlist_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["brief_path"],
        },
    ),
    (
        "circuit_firmware_check",
        "Check a firmware-agent pin map (*.fw-pinmap.json) against the circuit",
        {
            "type": "object",
            "properties": {
                "brief_path": {"type": "string"},
                "pinmap_path": {"type": "string"},
                "netlist_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["brief_path", "pinmap_path"],
        },
    ),
    (
        "circuit_doctor",
        "Report circuit environment diagnostics",
        {"type": "object", "properties": {}},
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
        "circuit_fit_sheet",
        "Clamp out-of-bounds schematic labels back inside the sheet",
        {
            "type": "object",
            "properties": {
                "schematic_path": {"type": "string"},
                "margin": {"type": "number"},
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
                    "enum": list(kicad_cli.CAMERA_SIDES),
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
                    "enum": list(kicad_cli.RENDER_BACKGROUNDS),
                },
                "quality": {
                    "type": "string",
                    "enum": list(kicad_cli.RENDER_QUALITIES),
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
                "kind": {"type": "string", "enum": list(kicad_cli.DIFF_KINDS)},
                "left_path": {"type": "string"},
                "right_path": {"type": "string"},
                "output_path": {"type": "string"},
                "format": {
                    "type": "string",
                    "enum": list(kicad_cli.DIFF_FORMATS),
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
                    "enum": list(kicad_cli.EXPORT_KINDS),
                },
                "source_path": {
                    "type": "string",
                    "description": "KiCad source path; fp_svg expects a footprint "
                    "library directory and sym_svg expects a symbol library file",
                },
                "symbol_name": {"type": "string"},
                "output_dir": {"type": "string"},
            },
            "required": ["kind", "source_path", "output_dir"],
        },
    ),
    (
        "circuit_import",
        "Import a non-KiCad schematic or PCB (Altium/Eagle/CADSTAR/EasyEDA(+Pro)/"
        "LTspice/PADS/DipTrace/PCAD/OrCAD schematics; PADS/Altium/Eagle/CADSTAR/"
        "Fabmaster/PCAD/SolidWorks boards); writes the converted KiCad file plus "
        "the importer's JSON report next to it as <output>.import.json",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(kicad_cli.IMPORT_KINDS)},
                "source_path": {"type": "string"},
                "output_path": {"type": "string"},
                "format": {
                    "type": "string",
                    "description": "input format hint; default auto",
                    "default": "auto",
                },
            },
            "required": ["kind", "source_path", "output_path"],
        },
    ),
    (
        "circuit_stackup",
        "Export the board stackup and write a deterministic section-diagram SVG "
        "plus the stackup JSON into output_dir",
        {
            "type": "object",
            "properties": {
                "board_path": {"type": "string"},
                "output_dir": {"type": "string"},
            },
            "required": ["board_path", "output_dir"],
        },
    ),
    (
        "circuit_rasterize",
        "Rasterize a .pdf (pdftoppm, one PNG per page) or .svg (rsvg-convert) "
        "intake file to PNG for visual review; attaches up to 4 images inline",
        {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "output_dir": {"type": "string"},
                "dpi": {"type": "integer", "default": 150},
            },
            "required": ["source_path", "output_dir"],
        },
    ),
    (
        "circuit_datasheet_extract",
        "Extract PDF datasheets through Poppler, pdfplumber, and OCR as needed",
        {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string"},
                "output_dir": {"type": "string"},
                "pages": {"type": "array", "items": {"type": "integer"}},
                "dpi": {"type": "integer", "default": 300},
            },
            "required": ["pdf_path"],
        },
    ),
    (
        "circuit_vision_read",
        "Create datasheet image crops for visual reading; every image must receive an answer "
        "and a multi-sentence impression describing appearance, legibility, ambiguity, and "
        "anything surprising.",
        {
            "type": "object",
            "properties": {
                "extraction_path": {"type": "string"},
                "requests": {"type": "array", "items": {"type": "object"}},
                "out_dir": {"type": "string"},
            },
            "required": ["extraction_path", "requests"],
        },
    ),
    (
        "circuit_vision_compare",
        "Build a hash-bound datasheet/KiCad comparison panel. Every image requires an answer "
        "and a multi-sentence impression describing appearance, legibility, ambiguity, and "
        "anything surprising.",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["compare_footprint", "compare_symbol"],
                },
                "symbol_lib_path": {"type": "string"},
                "symbol_name": {"type": "string"},
                "footprint_path": {"type": "string"},
                "density": {
                    "type": "string",
                    "enum": ["most", "nominal", "least"],
                    "default": "nominal",
                },
                "out_dir": {"type": "string"},
            },
            "required": [
                "part_spec_path",
                "kind",
                "symbol_lib_path",
                "symbol_name",
                "footprint_path",
            ],
        },
    ),
    (
        "circuit_vision_answer",
        "Record answers to a tool-managed vision-read batch",
        {
            "type": "object",
            "properties": {
                "batch_path": {"type": "string"},
                "answers": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "answer": {"type": "string"},
                            "impression": {
                                "type": "string",
                                "minLength": advisory.IMPRESSION_MIN_LENGTH,
                            },
                        },
                        "required": ["answer", "impression"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["batch_path", "answers"],
        },
    ),
    (
        "circuit_part_author_commit",
        "Seal this lane's PartSpec with a required overall datasheet impression.",
        {
            "type": "object",
            "properties": {
                "run_dir": {"type": "string"},
                "part_spec_path": {"type": "string"},
                "impression": {
                    "type": "string",
                    "minLength": advisory.IMPRESSION_MIN_LENGTH,
                },
            },
            "required": ["run_dir", "part_spec_path", "impression"],
        },
    ),
    (
        "circuit_part_author_compare",
        "Re-derive and reveal both sealed authoring lanes; unavailable to lane authors.",
        {
            "type": "object",
            "properties": {"run_dir": {"type": "string"}},
            "required": ["run_dir"],
        },
    ),
    (
        "circuit_part_spec_check",
        "Cross-check an authored PartSpec against datasheet extraction and evidence",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["part_spec_path"],
        },
    ),
    (
        "circuit_land_pattern",
        "Compute a land pattern using an optional project rule profile",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "library_dir": {"type": "string"},
                "rule_profile": {"type": "string"},
                "density": {
                    "type": "string",
                    "enum": ["most", "nominal", "least"],
                    "default": "nominal",
                },
                "output_path": {"type": "string"},
            },
            "required": ["part_spec_path"],
        },
    ),
    (
        "circuit_library_candidates",
        "Search installed and project libraries for reusable items, including "
        "product-tuned candidates",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "product": {
                    "type": "string",
                    "description": (
                        "Product name for preferring valid product-layer tuned footprints"
                    ),
                },
                "density": {
                    "type": "string",
                    "enum": ["most", "nominal", "least"],
                    "default": "nominal",
                },
                "output_path": {"type": "string"},
            },
            "required": ["part_spec_path"],
        },
    ),
    (
        "circuit_library_import",
        "Import KiCad library items with immutable source copies and provenance",
        {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "library_dir": {"type": "string"},
                "nickname": {"type": "string"},
                "origin": {
                    "type": "string",
                    "enum": [
                        "manufacturer",
                        "kicad_official",
                        "cern",
                        "third_party",
                        "generated",
                        "derived",
                    ],
                },
                "vendor": {"type": "string"},
                "url": {"type": "string"},
                "retrieved_at": {"type": "string", "format": "date-time"},
                "license": {
                    "type": "object",
                    "properties": {
                        "spdx": {"type": "string"},
                        "license_ref": {"type": "string"},
                        "terms_url": {"type": "string"},
                        "attribution": {"type": "string"},
                        "redistribution": {
                            "type": "string",
                            "enum": ["allowed", "project_only", "unknown"],
                        },
                    },
                    "required": ["attribution", "redistribution"],
                    "anyOf": [{"required": ["spdx"]}, {"required": ["license_ref"]}],
                },
                "symbol_names": {"type": "array", "items": {"type": "string"}},
                "members": {"type": "array", "items": {"type": "string"}},
                "replace": {"type": "boolean", "default": False},
            },
            "required": ["source_path", "library_dir", "nickname", "origin", "vendor", "license"],
        },
    ),
    (
        "circuit_library_record",
        "Record a generated or derived library artifact and its transformations",
        {
            "type": "object",
            "properties": {
                "library_dir": {"type": "string"},
                "artifact_path": {"type": "string"},
                "artifact": {
                    "type": "string",
                    "enum": ["symbol", "footprint", "model3d"],
                },
                "name": {"type": "string"},
                "transformation": {"type": "string"},
                "origin": {"type": "string", "enum": ["generated", "derived"]},
                "vendor": {"type": "string"},
                "url": {"type": "string"},
                "license": {
                    "type": "object",
                    "properties": {
                        "spdx": {"type": "string"},
                        "license_ref": {"type": "string"},
                        "terms_url": {"type": "string"},
                        "attribution": {"type": "string"},
                        "redistribution": {
                            "type": "string",
                            "enum": ["allowed", "project_only", "unknown"],
                        },
                    },
                    "required": ["attribution", "redistribution"],
                    "anyOf": [{"required": ["spdx"]}, {"required": ["license_ref"]}],
                },
                "part_spec_path": {"type": "string"},
                "derived_from": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "library_dir",
                "artifact_path",
                "artifact",
                "name",
                "transformation",
            ],
        },
    ),
    (
        "circuit_library_verify",
        "Verify an authored library part against its PartSpec, checks, and provenance",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "symbol_lib_path": {"type": "string"},
                "symbol_name": {"type": "string"},
                "footprint_path": {"type": "string"},
                "library_dir": {"type": "string"},
                "density": {
                    "type": "string",
                    "enum": ["most", "nominal", "least"],
                    "default": "nominal",
                },
                "tolerance_mm": {"type": "number", "default": 0.02},
                "model_required": {"type": "boolean", "default": True},
                "test_board": {"type": "boolean", "default": True},
                "rule_profile": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": [
                "part_spec_path",
                "symbol_lib_path",
                "symbol_name",
                "footprint_path",
            ],
        },
    ),
    (
        "circuit_library_review_packet",
        "Build a fresh, hash-bound human review packet for a library part",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "symbol_lib_path": {"type": "string"},
                "symbol_name": {"type": "string"},
                "footprint_path": {"type": "string"},
                "library_dir": {"type": "string"},
                "density": {
                    "type": "string",
                    "enum": ["most", "nominal", "least"],
                    "default": "nominal",
                },
                "tolerance_mm": {"type": "number", "default": 0.02},
                "model_required": {"type": "boolean", "default": True},
                "out_dir": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": [
                "part_spec_path",
                "symbol_lib_path",
                "symbol_name",
                "footprint_path",
                "library_dir",
            ],
        },
    ),
    (
        "circuit_library_review_status",
        "Recompute the current packet identity and report its human review state",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "symbol_lib_path": {"type": "string"},
                "symbol_name": {"type": "string"},
                "footprint_path": {"type": "string"},
                "library_dir": {"type": "string"},
                "density": {
                    "type": "string",
                    "enum": ["most", "nominal", "least"],
                    "default": "nominal",
                },
                "tolerance_mm": {"type": "number", "default": 0.02},
                "model_required": {"type": "boolean", "default": True},
                "output_path": {"type": "string"},
            },
            "required": [
                "part_spec_path",
                "symbol_lib_path",
                "symbol_name",
                "footprint_path",
                "library_dir",
            ],
        },
    ),
    (
        "circuit_library_review_apply",
        "Apply corrections from a validated reject event; never creates decision events",
        {
            "type": "object",
            "properties": {
                "part_spec_path": {"type": "string"},
                "library_dir": {"type": "string"},
                "packet_id": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
                "event_sha12": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
                "output_path": {"type": "string"},
            },
            "required": ["part_spec_path", "library_dir", "packet_id", "event_sha12"],
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


def _workspace_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _workspace_arguments_from_args(name, arguments, _TOOLS)


def _rewrite_text_block_images(text: str, image_dir: Path, counter: list[int]) -> str:
    return _mcp_konnect._rewrite_text_block_images(
        text,
        image_dir,
        counter,
        rewrite_base64_images=_rewrite_base64_images,
    )


def _anno(
    title: str, *, write: bool, destructive: bool = False, idempotent: bool = True
) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=not write,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "circuit_api_server_start": _anno("Start API server", write=True, idempotent=True),
    "circuit_api_server_status": _anno("API server status", write=False),
    "circuit_api_server_stop": _anno("Stop API server", write=True, idempotent=True),
    "circuit_brief_validate": _anno("Validate design brief", write=False),
    "circuit_brief_intake_check": _anno("Intake check", write=True),
    "circuit_brief_library_check": _anno("Library check", write=True),
    "circuit_netlist_export": _anno("Netlist export", write=True),
    "circuit_connectivity_check": _anno("Connectivity check", write=True),
    "circuit_connectivity_export": _anno("Connectivity export", write=True),
    "circuit_firmware_export": _anno("Firmware connectivity export", write=True),
    "circuit_firmware_check": _anno("Firmware pin map check", write=True),
    "circuit_doctor": _anno("Circuit doctor", write=False),
    "circuit_design_report": _anno("Design report", write=True),
    "circuit_sch_lint": _anno("Schematic lint", write=True),
    "circuit_fit_sheet": _anno("Fit sheet", write=True),
    "circuit_erc": _anno("ERC", write=True),
    "circuit_drc": _anno("DRC", write=True),
    "circuit_render": _anno("Render", write=True),
    "circuit_diff": _anno("Design diff", write=True),
    "circuit_jobset_run": _anno("Jobset run", write=True),
    "circuit_export": _anno("Export", write=True),
    "circuit_import": _anno("Import", write=True),
    "circuit_stackup": _anno("Stackup", write=True),
    "circuit_rasterize": _anno("Rasterize", write=True),
    "circuit_datasheet_extract": _anno("Datasheet extraction", write=True),
    "circuit_vision_read": _anno("Create datasheet vision reads", write=True),
    "circuit_vision_compare": _anno("Compare library art with datasheet", write=True),
    "circuit_vision_answer": _anno("Record datasheet vision answers", write=True),
    "circuit_part_author_commit": _anno("Seal a blind authoring lane", write=True),
    "circuit_part_author_compare": _anno("Compare sealed authoring lanes", write=True),
    "circuit_part_spec_check": _anno("PartSpec check", write=True),
    "circuit_land_pattern": _anno("Land pattern", write=True),
    "circuit_library_candidates": _anno("Library candidates", write=True),
    "circuit_library_import": _anno("Library import", write=True),
    "circuit_library_record": _anno("Library provenance record", write=True),
    "circuit_library_verify": _anno("Library verification", write=True),
    "circuit_library_review_packet": _anno("Library review packet", write=True),
    "circuit_library_review_status": _anno("Library review status", write=True),
    "circuit_library_review_apply": _anno("Apply review corrections", write=True),
    "circuit_konnect_call": _anno("Konnect call", write=True, destructive=True, idempotent=False),
    "circuit_kicad_version": _anno("KiCad version", write=False),
}


def tool_specs() -> list[Tool]:
    return [
        Tool(
            name=name,
            description=description,
            inputSchema=schema,
            annotations=_ANNOTATIONS[name],
        )
        for name, description, schema in _TOOLS
    ]


_MAX_VISION_IMAGES = 8


def _authoring_tool(name: str, args: dict[str, Any]) -> tuple[Any, list[Path]] | None:
    if name == "circuit_vision_read":
        raw_requests = args.get("requests")
        if not isinstance(raw_requests, list) or not all(
            isinstance(item, dict) for item in cast(list[Any], raw_requests)
        ):
            raise ValueError("circuit_vision_read requires a list of request objects")
        out_dir = Path(str(args["out_dir"])) if isinstance(args.get("out_dir"), str) else None
        batch = visionread.create_read_batch(
            Path(str(args["extraction_path"])),
            cast(list[dict[str, object]], raw_requests),
            out_dir,
            lane=os.environ.get("CIRCUIT_AUTHORING_LANE", "main"),
            profile=os.environ.get("CIRCUIT_LLM_PROFILE", ""),
            model=os.environ.get("CIRCUIT_LLM_MODEL", "unknown"),
        )
        batch_path = out_dir
        if batch_path is None:
            batch_path = Path(str(args["extraction_path"])).resolve().parent / "vision-reads"
            batch_path = batch_path / batch.batch_id
        else:
            batch_path = batch_path.resolve()
        result = {
            "batch_id": batch.batch_id,
            "batch_path": str(batch_path / "batch.json"),
            "items": [
                {
                    "read_id": item.read_id,
                    "kind": item.kind,
                    "prompt": item.prompt,
                    "image_path": str(batch_path / item.image_path),
                }
                for item in batch.items
            ],
        }
        image_paths = [batch_path / item.image_path for item in batch.items]
        return result, image_paths
    if name == "circuit_vision_compare":
        kind = _literal(
            args,
            "kind",
            ("compare_footprint", "compare_symbol"),
            context="circuit_vision_compare",
        )
        density = _literal(
            args,
            "density",
            ("most", "nominal", "least"),
            "nominal",
            context="circuit_vision_compare",
        )
        out_dir = Path(str(args["out_dir"])) if isinstance(args.get("out_dir"), str) else None
        batch = libraryvision.compare_library_item(
            Path(str(args["part_spec_path"])),
            kind=cast(Any, kind),
            symbol_lib=Path(str(args["symbol_lib_path"])),
            symbol_name=str(args["symbol_name"]),
            footprint_path=Path(str(args["footprint_path"])),
            density=cast(landpattern.Density, density),
            out_dir=out_dir,
            lane=os.environ.get("CIRCUIT_AUTHORING_LANE", "main"),
            profile=os.environ.get("CIRCUIT_LLM_PROFILE", ""),
            model=os.environ.get("CIRCUIT_LLM_MODEL", "unknown"),
        )
        batch_dir = (
            out_dir.resolve()
            if out_dir is not None
            else Path(str(args["part_spec_path"])).resolve().parent
            / "vision-reads"
            / batch.batch_id
        )
        return (
            {
                "batch_id": batch.batch_id,
                "batch_path": str(batch_dir / "batch.json"),
                "items": [
                    {
                        "read_id": item.read_id,
                        "kind": item.kind,
                        "prompt": item.prompt,
                        "bindings": item.bindings,
                        "image_path": str(batch_dir / item.image_path),
                    }
                    for item in batch.items
                ],
            },
            [batch_dir / item.image_path for item in batch.items],
        )
    if name == "circuit_vision_answer":
        raw_answers = args.get("answers")
        if not isinstance(raw_answers, dict):
            raise ValueError("circuit_vision_answer requires an answers object")
        result = visionread.record_answers(
            Path(str(args["batch_path"])),
            cast(
                dict[str, str | visionread.VisionAnswerInput | dict[str, object]],
                raw_answers,
            ),
        )
        return result, []
    if name == "circuit_part_author_commit":
        result = authoring.commit_lane(
            Path(str(args["run_dir"])),
            Path(str(args["part_spec_path"])),
            lane=os.environ.get("CIRCUIT_AUTHORING_LANE"),
            profile=os.environ.get("CIRCUIT_LLM_PROFILE", ""),
            model=os.environ.get("CIRCUIT_LLM_MODEL", "unknown"),
            impression=_required_string(args, "impression", "circuit_part_author_commit"),
        )
        return result, []
    if name == "circuit_part_author_compare":
        if os.environ.get("CIRCUIT_AUTHORING_LANE") in {"a", "b"}:
            raise ValueError("author lanes cannot reveal authoring comparisons")
        run_dir = Path(str(args["run_dir"]))
        result = authoring.compare_runs(run_dir)
        authoring.write_comparison(run_dir, result)
        return result, []
    return None


@server.list_tools()
async def list_tools() -> list[Tool]:
    return tool_specs()


async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    image_paths: list[Path] = []
    result: Any = None
    try:
        args = _workspace_arguments(name, arguments or {})
        authoring_result = _authoring_tool(name, args)
        if authoring_result is not None:
            result, image_paths = authoring_result
        elif name == "circuit_api_server_start":
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
            connectivity_report = netlist.check_connectivity(
                brief.load_brief(brief_path),
                netlist.parse_netlist(netlist_path),
                brief_path=brief_path,
                netlist_path=netlist_path,
            )
            output = _output_path(source, args.get("output_path"), "connectivity")
            output.write_text(connectivity_report.model_dump_json(indent=2), encoding="utf-8")
            result = connectivity_report
        elif name == "circuit_connectivity_export":
            brief_path = Path(str(args["brief_path"]))
            design = brief.load_brief(brief_path)
            parsed_netlist = (
                netlist.parse_netlist(Path(str(args["netlist_path"])))
                if args.get("netlist_path")
                else None
            )
            output = _output_path(
                brief_path,
                _optional_string(args.get("output_path")),
                "connectivity-source",
            )
            connectivity.write_connectivity(design, output, parsed_netlist)
            payload = connectivity.connectivity_source(design, parsed_netlist)
            result = connectivity.connectivity_result(design, payload, str(output))
        elif name in ("circuit_firmware_export", "circuit_firmware_check"):
            brief_path = Path(str(args["brief_path"]))
            netlist_arg = _optional_string(args.get("netlist_path"))
            netlist_file = Path(netlist_arg) if netlist_arg else None
            firmware_payload = firmware.firmware_connectivity(
                brief.load_brief(brief_path),
                brief_path,
                netlist.parse_netlist(netlist_file) if netlist_file else None,
                netlist_file,
            )
            if name == "circuit_firmware_export":
                output = _output_path(
                    brief_path, _optional_string(args.get("output_path")), "firmware"
                )
                firmware.write_firmware_connectivity(firmware_payload, output)
                result = {
                    "verdict": "pass",
                    "design": firmware_payload.design,
                    "source": firmware_payload.source,
                    "mcus": [m.ref for m in firmware_payload.mcus],
                    "out": str(output),
                }
            else:
                pinmap_path = Path(str(args["pinmap_path"]))
                firmware_report = firmware.check_firmware_pinmap(
                    firmware_payload,
                    firmware.load_pinmap(pinmap_path),
                    firmware.sha256_file(pinmap_path),
                )
                output = _output_path(
                    brief_path, _optional_string(args.get("output_path")), "firmware-check"
                )
                output.write_text(firmware_report.model_dump_json(indent=2), encoding="utf-8")
                result = firmware_report
        elif name == "circuit_doctor":
            checks = doctor.checks()
            result = {
                "status": "ok" if all(item["status"] != "fail" for item in checks) else "fail",
                "checks": checks,
            }
        elif name == "circuit_design_report":
            brief_path = Path(str(args["brief_path"]))
            schematic = Path(str(args["schematic_path"]))
            board = Path(str(args["board_path"]))
            reports = schematic.parent / "circuit-reports"
            connectivity_path = reports / f"{schematic.stem}.connectivity.json"
            sch_lint_path = reports / f"{schematic.stem}.sch_lint.json"
            erc_path = reports / f"{schematic.stem}.erc.json"
            drc_path = reports / f"{board.stem}.drc.json"
            connectivity_report = (
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
                connectivity=connectivity_report,
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
        elif name == "circuit_fit_sheet":
            source = Path(str(args["schematic_path"]))
            margin_arg = args.get("margin")
            moves = fit_sheet.clamp_labels(
                source,
                margin=float(margin_arg) if margin_arg is not None else fit_sheet.EDGE_MARGIN_MM,
            )
            result = {"clamped": len(moves), "items": moves}
        elif name == "circuit_erc":
            source = Path(str(args["schematic_path"]))
            result = kicad_cli.erc(source, _output_path(source, args.get("output_path"), "erc"))
        elif name == "circuit_drc":
            source = Path(str(args["board_path"]))
            result = kicad_cli.drc(source, _output_path(source, args.get("output_path"), "drc"))
        elif name == "circuit_render":
            render_kind = str(args.get("kind", "board3d"))
            if render_kind == "board3d":
                zoom_arg = args.get("zoom")
                result = str(
                    kicad_cli.render(
                        Path(_required_string(args, "board_path", "kind 'board3d'")),
                        Path(_required_string(args, "output_path", "kind 'board3d'")),
                        side=_literal(
                            args, "side", kicad_cli.CAMERA_SIDES, "top", context="circuit_render"
                        ),
                        width=int(args.get("width", 1280)),
                        height=int(args.get("height", 720)),
                        rotate=_optional_string(args.get("rotate")),
                        zoom=float(cast(float, zoom_arg)) if zoom_arg is not None else None,
                        pan=_optional_string(args.get("pan")),
                        pivot=_optional_string(args.get("pivot")),
                        perspective=bool(args.get("perspective", False)),
                        floor=bool(args.get("floor", False)),
                        background=_optional_literal(
                            args,
                            "background",
                            kicad_cli.RENDER_BACKGROUNDS,
                            context="circuit_render",
                        ),
                        quality=_optional_literal(
                            args, "quality", kicad_cli.RENDER_QUALITIES, context="circuit_render"
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
            diff_format: kicad_cli.DiffFormat = _literal(
                args, "format", kicad_cli.DIFF_FORMATS, "json", context="circuit_diff"
            )
            result = kicad_cli.diff(
                _literal(args, "kind", kicad_cli.DIFF_KINDS, context="circuit_diff"),
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
            export_kind = cast(
                kicad_cli.ExportKind,
                _literal(args, "kind", kicad_cli.EXPORT_KINDS, context="circuit_export"),
            )
            export_source = Path(str(args["source_path"]))
            export_output = Path(str(args["output_dir"]))
            symbol_name = (
                str(args["symbol_name"]) if isinstance(args.get("symbol_name"), str) else None
            )
            if export_kind == "sym_svg":
                result = kicad_cli.export(
                    export_kind,
                    export_source,
                    export_output,
                    symbol_name=symbol_name,
                )
            else:
                result = kicad_cli.export(export_kind, export_source, export_output)
        elif name == "circuit_import":
            result = kicad_cli.import_file(
                _literal(args, "kind", kicad_cli.IMPORT_KINDS, context="circuit_import"),
                Path(str(args["source_path"])),
                Path(str(args["output_path"])),
                format=str(args.get("format", "auto")),
            )
        elif name == "circuit_stackup":
            board = Path(str(args["board_path"]))
            out_dir = Path(str(args["output_dir"]))
            json_path = out_dir / f"{board.stem}-stackup.json"
            data = kicad_cli.export_stackup(board, json_path)
            svg_path = stackup.write_stackup_diagram(data, out_dir / f"{board.stem}-stackup.svg")
            result = {
                "json_path": str(json_path),
                "svg_path": str(svg_path),
            }
        elif name == "circuit_rasterize":
            images = raster.rasterize(
                Path(str(args["source_path"])),
                Path(str(args["output_dir"])),
                dpi=int(args.get("dpi", 150)),
            )
            result = {
                "output_dir": str(Path(str(args["output_dir"]))),
                "images": [str(path) for path in images],
            }
            image_paths = images
        elif name == "circuit_datasheet_extract":
            source = Path(str(args["pdf_path"]))
            output = args.get("output_dir")
            if output is None:
                digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
                output_dir = source.parent / f"datasheet-{digest}"
            else:
                output_dir = Path(str(output))
            requested_pages = args.get("pages")
            extraction = datasheet.extract_datasheet(
                source,
                output_dir,
                pages=cast(list[int], requested_pages)
                if isinstance(requested_pages, list)
                else None,
                dpi=int(args.get("dpi", 300)),
            )
            result = {
                **extraction.model_dump(mode="json"),
                "extraction_path": str(output_dir / "extraction.json"),
            }
        elif name == "circuit_part_spec_check":
            spec_path = Path(str(args["part_spec_path"]))
            spec = partspec.load_part_spec(spec_path)
            extraction_path = Path(spec.datasheet.extraction_path)
            if not extraction_path.is_absolute():
                extraction_path = spec_path.resolve().parent / extraction_path
            extraction = datasheet.load_extraction(extraction_path)
            result = partspec.check_part_spec(
                spec,
                extraction,
                spec_path=spec_path,
                extraction_path=extraction_path,
            )
            output = _output_path(
                spec_path,
                _optional_string(args.get("output_path")),
                "part-spec",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_land_pattern":
            spec_path = Path(str(args["part_spec_path"]))
            spec = partspec.load_part_spec(spec_path)
            library_dir_value = args.get("library_dir")
            library_dir = (
                Path(str(library_dir_value))
                if isinstance(library_dir_value, str)
                else spec_path.parent / "library"
            )
            rule_profile = _optional_string(args.get("rule_profile"))
            rules = (
                ruleprofile.load_rules(rule_profile, library_dir / "rules")
                if rule_profile is not None
                else None
            )
            density = cast(
                landpattern.Density,
                _literal(
                    args,
                    "density",
                    ("most", "nominal", "least"),
                    rules.density if rules is not None else "nominal",
                    context="circuit_land_pattern",
                ),
            )
            result = landpattern.compute_land_pattern(spec, density, rules=rules)
            output = _output_path(
                spec_path,
                _optional_string(args.get("output_path")),
                "land-pattern",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_library_candidates":
            spec_path = Path(str(args["part_spec_path"]))
            spec = partspec.load_part_spec(spec_path)
            density = cast(
                landpattern.Density,
                _literal(
                    args,
                    "density",
                    ("most", "nominal", "least"),
                    "nominal",
                    context="circuit_library_candidates",
                ),
            )
            reference = landpattern.compute_land_pattern(spec, density)
            result = libreuse.find_candidates(
                spec,
                roots=libraries.default_roots(),
                project_library_dir=spec_path.parent / "library",
                reference=reference,
                product=_optional_string(args.get("product")),
            )
            output = _output_path(
                spec_path,
                _optional_string(args.get("output_path")),
                "library-candidates",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_library_import":
            source_path = Path(str(args["source_path"]))
            library_dir = Path(str(args["library_dir"]))
            license_data = args.get("license")
            if not isinstance(license_data, dict):
                raise ValueError("circuit_library_import requires a license object")
            import_source = libsource.SourceInfoInput.model_validate(
                {
                    "origin": args.get("origin"),
                    "vendor": args.get("vendor"),
                    "url": args.get("url"),
                    "retrieved_at": args.get("retrieved_at"),
                    "license": license_data,
                }
            )
            symbol_names = args.get("symbol_names")
            members = args.get("members")
            result = libsource.import_library_item(
                source_path,
                library_dir,
                str(args["nickname"]),
                source=import_source,
                symbol_names=cast(list[str], symbol_names)
                if isinstance(symbol_names, list)
                else None,
                members=cast(list[str], members) if isinstance(members, list) else None,
                replace=bool(args.get("replace", False)),
            )
            output = _output_path(
                library_dir.parent / f"{args['nickname']}.json",
                None,
                "library-import",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_library_record":
            library_dir = Path(str(args["library_dir"]))
            artifact_path = Path(str(args["artifact_path"]))
            origin = args.get("origin")
            source_info: libsource.SourceInfo | None = None
            source_fields = ("origin", "vendor", "url", "license")
            if any(args.get(field) is not None for field in source_fields):
                if origin not in ("generated", "derived"):
                    raise ValueError(
                        "source metadata for a new library record requires origin "
                        "'generated' or 'derived'"
                    )
                license_data = args.get("license")
                if not isinstance(license_data, dict):
                    raise ValueError("source metadata requires a license object")
                source_input = libsource.SourceInfoInput.model_validate(
                    {
                        "origin": origin,
                        "vendor": args.get("vendor"),
                        "url": args.get("url"),
                        "license": license_data,
                    }
                )
                resolved_artifact = artifact_path.resolve(strict=True)
                relative_artifact = resolved_artifact.relative_to(library_dir.resolve())
                source_info = libsource.SourceInfo(
                    **source_input.model_dump(),
                    original_path=relative_artifact.as_posix(),
                    original_sha256=hashlib.sha256(resolved_artifact.read_bytes()).hexdigest(),
                )
            part_spec_path = args.get("part_spec_path")
            result = libsource.record_library_item(
                library_dir,
                artifact_path,
                artifact=_literal(
                    args,
                    "artifact",
                    ("symbol", "footprint", "model3d"),
                    context="circuit_library_record",
                ),
                name=str(args["name"]),
                source=source_info,
                transformation=str(args["transformation"]),
                part_spec_sha256=partspec.part_spec_sha256(Path(str(part_spec_path)))
                if isinstance(part_spec_path, str)
                else None,
                derived_from=cast(list[str], args.get("derived_from", [])),
            )
            output = _output_path(
                library_dir.parent / "library.json",
                None,
                "library-record",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_library_verify":
            spec_path = Path(str(args["part_spec_path"]))
            spec = partspec.load_part_spec(spec_path)
            library_dir_value = args.get("library_dir")
            library_dir = (
                Path(str(library_dir_value)) if isinstance(library_dir_value, str) else None
            )
            rule_profile = _optional_string(args.get("rule_profile"))
            rules_dir = (
                library_dir / "rules"
                if library_dir is not None
                else spec_path.parent / "library" / "rules"
            )
            rules = (
                ruleprofile.load_rules(rule_profile, rules_dir)
                if rule_profile is not None
                else None
            )
            density = cast(
                landpattern.Density,
                _literal(
                    args,
                    "density",
                    ("most", "nominal", "least"),
                    rules.density if rules is not None else "nominal",
                    context="circuit_library_verify",
                ),
            )
            output = _output_path(
                spec_path,
                _optional_string(args.get("output_path")),
                "library-verification",
            )
            result = libverify.verify_library_part(
                spec,
                spec_path=spec_path,
                symbol_lib=Path(str(args["symbol_lib_path"])),
                symbol_name=str(args["symbol_name"]),
                footprint_path=Path(str(args["footprint_path"])),
                library_dir=library_dir,
                reference=landpattern.compute_land_pattern(spec, density, rules=rules),
                rules=rules,
                tolerance_mm=float(args.get("tolerance_mm", 0.02)),
                model_required=bool(args.get("model_required", True)),
                test_board=bool(args.get("test_board", True)),
                output_path=output,
            )
        elif name == "circuit_library_review_packet":
            spec_path = Path(str(args["part_spec_path"]))
            library_dir = Path(str(args["library_dir"]))
            density = cast(
                landpattern.Density,
                _literal(
                    args,
                    "density",
                    ("most", "nominal", "least"),
                    "nominal",
                    context="circuit_library_review_packet",
                ),
            )
            output_dir = Path(str(args.get("out_dir") or (library_dir / "reviews")))
            result = libreview.build_review_packet(
                spec_path,
                symbol_lib=Path(str(args["symbol_lib_path"])),
                symbol_name=str(args["symbol_name"]),
                footprint_path=Path(str(args["footprint_path"])),
                library_dir=library_dir,
                density=density,
                tolerance_mm=float(args.get("tolerance_mm", 0.02)),
                model_required=bool(args.get("model_required", True)),
                out_dir=output_dir,
            )
            output = _output_path(
                spec_path,
                _optional_string(args.get("output_path")),
                "library-review-packet",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_library_review_status":
            spec_path = Path(str(args["part_spec_path"]))
            library_dir = Path(str(args["library_dir"]))
            density = cast(
                landpattern.Density,
                _literal(
                    args,
                    "density",
                    ("most", "nominal", "least"),
                    "nominal",
                    context="circuit_library_review_status",
                ),
            )
            symbol_lib = Path(str(args["symbol_lib_path"]))
            symbol_name = str(args["symbol_name"])
            footprint_path = Path(str(args["footprint_path"]))
            tolerance_mm = float(args.get("tolerance_mm", 0.02))
            model_required = bool(args.get("model_required", True))
            current_id = libreview.current_packet_id(
                spec_path,
                symbol_lib=symbol_lib,
                symbol_name=symbol_name,
                footprint_path=footprint_path,
                library_dir=library_dir,
                density=density,
                tolerance_mm=tolerance_mm,
                model_required=model_required,
            )
            result = libreview.review_status(
                library_dir,
                partspec.load_part_spec(spec_path),
                current_id,
                spec_path=spec_path,
            )
            output = _output_path(
                spec_path,
                _optional_string(args.get("output_path")),
                "library-review-status",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_library_review_apply":
            spec_path = Path(str(args["part_spec_path"]))
            library_dir = Path(str(args["library_dir"]))
            packet_id = str(args["packet_id"])
            event_sha12 = str(args["event_sha12"])
            if len(event_sha12) != 12 or any(
                character not in "0123456789abcdef" for character in event_sha12
            ):
                raise ValueError("event_sha12 must be 12 lowercase hexadecimal characters")
            decisions = libreview.load_decisions(library_dir, packet_id)
            if any(not item.valid for item in decisions):
                result = libreview.CorrectionResult(
                    artifact_kind="circuit_library_review_correction",
                    applied=False,
                    packet_id=packet_id,
                    applied_pointers=[],
                    reasons=["decision_invalid"],
                )
            else:
                rejects = [
                    item
                    for item in decisions
                    if item.decision == "reject"
                    and item.corrections
                    and item.event_sha256 is not None
                    and item.event_sha256.startswith(event_sha12)
                ]
                decision = rejects[0] if len(rejects) == 1 else None
                result = (
                    libreview.apply_corrections(spec_path, decision)
                    if decision is not None
                    else libreview.CorrectionResult(
                        artifact_kind="circuit_library_review_correction",
                        applied=False,
                        packet_id=packet_id,
                        applied_pointers=[],
                        reasons=["matching_valid_reject_with_corrections_missing"],
                    )
                )
            output = _output_path(
                spec_path,
                _optional_string(args.get("output_path")),
                "library-review-apply",
            )
            output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        elif name == "circuit_konnect_call":
            konnect_arguments = args.get("arguments")
            ops = args.get("ops")
            socket_url = _socket_url(args["socket"]) if "socket" in args else None
            if ops is not None:
                if not isinstance(ops, list) or not all(
                    isinstance(item, dict) for item in cast(list[Any], ops)
                ):
                    raise ValueError("circuit_konnect_call 'ops' must be a list of objects")
                return await _konnect_call(
                    "",
                    {},
                    socket_url,
                    ops=cast(list[dict[str, Any]], ops),
                    rewrite_text_block_images=_rewrite_text_block_images,
                    rewrite_base64_images=_rewrite_base64_images,
                )
            tool = args.get("tool")
            if not isinstance(tool, str) or not tool:
                raise ValueError("circuit_konnect_call requires 'tool' or 'ops'")
            return await _konnect_call(
                tool,
                cast(dict[str, Any], konnect_arguments)
                if isinstance(konnect_arguments, dict)
                else {},
                socket_url,
                rewrite_text_block_images=_rewrite_text_block_images,
                rewrite_base64_images=_rewrite_base64_images,
            )
        elif name == "circuit_kicad_version":
            result = kicad_cli.version()
        else:
            raise ValueError(f"unknown tool: {name}")
        value = result.model_dump() if isinstance(result, BaseModel) else result
        content: list[ContentBlock] = [TextContent(type="text", text=_json(value))]
        image_limit = (
            _MAX_VISION_IMAGES
            if name in {"circuit_vision_read", "circuit_vision_compare"}
            else _MAX_INLINE_IMAGES
        )
        for image_path in image_paths[:image_limit]:
            image = _image_content(image_path)
            if image is not None:
                content.append(image)
        return CallToolResult(content=content)
    except Exception as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=_json({"error": str(exc)}))],
            isError=True,
        )


__all__ = ["call_tool", "server"]

server.call_tool()(call_tool)


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
