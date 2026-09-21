import asyncio
import os
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from circuit import mcp_server


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
        "circuit_kicad_version",
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
            assert len(tools.tools) == 16
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
