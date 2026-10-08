"""Host tests for the schematic signal-chain layout, wiring, and readability gate."""

from __future__ import annotations

import importlib
import io
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from circuit import brief, sch_lint

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LED_LOOP_BRIEF = _REPO_ROOT / "tests" / "data" / "brief_led_loop.json"


def _module() -> Any:
    """Load e2e_authoring with the scripts dir on sys.path (sibling imports)."""
    scripts_dir = str(_REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("e2e_authoring")


def _chain_brief(count: int) -> brief.DesignBrief:
    """A synthetic brief: one connector feeding a chain of two-pin parts."""
    parts = [
        {
            "reference": "J1",
            "lib_id": "Connector:Conn_01x02",
            "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            "connector": True,
        }
    ]
    nets: list[dict[str, object]] = []
    previous = "J1.2"
    for index in range(2, count + 1):
        reference = f"R{index - 1}"
        parts.append(
            {
                "reference": reference,
                "lib_id": "Device:R",
                "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
                "value": "1k",
            }
        )
        nets.append({"name": f"N{index}", "pins": [previous, f"{reference}.1"]})
        previous = f"{reference}.2"
    nets.append({"name": "GND", "pins": [previous, "J1.1"]})
    return brief.DesignBrief.model_validate(
        {
            "name": f"chain{count}",
            "description": "synthetic chain",
            "parts": parts,
            "nets": nets,
            "board": {"width_mm": 100.0, "height_mm": 80.0},
            "drawing": {"revision": "A"},
        }
    )


def test_schematic_part_order_follows_signal_chain() -> None:
    module = _module()
    loaded_brief = brief.load_brief(_LED_LOOP_BRIEF)
    assert module._schematic_part_order(loaded_brief) == ["J1", "R1", "D1"]


def test_schematic_layout_ignores_board_placements() -> None:
    module = _module()
    loaded_brief = brief.load_brief(_LED_LOOP_BRIEF)
    paper = "A5"
    positions = module._schematic_layout(loaded_brief, paper)
    board_xy = {
        reference: (placement.x_mm, placement.y_mm)
        for reference, placement in loaded_brief.board.placements.items()
    }
    assert set(positions) == {"J1", "R1", "D1"}
    assert len(set(positions.values())) == len(positions)
    for reference, position in positions.items():
        assert position != board_xy[reference]
        x, y = position
        width, height = module.titleblock.PAPER_SIZES[paper]
        assert 0 < x < width
        assert 0 < y < height


def test_schematic_layout_spread_passes_sheet_usage() -> None:
    module = _module()
    for count in range(2, 9):
        loaded_brief = _chain_brief(count)
        paper = module.titleblock.paper_for_part_count(count)
        positions = module._schematic_layout(loaded_brief, paper)
        xs = [position[0] for position in positions.values()]
        ys = [position[1] for position in positions.values()]
        width, height = module.titleblock.PAPER_SIZES[paper]
        usage = (max(xs) - min(xs)) * (max(ys) - min(ys)) / (width * height)
        assert usage >= 0.30, (count, paper, usage)


def _finding(finding_type: str) -> sch_lint.SchLintFinding:
    return sch_lint.SchLintFinding(
        type=finding_type,
        severity="warning",
        description=f"{finding_type} fixture",
        items=["R1"],
    )


def test_readability_failures_selects_gate_types() -> None:
    module = _module()
    gated = [
        _finding("property_on_symbol"),
        _finding("sheet_underutilized"),
        _finding("item_out_of_bounds"),
        _finding("label_only_connectivity"),
    ]
    ignored = [
        _finding("property_hidden"),
        _finding("title_block_incomplete"),
        _finding("notes_absent"),
        _finding("junction_missing"),
        _finding("label_off_wire"),
        _finding("component_on_power_symbol"),
        _finding("power_flag_crowded"),
    ]
    report = sch_lint.SchLintReport(
        source=Path("fixture.kicad_sch"),
        verdict="pass",
        errors=0,
        warnings=len(gated) + len(ignored),
        symbols_checked=3,
        findings=[*gated, *ignored],
    )
    failures = module._readability_failures(report)
    assert {finding.type for finding in failures} == {
        "property_on_symbol",
        "sheet_underutilized",
        "item_out_of_bounds",
        "label_only_connectivity",
    }


class _StubSession:
    """KonnectSession duck-type recording calls and answering canned results."""

    def __init__(self, schematic: Path) -> None:
        self.schematic = schematic
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, arguments: Mapping[str, object], timeout: float = 30.0) -> object:
        self.calls.append((name, dict(arguments)))
        if name == "batch_place_components":
            self.schematic.write_text(
                '(kicad_sch (version 20231120) (generator "eeschema") (lib_symbols))',
                encoding="utf-8",
            )
            return {"placed": len(cast(list[Any], arguments["components"]))}
        if name == "batch_get_schematic_pin_locations":
            return {
                "components": [
                    {
                        "reference": reference,
                        "x": x,
                        "y": y,
                        "pins": [{"number": "1", "x": x, "y": y - 3.81}],
                    }
                    for reference, x, y in (
                        ("J1", 25.4, 25.4),
                        ("R1", 184.6, 109.9),
                        ("D1", 25.4, 109.9),
                    )
                ]
            }
        return {}


def _stub_run(module: Any, tmp_path: Path) -> Any:
    loaded_brief = brief.load_brief(_LED_LOOP_BRIEF)
    project = tmp_path / "led_loop.kicad_pro"
    project.write_text("{}", encoding="utf-8")
    run = module.AuthoringRun(
        brief_path=_LED_LOOP_BRIEF,
        intake_path=None,
        workdir=tmp_path,
        kicad_share=tmp_path,
        loaded_brief=loaded_brief,
        log=io.StringIO(),
        reports_dir=tmp_path,
        konnect_exports=tmp_path,
        project=project,
        schematic=tmp_path / "led_loop.kicad_sch",
        board=tmp_path / "led_loop.kicad_pcb",
        socket_path=tmp_path / "konnect.sock",
        environment={},
    )
    run.konnect = cast(Any, _StubSession(run.schematic))
    return run


def test_stage_author_schematic_wires_signal_chain(tmp_path: Path) -> None:
    module = _module()
    run = _stub_run(module, tmp_path)
    module._stage_author_schematic(run)
    session = cast(_StubSession, run.konnect)
    names = [name for name, _ in session.calls]

    place = session.calls[names.index("batch_place_components")][1]
    components = cast(list[dict[str, Any]], place["components"])
    board_xy = {(8.0, 12.0), (20.0, 12.0), (36.0, 12.0)}
    assert all((item["x"], item["y"]) not in board_xy for item in components)

    wire = session.calls[names.index("batch_connect_pins")][1]
    connections = cast(list[dict[str, str]], wire["connections"])
    assert connections == [
        {"ref1": "J1", "pin1": "1", "ref2": "R1", "pin2": "1"},
        {"ref1": "R1", "pin1": "2", "ref2": "D1", "pin2": "2"},
        {"ref1": "D1", "pin1": "1", "ref2": "J1", "pin2": "2"},
    ]

    assert names.count("edit_schematic_component") == 3
    assert names.count("batch_connect_to_net") == 3
    assert names.index("batch_place_components") < names.index("batch_connect_pins")
    assert names.index("batch_connect_pins") < names.index("batch_connect_to_net")


def test_stage_author_schematic_retries_wiring_per_connection(tmp_path: Path) -> None:
    module = _module()
    run = _stub_run(module, tmp_path)
    session = cast(_StubSession, run.konnect)
    real_call = session.call

    def flaky(name: str, arguments: Mapping[str, object], timeout: float = 30.0) -> object:
        if name == "batch_connect_pins":
            raise module.StepFailure(name, "batch failed")
        return real_call(name, arguments, timeout)

    session.call = flaky  # type: ignore[method-assign]
    module._stage_author_schematic(run)
    names = [name for name, _ in session.calls]
    assert names.count("connect_pins") == 3
