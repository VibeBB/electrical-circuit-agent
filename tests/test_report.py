from pathlib import Path

from circuit.advisory import AdvisoryResult, merge_detail
from circuit.brief import load_brief
from circuit.kicad_cli import JobsetResult, Report
from circuit.netlist import check_connectivity, parse_netlist
from circuit.report import build_design_report, write_report

ROOT = Path(__file__).parent


def _connectivity():
    brief_path = ROOT / "data" / "brief_led_loop.json"
    netlist_path = ROOT / "data" / "netlist_sample.net"
    return check_connectivity(
        load_brief(brief_path),
        parse_netlist(netlist_path),
        brief_path=brief_path,
        netlist_path=netlist_path,
    )


def test_design_report_is_fail_closed_when_gate_missing(tmp_path: Path) -> None:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    value = build_design_report(
        load_brief(brief_path),
        brief_path=brief_path,
        kicad_version="10.99.0",
        connectivity=_connectivity(),
        erc=None,
        drc=None,
        exports={},
    )
    assert value.verdict == "fail"
    assert value.reasons == ["gate not executed: erc", "gate not executed: drc"]
    output = tmp_path / "design-report.json"
    write_report(value, output)
    assert output.exists()


def test_design_report_passes_only_when_all_gates_pass(tmp_path: Path) -> None:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    erc_path = tmp_path / "erc.json"
    drc_path = tmp_path / "drc.json"
    erc_path.write_text(
        '{"$schema":"https://schemas.kicad.org/erc.v1.json","kicad_version":"10.99.0","sheets":[]}',
        encoding="utf-8",
    )
    drc_path.write_text(
        '{"$schema":"https://schemas.kicad.org/drc.v1.json","kicad_version":"10.99.0",'
        '"violations":[],"unconnected_items":[],"schematic_parity":[]}',
        encoding="utf-8",
    )
    value = build_design_report(
        load_brief(brief_path),
        brief_path=brief_path,
        kicad_version="10.99.0",
        connectivity=_connectivity(),
        erc=Report.from_json_file(
            erc_path, kind="erc", source=tmp_path / "a.kicad_sch", kicad_version="10.99.0"
        ),
        drc=Report.from_json_file(
            drc_path, kind="drc", source=tmp_path / "a.kicad_pcb", kicad_version="10.99.0"
        ),
        exports={"gerbers": ["a.gbr"]},
    )
    assert value.verdict == "pass"


def test_advisory_error_does_not_change_gate_verdict(tmp_path: Path) -> None:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    erc_path = tmp_path / "erc.json"
    drc_path = tmp_path / "drc.json"
    erc_path.write_text(
        '{"$schema":"https://schemas.kicad.org/erc.v1.json","kicad_version":"10.99.0","sheets":[]}',
        encoding="utf-8",
    )
    drc_path.write_text(
        '{"$schema":"https://schemas.kicad.org/drc.v1.json","kicad_version":"10.99.0",'
        '"violations":[],"unconnected_items":[],"schematic_parity":[]}',
        encoding="utf-8",
    )
    advisory = AdvisoryResult(
        tool="run_design_review",
        stage="review",
        status="error",
        summary="advisory unavailable",
    )
    value = build_design_report(
        load_brief(brief_path),
        brief_path=brief_path,
        kicad_version="10.99.0",
        connectivity=_connectivity(),
        erc=Report.from_json_file(
            erc_path, kind="erc", source=tmp_path / "a.kicad_sch", kicad_version="10.99.0"
        ),
        drc=Report.from_json_file(
            drc_path, kind="drc", source=tmp_path / "a.kicad_pcb", kicad_version="10.99.0"
        ),
        exports={},
        advisory=[advisory],
    )

    assert value.verdict == "pass"
    assert value.advisory == [advisory]


def test_design_report_accepts_legacy_json_without_advisory(tmp_path: Path) -> None:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    value = build_design_report(
        load_brief(brief_path),
        brief_path=brief_path,
        kicad_version="10.99.0",
        connectivity=_connectivity(),
        erc=None,
        drc=None,
        exports={},
    )

    legacy = value.model_dump()
    legacy.pop("advisory")
    from circuit.report import DesignReport

    loaded = DesignReport.model_validate(legacy)
    assert loaded.advisory == []


def test_merge_detail_preserves_existing_advisory_detail() -> None:
    result = AdvisoryResult(
        tool="run_drc",
        stage="review",
        status="ok",
        summary="Konnect DRC completed",
        detail={"violations": 2, "source": "konnect"},
    )

    merge_detail(result, {"kicad_cli_error_count": 0, "source": "kicad-cli"})

    assert result.detail == {
        "violations": 2,
        "source": "kicad-cli",
        "kicad_cli_error_count": 0,
    }


def test_jobset_inconsistency_is_a_deterministic_failure(tmp_path: Path) -> None:
    brief_path = ROOT / "data" / "brief_led_loop.json"
    erc_path = tmp_path / "erc.json"
    drc_path = tmp_path / "drc.json"
    erc_path.write_text(
        '{"$schema":"https://schemas.kicad.org/erc.v1.json","kicad_version":"10.99.0","sheets":[]}',
        encoding="utf-8",
    )
    drc_path.write_text(
        '{"$schema":"https://schemas.kicad.org/drc.v1.json","kicad_version":"10.99.0",'
        '"violations":[],"unconnected_items":[],"schematic_parity":[]}',
        encoding="utf-8",
    )
    value = build_design_report(
        load_brief(brief_path),
        brief_path=brief_path,
        kicad_version="10.99.0",
        connectivity=_connectivity(),
        erc=Report.from_json_file(
            erc_path, kind="erc", source=tmp_path / "a.kicad_sch", kicad_version="10.99.0"
        ),
        drc=Report.from_json_file(
            drc_path, kind="drc", source=tmp_path / "a.kicad_pcb", kicad_version="10.99.0"
        ),
        exports={},
        jobset=JobsetResult(
            jobset=tmp_path / "jobset.kicad_jobset",
            project=tmp_path / "a.kicad_pro",
            output_dir=tmp_path / "out",
            exit_code=0,
        ),
        jobset_consistent=False,
    )
    assert value.verdict == "fail"
    assert "gate failed: jobset consistency" in value.reasons
