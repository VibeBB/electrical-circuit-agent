import json
from pathlib import Path

import pytest

from circuit.kicad_cli import (
    KicadCliError,
    Report,
    diff,
    erc,
    export,
    export_netlist,
    jobset_run,
    render,
    render_layers,
    render_schematic,
    reports_equivalent,
)


def test_report_passes_without_errors_or_unconnected(tmp_path: Path) -> None:
    report_path = tmp_path / "drc.json"
    report_path.write_text(
        '{"$schema": "https://schemas.kicad.org/drc.v1.json",'
        ' "violations": [{"type": "warning", "severity": "warning",'
        ' "description": "advisory", "items": []}],'
        ' "unconnected_items": [], "schematic_parity": []}',
        encoding="utf-8",
    )
    report = Report.from_json_file(
        report_path,
        kind="drc",
        source=tmp_path / "board.kicad_pcb",
        kicad_version="10.99.0",
    )
    assert report.verdict == "pass"
    assert report.warnings == 1


def test_report_fails_for_unconnected_items(tmp_path: Path) -> None:
    report_path = tmp_path / "drc.json"
    report_path.write_text(
        '{"$schema": "https://schemas.kicad.org/drc.v1.json",'
        ' "violations": [], "schematic_parity": [],'
        ' "unconnected_items": [{"type": "unconnected", "severity": "error",'
        ' "description": "missing", "items": []}]}',
        encoding="utf-8",
    )
    report = Report.from_json_file(
        report_path,
        kind="drc",
        source=tmp_path / "board.kicad_pcb",
        kicad_version="10.99.0",
    )
    assert report.verdict == "fail"
    assert report.violations[0].type == "unconnected"
    assert report.unconnected == 1


def test_report_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "erc.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(KicadCliError):
        Report.from_json_file(
            path,
            kind="erc",
            source=tmp_path / "board.kicad_sch",
            kicad_version="10.99.0",
        )


def test_committed_samples_parse() -> None:
    root = Path(__file__).parent / "data"
    erc_report = Report.from_json_file(
        root / "erc_sample.json",
        kind="erc",
        source=Path("board.kicad_sch"),
        kicad_version="10.99.0",
    )
    drc_report = Report.from_json_file(
        root / "drc_sample.json",
        kind="drc",
        source=Path("board.kicad_pcb"),
        kicad_version="10.99.0",
    )
    assert erc_report.verdict == "fail"
    assert drc_report.verdict == "fail"


def test_report_rejects_wrong_schema(tmp_path: Path) -> None:
    path = tmp_path / "erc.json"
    path.write_text('{"$schema": "https://schemas.kicad.org/unknown.v1.json"}', encoding="utf-8")
    with pytest.raises(KicadCliError):
        Report.from_json_file(
            path,
            kind="erc",
            source=Path("board.kicad_sch"),
            kicad_version="10.99.0",
        )


def test_erc_sheet_violations_are_flattened(tmp_path: Path) -> None:
    path = tmp_path / "erc.json"
    path.write_text(
        '{"$schema": "https://schemas.kicad.org/erc.v1.json", "sheets": ['
        '{"path": "/main", "violations": ['
        '{"type": "error", "severity": "error", "description": "bad",'
        ' "items": [{"description": "U1 pin 1"}]},'
        '{"type": "warning", "severity": "warning", "description": "check",'
        ' "items": []}]}]}',
        encoding="utf-8",
    )
    report = Report.from_json_file(
        path,
        kind="erc",
        source=Path("board.kicad_sch"),
        kicad_version="10.99.0",
    )
    assert report.verdict == "fail"
    assert report.violations[0].sheet == "/main"
    assert report.violations[0].items == ["U1 pin 1"]


def test_erc_warnings_only_pass(tmp_path: Path) -> None:
    path = tmp_path / "erc.json"
    path.write_text(
        '{"$schema": "https://schemas.kicad.org/erc.v1.json", "sheets": ['
        '{"path": "/", "violations": [{"type": "warning", "severity": "warning",'
        ' "description": "check", "items": []}]}]}',
        encoding="utf-8",
    )
    report = Report.from_json_file(
        path,
        kind="erc",
        source=Path("board.kicad_sch"),
        kicad_version="10.99.0",
    )
    assert report.verdict == "pass"
    assert report.warnings == 1


def test_drc_schematic_parity_error_fails(tmp_path: Path) -> None:
    path = tmp_path / "drc.json"
    path.write_text(
        '{"$schema": "https://schemas.kicad.org/drc.v1.json",'
        ' "violations": [], "unconnected_items": [],'
        ' "schematic_parity": [{"type": "parity", "severity": "error",'
        ' "description": "mismatch", "items": []}]}',
        encoding="utf-8",
    )
    report = Report.from_json_file(
        path,
        kind="drc",
        source=Path("board.kicad_pcb"),
        kicad_version="10.99.0",
    )
    assert report.verdict == "fail"


def test_erc_reuses_cached_report_for_unchanged_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sch = tmp_path / "a.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    output = tmp_path / "a.erc.json"
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        Path(args[args.index("--output") + 1]).write_text(
            '{"$schema":"https://schemas.kicad.org/erc.v1.json",'
            '"kicad_version":"10.99.0","sheets":[]}',
            encoding="utf-8",
        )
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    monkeypatch.setattr("circuit.kicad_cli.version", lambda: "10.99.0")
    assert erc(sch, output).verdict == "pass"
    assert len(calls) == 1
    assert erc(sch, output).verdict == "pass"
    assert len(calls) == 1  # unchanged input served from the cache
    sch.write_text("(kicad_sch) changed", encoding="utf-8")
    assert erc(sch, output).verdict == "pass"
    assert len(calls) == 2


def test_export_netlist_uses_kicadsexpr_and_requires_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        output = Path(args[args.index("-o") + 1])
        output.write_text('(export (version "E") (components) (nets))', encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    output = export_netlist(tmp_path / "board.kicad_sch", tmp_path / "out" / "board.net")
    assert output.is_file()
    assert calls == [
        [
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            "-o",
            str(tmp_path / "out" / "board.net"),
            str(tmp_path / "board.kicad_sch"),
        ]
    ]


def test_render_uses_requested_view_and_requires_nonempty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(b"PNG")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    output = render(pcb, tmp_path / "render.png", side="bottom")
    assert output.read_bytes() == b"PNG"
    assert calls[0][-2:] == ["bottom", str(pcb)]


def test_render_passes_camera_controls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        Path(args[args.index("--output") + 1]).write_bytes(b"PNG")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    render(
        pcb,
        tmp_path / "render.png",
        side="left",
        rotate="-45,0,45",
        zoom=2.5,
        pan="3,0,0",
        pivot="-10,2,0",
        perspective=True,
        floor=True,
        background="opaque",
        quality="high",
    )
    args = calls[0]
    assert args[args.index("--side") + 1] == "left"
    assert args[args.index("--rotate") + 1] == "-45,0,45"
    assert args[args.index("--zoom") + 1] == "2.5"
    assert args[args.index("--pan") + 1] == "3,0,0"
    assert args[args.index("--pivot") + 1] == "-10,2,0"
    assert "--perspective" in args
    assert "--floor" in args
    assert args[args.index("--background") + 1] == "opaque"
    assert args[args.index("--quality") + 1] == "high"


@pytest.mark.parametrize("bad", ["1,2", "a,b,c", "1,2,3,4", "1..0,2,3"])
def test_render_rejects_malformed_xyz_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(_args: list[str], **_: object):
        raise AssertionError("kicad-cli must not run for malformed args")

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    with pytest.raises(KicadCliError, match="X,Y,Z"):
        render(pcb, tmp_path / "render.png", rotate=bad)


def test_render_schematic_collects_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    out_dir = tmp_path / "sch-png"
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        (out_dir / "board-1.png").write_bytes(b"PNG")
        (out_dir / "board-2.png").write_bytes(b"PNG")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    images = render_schematic(sch, out_dir, pages="1,2", dpi=150, black_and_white=True)
    assert [path.name for path in images] == ["board-1.png", "board-2.png"]
    args = calls[0]
    assert args[:3] == ["sch", "export", "png"]
    assert args[args.index("--pages") + 1] == "1,2"
    assert args[args.index("--dpi") + 1] == "150"
    assert "--black-and-white" in args


def test_render_schematic_fails_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")

    def fake_run(args: list[str], **_: object):
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    with pytest.raises(KicadCliError, match="no PNG output"):
        render_schematic(sch, tmp_path / "sch-png")


def test_render_layers_builds_expected_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    out_dir = tmp_path / "layers"
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        (out_dir / "board-F_Cu.png").write_bytes(b"PNG")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    images = render_layers(
        pcb,
        out_dir,
        layers="F.Cu,B.Cu",
        common_layers="Edge.Cuts",
        mirror=True,
        scale=0,
        sketch_pads_on_fab_layers=True,
        sketch_pad_numbers=True,
        black_and_white=True,
        include_border_title=True,
        dpi=150,
    )
    assert [path.name for path in images] == ["board-F_Cu.png"]
    args = calls[0]
    assert args[:3] == ["pcb", "export", "png"]
    assert args[args.index("--layers") + 1] == "F.Cu,B.Cu"
    assert args[args.index("--common-layers") + 1] == "Edge.Cuts"
    assert "--mirror" in args
    assert args[args.index("--scale") + 1] == "0"
    assert "--sketch-pads-on-fab-layers" in args
    assert "--sketch-pad-numbers" in args
    assert "--black-and-white" in args
    assert "--include-border-title" in args


def test_render_layers_requires_layers(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    with pytest.raises(KicadCliError, match="layers"):
        render_layers(pcb, tmp_path / "out", layers="  ")


def test_export_fp_svg_builds_expected_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    footprint = tmp_path / "SOIC-8.kicad_mod"
    footprint.write_text("(footprint)", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    export("fp_svg", footprint, tmp_path / "out")
    args = calls[0]
    assert args[:3] == ["fp", "export", "svg"]
    assert "--sketch-pads-on-fab-layers" in args
    assert "--sketch-pad-numbers" in args
    assert args[-1] == str(footprint)


@pytest.mark.parametrize("returncode, identical", [(0, True), (5, False)])
def test_diff_parses_json_and_exit_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, identical: bool
) -> None:
    left = tmp_path / "left.kicad_pcb"
    right = tmp_path / "right.kicad_pcb"
    left.write_text("(kicad_pcb)", encoding="utf-8")
    right.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(args: list[str], **_: object):
        output = Path(args[args.index("--output") + 1])
        output.write_text(json.dumps({"changes": []}), encoding="utf-8")
        return type("Result", (), {"returncode": returncode, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    result = diff("pcb", left, right, tmp_path / "diff.json")
    assert result.identical is identical
    assert result.exit_code == returncode


def test_diff_rejects_unexpected_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    left = tmp_path / "left.kicad_sch"
    right = tmp_path / "right.kicad_sch"
    left.write_text("(kicad_sch)", encoding="utf-8")
    right.write_text("(kicad_sch)", encoding="utf-8")

    def fake_run(_args: list[str], **_: object):
        return type("Result", (), {"returncode": 2, "stderr": "bad", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    with pytest.raises(KicadCliError, match="bad"):
        diff("sch", left, right, tmp_path / "diff.json")


def test_jobset_run_parses_outputs_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "board.kicad_pro"
    project.write_text("{}", encoding="utf-8")
    jobset = tmp_path / "jobset.kicad_jobset"
    jobset.write_text(
        json.dumps(
            {
                "jobs": [],
                "meta": {"version": 1},
                "outputs": [
                    {
                        "id": "out",
                        "type": "folder",
                        "only": [],
                        "description": "out",
                        "settings": {"output_path": "."},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_run(args: list[str], **_: object):
        run_jobset = Path(args[args.index("--file") + 1])
        data = json.loads(run_jobset.read_text(encoding="utf-8"))
        output = Path(data["outputs"][0]["settings"]["output_path"])
        (output / "erc.json").write_text("{}", encoding="utf-8")
        (output / "drc.json").write_text("{}", encoding="utf-8")
        (output / "board.step").write_text("step", encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    result = jobset_run(project, tmp_path / "out", jobset)
    assert result.exit_code == 0
    assert result.erc_report == tmp_path / "out" / "erc.json"
    assert len(result.outputs) == 3


def test_jobset_run_fails_on_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "board.kicad_pro"
    project.write_text("{}", encoding="utf-8")
    jobset = tmp_path / "jobset.kicad_jobset"
    jobset.write_text('{"jobs":[],"meta":{"version":1},"outputs":[]}', encoding="utf-8")

    def fake_run(_args: list[str], **_: object):
        return type("Result", (), {"returncode": 1, "stderr": "job failed", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    with pytest.raises(KicadCliError, match="job failed"):
        jobset_run(project, tmp_path / "out", jobset)


def test_reports_equivalent_ignores_only_date(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text('{"date":"one","violations":[]}', encoding="utf-8")
    right.write_text('{"date":"two","violations":[]}', encoding="utf-8")
    assert reports_equivalent(left, right)
    right.write_text('{"date":"two","violations":[{"type":"error"}]}', encoding="utf-8")
    assert not reports_equivalent(left, right)


@pytest.mark.parametrize(
    "kind, source_name",
    [
        ("sch_pdf", "board.kicad_sch"),
        ("sch_svg", "board.kicad_sch"),
        ("pcb_pdf", "board.kicad_pcb"),
        ("pcb_svg", "board.kicad_pcb"),
        ("dxf", "board.kicad_pcb"),
        ("ipc2581", "board.kicad_pcb"),
        ("odb", "board.kicad_pcb"),
        ("gencad", "board.kicad_pcb"),
        ("vrml", "board.kicad_pcb"),
        ("glb", "board.kicad_pcb"),
    ],
)
def test_new_export_kinds_build_expected_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, source_name: str
) -> None:
    source = tmp_path / source_name
    source.write_text("source", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("circuit.kicad_cli.run", fake_run)
    export(kind, source, tmp_path / "out")  # type: ignore[arg-type]
    assert calls
    assert calls[0][0] in {"sch", "pcb"}
    output_arg = Path(calls[0][calls[0].index("--output") + 1])
    if kind == "sch_svg":
        # kicad-cli treats -o as a directory for sch svg exports.
        assert output_arg == tmp_path / "out"
    elif kind == "sch_pdf":
        assert output_arg == tmp_path / "out" / "board.pdf"
    elif kind in {"pcb_svg", "dxf"}:
        assert output_arg == tmp_path / "out"
        assert "F.Cu,B.Cu,Edge.Cuts" in calls[0]
    elif kind == "pcb_pdf":
        assert output_arg == tmp_path / "out" / "board.pdf"
        assert "F.Cu,B.Cu,Edge.Cuts" in calls[0]
