import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent

from circuit import mcp_server
from circuit.advisory import AdvisoryResult
from circuit.kicad_cli import DiffReport, JobsetResult
from circuit.netlist import ConnectivityReport
from circuit.report import DesignReport
from circuit.sch_lint import SchLintReport


def test_mcp_server_lists_expected_tools() -> None:
    names = {name for name, _, _ in mcp_server._TOOLS}  # pyright: ignore[reportPrivateUsage]
    assert names == {
        "circuit_api_server_start",
        "circuit_api_server_status",
        "circuit_api_server_stop",
        "circuit_brief_validate",
        "circuit_brief_intake_check",
        "circuit_brief_library_check",
        "circuit_netlist_export",
        "circuit_connectivity_check",
        "circuit_design_report",
        "circuit_erc",
        "circuit_drc",
        "circuit_render",
        "circuit_diff",
        "circuit_jobset_run",
        "circuit_export",
        "circuit_konnect_call",
        "circuit_kicad_version",
        "circuit_sch_lint",
    }


def test_output_path_defaults_to_report_directory(tmp_path: Path) -> None:
    path = mcp_server._output_path(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "board.kicad_pcb", None, "drc"
    )
    assert path == tmp_path / "circuit-reports" / "board.drc.json"


def test_stdio_server_lists_tools_and_reports_version(tmp_path: Path) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "10.99.0"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    async def exercise() -> None:
        env = {**os.environ, "CIRCUIT_KICAD_CLI": str(fake)}
        params = StdioServerParameters(
            command="python3",
            args=["-m", "circuit.mcp_server"],
            env=env,
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 18
            result = await session.call_tool("circuit_kicad_version", {})
            assert result.isError is False
            content = result.content[0]
            assert isinstance(content, TextContent)
            assert content.text == '"10.99.0"'

    asyncio.run(exercise())


def test_brief_validate_tool(tmp_path: Path) -> None:
    brief_path = Path(__file__).parent / "data" / "brief_led_loop.json"

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool("circuit_brief_validate", {"brief_path": str(brief_path)}),
        )
        assert result.isError is False
        assert '"brief_sha256"' in result.content[0].text

    asyncio.run(exercise())


def test_render_result_includes_image_content(tmp_path: Path, monkeypatch: Any) -> None:
    # Smallest valid PNG (1x1 transparent pixel).
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
        "454e44ae426082"
    )
    out_path = tmp_path / "render.png"
    out_path.write_bytes(png_bytes)

    def fake_render(
        pcb: Path,
        out: Path,
        *,
        side: str,
        width: int = 1280,
        height: int = 720,
    ) -> Path:
        return out_path

    monkeypatch.setattr(mcp_server.kicad_cli, "render", fake_render)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "board_path": str(tmp_path / "board.kicad_pcb"),
                    "output_path": str(out_path),
                    "side": "top",
                },
            ),
        )
        assert result.isError is False
        assert isinstance(result.content[0], TextContent)
        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert image.mimeType == "image/png"
        assert base64.b64decode(image.data) == png_bytes

    asyncio.run(exercise())


def test_render_result_text_only_when_png_missing(tmp_path: Path, monkeypatch: Any) -> None:
    def fake_render(
        pcb: Path,
        out: Path,
        *,
        side: str,
        width: int = 1280,
        height: int = 720,
    ) -> Path:
        return tmp_path / "render.png"

    monkeypatch.setattr(mcp_server.kicad_cli, "render", fake_render)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "board_path": str(tmp_path / "board.kicad_pcb"),
                    "output_path": str(tmp_path / "render.png"),
                    "side": "top",
                },
            ),
        )
        assert result.isError is False
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)

    asyncio.run(exercise())


def test_konnect_call_proxies_to_managed_subprocess(tmp_path: Path, monkeypatch: Any) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "10.99.0"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("CIRCUIT_KICAD_CLI", str(fake))
    monkeypatch.setenv("CIRCUIT_KONNECT", "python3 -m circuit.mcp_server")

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {"tool": "circuit_kicad_version"},
            ),
        )
        assert result.isError is False
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "10.99.0" in content.text

    asyncio.run(exercise())


def _write_gate_reports(project: Path) -> Path:
    reports = project / "circuit-reports"
    reports.mkdir(parents=True)
    brief_path = Path(__file__).parent / "data" / "brief_led_loop.json"
    connectivity = ConnectivityReport(
        brief_path=brief_path,
        netlist_path=project / "circuit-reports" / "board.net",
        brief_sha256="0" * 64,
        expected={"VIN": ["J1.1", "R1.1"]},
        actual={"VIN": ["J1.1", "R1.1"]},
        missing_nets=[],
        mismatched_nets={},
        unexpected_nets=[],
        missing_parts=[],
        footprint_mismatches={},
        verdict="pass",
    )
    (reports / "board.connectivity.json").write_text(
        connectivity.model_dump_json(), encoding="utf-8"
    )
    sch_lint_report = SchLintReport(
        source=project / "board.kicad_sch",
        verdict="pass",
        errors=0,
        warnings=0,
        symbols_checked=1,
    )
    (reports / "board.sch_lint.json").write_text(
        sch_lint_report.model_dump_json(), encoding="utf-8"
    )
    (reports / "board.erc.json").write_text(
        json.dumps(
            {
                "$schema": "https://schemas.kicad.org/erc.v1.json",
                "kicad_version": "11.0",
                "sheets": [{"path": "/", "violations": []}],
            }
        ),
        encoding="utf-8",
    )
    (reports / "board.drc.json").write_text(
        json.dumps(
            {
                "$schema": "https://schemas.kicad.org/drc.v1.json",
                "kicad_version": "11.0",
                "unconnected_items": [],
                "violations": [],
                "schematic_parity": [],
            }
        ),
        encoding="utf-8",
    )
    return reports


def test_design_report_collects_pipeline_sections(tmp_path: Path) -> None:
    brief_path = Path(__file__).parent / "data" / "brief_led_loop.json"
    project = tmp_path
    (project / "board.kicad_sch").write_text("()", encoding="utf-8")
    (project / "board.kicad_pcb").write_text("()", encoding="utf-8")
    reports = _write_gate_reports(project)

    gerber = project / "exports" / "gerbers" / "board-F_Cu.gtl"
    gerber.parent.mkdir(parents=True)
    gerber.write_text("gerber", encoding="utf-8")
    (reports / "render-top.png").write_bytes(b"\x89PNG")
    advisory = AdvisoryResult(
        tool="run_erc",
        stage="schematic",
        status="ok",
        summary="konnect erc clean",
    )
    (reports / "erc.advisory.json").write_text(advisory.model_dump_json(), encoding="utf-8")
    (reports / "advisory.jsonl").write_text(advisory.model_dump_json() + "\n", encoding="utf-8")
    diff = DiffReport(
        kind="pcb",
        left=project / "board.kicad_pcb",
        right=project / "board.kicad_pcb",
        identical=True,
        exit_code=0,
        output=reports / "board.pcb.diff.json",
        changes=[],
    )
    (reports / "board.pcb.diff.json").write_text(diff.model_dump_json(), encoding="utf-8")
    jobset_erc = reports / "jobset-erc.json"
    jobset_erc.write_text(
        (reports / "board.erc.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    jobset = JobsetResult(
        jobset=Path("default.kicad_jobset"),
        project=project / "board.kicad_pro",
        output_dir=project / "jobset",
        exit_code=0,
        outputs=[jobset_erc],
        erc_report=jobset_erc,
        drc_report=None,
    )
    (reports / "board.jobset.json").write_text(jobset.model_dump_json(), encoding="utf-8")

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_design_report",
                {
                    "brief_path": str(brief_path),
                    "schematic_path": str(project / "board.kicad_sch"),
                    "board_path": str(project / "board.kicad_pcb"),
                },
            ),
        )
        assert result.isError is False
        report_path = reports / "board.design-report.json"
        assert report_path.is_file()
        design = DesignReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        assert design.verdict == "pass"
        assert design.connectivity is not None
        assert design.sch_lint is not None
        assert design.erc is not None and design.erc.verdict == "pass"
        assert design.drc is not None and design.drc.verdict == "pass"
        assert design.exports == {"gerbers": [str(gerber)]}
        assert [str(reports / "render-top.png")] == design.renders
        assert len(design.advisory) == 2
        assert design.jobset is not None
        assert design.jobset_consistent is True
        assert "board.pcb" in design.diffs
        assert design.diffs["board.pcb"].identical is True

    asyncio.run(exercise())
