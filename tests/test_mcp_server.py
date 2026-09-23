import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
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
        "circuit_import",
        "circuit_stackup",
        "circuit_rasterize",
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
            assert len(tools.tools) == 21
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
        **_: object,
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
        **_: object,
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


def test_render_schematic_kind_attaches_images(tmp_path: Path, monkeypatch: Any) -> None:
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
        "454e44ae426082"
    )
    out_dir = tmp_path / "sch-png"
    produced = [out_dir / f"board-{page}.png" for page in (1, 2)]
    for path in produced:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes)

    def fake_render_schematic(sch: Path, out: Path, **_: object) -> list[Path]:
        return produced

    monkeypatch.setattr(mcp_server.kicad_cli, "render_schematic", fake_render_schematic)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "kind": "schematic",
                    "schematic_path": str(tmp_path / "board.kicad_sch"),
                    "output_dir": str(out_dir),
                },
            ),
        )
        assert result.isError is False
        assert isinstance(result.content[0], TextContent)
        payload = json.loads(result.content[0].text)
        assert payload["images"] == [str(path) for path in produced]
        images = result.content[1:]
        assert len(images) == 2
        assert all(isinstance(image, ImageContent) for image in images)

    asyncio.run(exercise())


def test_render_layers_kind_attaches_capped_images(tmp_path: Path, monkeypatch: Any) -> None:
    png_bytes = b"\x89PNG" + b"0" * 32
    out_dir = tmp_path / "layers"
    out_dir.mkdir()
    produced = [out_dir / f"board-{index}.png" for index in range(6)]
    for path in produced:
        path.write_bytes(png_bytes)

    def fake_render_layers(pcb: Path, out: Path, **kwargs: object) -> list[Path]:
        assert kwargs["layers"] == "F.Cu,B.Cu"
        return produced

    monkeypatch.setattr(mcp_server.kicad_cli, "render_layers", fake_render_layers)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "kind": "layers",
                    "board_path": str(tmp_path / "board.kicad_pcb"),
                    "output_dir": str(out_dir),
                    "layers": "F.Cu,B.Cu",
                },
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert len(payload["images"]) == 6
        assert len(result.content) == 1 + 4  # inline image cap

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "kind,missing",
    [
        ("board3d", "output_path"),
        ("schematic", "schematic_path"),
        ("schematic", "output_dir"),
        ("layers", "board_path"),
        ("layers", "layers"),
    ],
)
def test_render_kind_requires_its_inputs(tmp_path: Path, kind: str, missing: str) -> None:
    async def exercise() -> None:
        args: dict[str, Any] = {
            "kind": kind,
            "board_path": str(tmp_path / "board.kicad_pcb"),
            "schematic_path": str(tmp_path / "board.kicad_sch"),
            "output_path": str(tmp_path / "out.png"),
            "output_dir": str(tmp_path / "out"),
            "layers": "F.Cu",
        }
        del args[missing]
        result = cast(Any, await mcp_server.call_tool("circuit_render", args))
        assert result.isError is True
        assert isinstance(result.content[0], TextContent)
        assert missing in result.content[0].text

    asyncio.run(exercise())


def test_render_unknown_kind_errors(tmp_path: Path) -> None:
    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {"kind": "cross_section", "board_path": str(tmp_path / "b.kicad_pcb")},
            ),
        )
        assert result.isError is True
        assert "unknown circuit_render kind" in result.content[0].text

    asyncio.run(exercise())


def test_diff_png_attaches_image(tmp_path: Path, monkeypatch: Any) -> None:
    png_bytes = b"\x89PNG" + b"0" * 32
    out_path = tmp_path / "diff.png"
    out_path.write_bytes(png_bytes)

    def fake_diff(
        kind: str, left: Path, right: Path, out: Path, *, format: str = "json"
    ) -> DiffReport:
        assert format == "png"
        return DiffReport(
            kind=cast(Any, kind),
            left=left,
            right=right,
            identical=False,
            exit_code=5,
            output=out_path,
            format="png",
        )

    monkeypatch.setattr(mcp_server.kicad_cli, "diff", fake_diff)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_diff",
                {
                    "kind": "pcb",
                    "left_path": str(tmp_path / "a.kicad_pcb"),
                    "right_path": str(tmp_path / "b.kicad_pcb"),
                    "output_path": str(out_path),
                    "format": "png",
                },
            ),
        )
        assert result.isError is False
        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert base64.b64decode(image.data) == png_bytes

    asyncio.run(exercise())


def test_rewrite_text_block_images_extracts_payload(tmp_path: Path) -> None:
    png_bytes = b"\x89PNG" + b"payload" * 200
    encoded = base64.b64encode(png_bytes).decode("ascii")
    counter = [0]
    rewritten = mcp_server._rewrite_text_block_images(  # pyright: ignore[reportPrivateUsage]
        json.dumps({"tool": "render_schematic_png", "png_base64": encoded}),
        tmp_path / "images",
        counter,
    )
    payload = json.loads(rewritten)
    entry = payload["png_base64"]
    assert Path(entry["image_path"]).read_bytes() == png_bytes
    assert entry["sha256"] == hashlib.sha256(png_bytes).hexdigest()


def test_rewrite_text_block_images_ignores_non_image_text(tmp_path: Path) -> None:
    counter = [0]
    text = json.dumps({"result": "all good", "note": "x" * 5000})
    rewritten = mcp_server._rewrite_text_block_images(  # pyright: ignore[reportPrivateUsage]
        text, tmp_path / "images", counter
    )
    assert rewritten == text
    assert (
        mcp_server._rewrite_text_block_images(  # pyright: ignore[reportPrivateUsage]
            "not json at all", tmp_path / "images", counter
        )
        == "not json at all"
    )


def test_rewrite_base64_images_handles_data_url_and_image_blocks(
    tmp_path: Path,
) -> None:
    png_bytes = b"\x89PNG" + b"payload" * 200
    encoded = base64.b64encode(png_bytes).decode("ascii")
    counter = [0]
    rewritten = mcp_server._rewrite_base64_images(  # pyright: ignore[reportPrivateUsage]
        {
            "type": "image",
            "data": encoded,
            "mimeType": "image/png",
            "nested": [{"url": f"data:image/png;base64,{encoded}"}],
        },
        tmp_path / "images",
        counter,
    )
    assert isinstance(rewritten, dict)
    rewritten_dict = cast(dict[str, Any], rewritten)
    assert str(rewritten_dict["data"]["image_path"]).endswith(".png")
    nested = cast(list[dict[str, Any]], rewritten_dict["nested"])
    assert (
        str(cast(dict[str, Any], nested[0]["url"])["sha256"])
        == hashlib.sha256(png_bytes).hexdigest()
    )
    assert counter[0] == 2


def test_konnect_call_ops_extract_image_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    png_bytes = (
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
            "454e44ae426082"
        )
        + b"pad" * 400
    )
    out_path = tmp_path / "render.png"
    out_path.write_bytes(png_bytes)

    # The managed konnect subprocess cannot inherit monkeypatches, so wrap the
    # real server in a script that stubs kicad_cli.render for the child.
    wrapper = tmp_path / "konnect_stub.py"
    wrapper.write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from circuit import mcp_server\n"
        f"PNG = {png_bytes!r}\n"
        "def fake_render(pcb, out, **_):\n"
        "    out.write_bytes(PNG)\n"
        "    return out\n"
        "mcp_server.kicad_cli.render = fake_render\n"
        "asyncio.run(mcp_server._run())\n",
        encoding="utf-8",
    )
    _fake_konnect(tmp_path, monkeypatch)
    monkeypatch.setenv("CIRCUIT_KONNECT", f"python3 {wrapper}")
    monkeypatch.setenv("CIRCUIT_KONNECT_IMAGE_DIR", str(tmp_path / "konnect-images"))

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {
                    "ops": [
                        {
                            "tool": "circuit_render",
                            "arguments": {
                                "kind": "board3d",
                                "board_path": str(tmp_path / "b.kicad_pcb"),
                                "output_path": str(out_path),
                                "side": "top",
                            },
                        }
                    ],
                },
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        blocks = cast(list[Any], payload["results"][0]["content"])
        dict_blocks = [cast(dict[str, Any], block) for block in blocks if isinstance(block, dict)]
        image_data = cast(dict[str, str], dict_blocks[0]["data"])
        assert image_data["sha256"] == hashlib.sha256(png_bytes).hexdigest()
        extracted = Path(image_data["image_path"])
        assert extracted.is_file()
        assert extracted.read_bytes() == png_bytes

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


def _fake_konnect(tmp_path: Path, monkeypatch: Any) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "10.99.0"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("CIRCUIT_KICAD_CLI", str(fake))
    monkeypatch.setenv("CIRCUIT_KONNECT", "python3 -m circuit.mcp_server")


def test_konnect_call_runs_ops_in_one_session(tmp_path: Path, monkeypatch: Any) -> None:
    _fake_konnect(tmp_path, monkeypatch)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {
                    "ops": [
                        {"tool": "circuit_kicad_version"},
                        {"tool": "circuit_kicad_version", "arguments": {}},
                    ],
                },
            ),
        )
        assert result.isError is False
        content = result.content[0]
        assert isinstance(content, TextContent)
        payload = json.loads(content.text)
        assert len(payload["results"]) == 2
        assert all("10.99.0" in entry["content"][0] for entry in payload["results"])

    asyncio.run(exercise())


def test_konnect_call_ops_propagate_errors(tmp_path: Path, monkeypatch: Any) -> None:
    _fake_konnect(tmp_path, monkeypatch)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {"ops": [{"tool": "no_such_tool"}, {"tool": "circuit_kicad_version"}]},
            ),
        )
        assert result.isError is True
        content = result.content[0]
        assert isinstance(content, TextContent)
        payload = json.loads(content.text)
        assert payload["results"][0]["isError"] is True
        assert payload["results"][1]["isError"] is False

    asyncio.run(exercise())


def test_konnect_call_requires_tool_or_ops(tmp_path: Path, monkeypatch: Any) -> None:
    _fake_konnect(tmp_path, monkeypatch)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool("circuit_konnect_call", {}),
        )
        assert result.isError is True
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "requires" in content.text

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


def test_import_dispatches_and_returns_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from circuit.kicad_cli import ImportResult

    def fake_import(kind: str, source: Path, output: Path, *, format: str) -> ImportResult:
        return ImportResult(
            kind=cast(Any, kind),
            source=source,
            output=output,
            report_path=output.with_name(output.name + ".import.json"),
            report={"source_format": "LTspice"},
        )

    monkeypatch.setattr(mcp_server.kicad_cli, "import_file", fake_import)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_import",
                {
                    "kind": "sch",
                    "source_path": str(tmp_path / "in.asc"),
                    "output_path": str(tmp_path / "out.kicad_sch"),
                    "format": "ltspice",
                },
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert payload["report"] == {"source_format": "LTspice"}
        assert payload["output"].endswith("out.kicad_sch")

    asyncio.run(exercise())


def test_stackup_writes_json_and_svg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = tmp_path / "stackup"

    def fake_stackup(board: Path, out: Path) -> dict[str, object]:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{"layers":[]}', encoding="utf-8")
        return {"layers": [{"type": "BSLT_COPPER", "enabled": True}]}

    monkeypatch.setattr(mcp_server.kicad_cli, "export_stackup", fake_stackup)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_stackup",
                {"board_path": str(tmp_path / "board.kicad_pcb"), "output_dir": str(out_dir)},
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert Path(payload["json_path"]).is_file()
        svg = Path(payload["svg_path"])
        assert svg.is_file() and svg.read_text(encoding="utf-8").startswith("<svg")

    asyncio.run(exercise())


def test_rasterize_attaches_pngs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
        "454e44ae426082"
    )
    page = tmp_path / "doc-1.png"
    page.write_bytes(png_bytes)

    def fake_rasterize(source: Path, out_dir: Path, *, dpi: int) -> list[Path]:
        return [page]

    monkeypatch.setattr(mcp_server.raster, "rasterize", fake_rasterize)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_rasterize",
                {
                    "source_path": str(tmp_path / "doc.pdf"),
                    "output_dir": str(tmp_path / "pages"),
                },
            ),
        )
        assert result.isError is False
        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert image.mimeType == "image/png"

    asyncio.run(exercise())
