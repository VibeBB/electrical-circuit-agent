import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, cast

import pytest

from circuit import libraries as libraries_module
from circuit.brief import DesignBrief
from circuit.landpattern import LandPatternResult
from circuit.libitems import parse_footprint
from circuit.libraries import LibraryRoots, check_libraries, symbol_pins
from circuit.libreview import ReviewFinding, ReviewStatus
from circuit.libverify import (
    LibraryVerification,
    VerificationInputs,
    VerifiedFootprint,
    VerifiedSymbol,
)
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
    part_spec_sha256,
)
from circuit.pinsource import PinSourceInput
from pinout_fixtures import pinout_drawing

SYMBOLS = """\
(kicad_symbol_lib
  (version 20241209)
  (symbol "R"
    (symbol "R_0_1"
      (pin passive line (number "1") (name "~"))
      (pin passive line (number "2") (name "~"))))
  (symbol "LED"
    (symbol "LED_0_1"
      (pin passive line (number "1") (name "K"))
      (pin passive line (number "2") (name "A"))))
  (symbol "LED_ALT" (extends "LED"))
)
"""


def _brief() -> DesignBrief:
    return DesignBrief.model_validate(
        {
            "name": "test",
            "parts": [
                {"reference": "R1", "lib_id": "Device:R", "footprint": "Device:X"},
                {"reference": "D1", "lib_id": "Device:LED", "footprint": "Device:Y"},
            ],
            "nets": [
                {"name": "N1", "pins": ["R1.1", "D1.1"]},
                {"name": "N2", "pins": ["R1.2", "D1.2"]},
            ],
            "board": {"width_mm": 10, "height_mm": 10},
        }
    )


def _roots(tmp_path: Path) -> LibraryRoots:
    symbols = tmp_path / "symbols"
    footprints = tmp_path / "footprints"
    (footprints / "Device.pretty").mkdir(parents=True)
    (footprints / "Device.pretty" / "X.kicad_mod").write_text("(footprint X)", encoding="utf-8")
    (footprints / "Device.pretty" / "Y.kicad_mod").write_text("(footprint Y)", encoding="utf-8")
    symbols.mkdir()
    (symbols / "Device.kicad_sym").write_text(SYMBOLS, encoding="utf-8")
    return LibraryRoots(symbol_dirs=[symbols], footprint_dirs=[footprints])


def _empty_roots(tmp_path: Path) -> LibraryRoots:
    symbols = tmp_path / "symbols"
    footprints = tmp_path / "footprints"
    symbols.mkdir(parents=True)
    footprints.mkdir(parents=True)
    return LibraryRoots(symbol_dirs=[symbols], footprint_dirs=[footprints])


def _project_spec() -> PartSpec:
    reading = Reading(
        page=1,
        bbox=(0, 0, 1, 1),
        vision="fixture",
        vision_record="vision.json",
    )

    def dimension(value: float) -> Dimension:
        return Dimension(nom=value, reading=reading)

    package = PackageSpec(
        family="gullwing_dual",
        drawing_id="X",
        pin_count=2,
        pitch=dimension(0.65),
        body_length=dimension(2.0),
        body_width=dimension(1.5),
        height=dimension(0.5),
        lead_span=dimension(3.0),
        lead_length=dimension(0.5),
        lead_width=dimension(0.3),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=reading,
    )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TEST",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        pinout=pinout_drawing({"1": "PIN1", "2": "PIN2"}),
        pins=[
            PinSpec(
                number=str(number),
                name=f"PIN{number}",
                electrical_type="passive",
                reading=reading,
            )
            for number in range(1, 3)
        ],
        orderable=[
            OrderableVariant(
                mpn="TEST",
                package_designator="X",
                pin_count=2,
                row=CellRef(table=0, row=1, col=0),
                reading=reading,
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def _fake_project_verifier(
    spec: PartSpec,
    *,
    spec_path: Path,
    spec_check_path: Path | None = None,
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    library_dir: Path | None,
    reference: LandPatternResult,
    tolerance_mm: float = 0.02,
    model_required: bool = True,
    pin_source_path: Path | None = None,
    pin_sources: Sequence[PinSourceInput | Path] | None = None,
    output_path: Path | None = None,
) -> LibraryVerification:
    del spec, spec_check_path, library_dir, reference
    footprint = parse_footprint(footprint_path)
    text = footprint_path.read_text(encoding="utf-8")
    verdict = "fail" if "(at 0.1 0)" in text else "pass"
    report = LibraryVerification(
        artifact_kind="circuit_library_verification",
        verdict=verdict,
        part_spec_sha256=part_spec_sha256(spec_path),
        inputs=VerificationInputs(
            part_spec_path=Path("part-spec.json"),
            symbol_lib=Path(symbol_lib.name),
            symbol_name=symbol_name,
            footprint_path=Path(footprint_path.relative_to(symbol_lib.parent)),
            density="nominal",
            tolerance_mm=tolerance_mm,
            model_required=model_required,
            pin_source_path=pin_source_path,
            pin_sources=[
                source if isinstance(source, PinSourceInput) else PinSourceInput(path=source)
                for source in pin_sources or []
            ],
        ),
        symbol=VerifiedSymbol(
            lib_path=symbol_lib,
            name=symbol_name,
            sha256=hashlib.sha256(symbol_lib.read_bytes()).hexdigest(),
        ),
        footprint=VerifiedFootprint(
            path=footprint_path,
            name=footprint.name,
            sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
        ),
        models=[],
        findings=[],
    )
    if output_path is not None:
        output_path.write_text(report.model_dump_json(), encoding="utf-8")
    return report


def _packet_id_stub(packet: str) -> Callable[..., str]:
    def fake_current_packet_id(
        _spec_path: Path,
        *,
        symbol_lib: Path,
        symbol_name: str,
        footprint_path: Path,
        library_dir: Path | None,
        density: Literal["most", "nominal", "least"],
        tolerance_mm: float,
        model_required: bool,
        pin_source_path: Path | None = None,
        pin_sources: Sequence[PinSourceInput] | None = None,
    ) -> str:
        del symbol_lib, symbol_name, footprint_path, library_dir
        del density, tolerance_mm, model_required, pin_source_path, pin_sources
        return packet

    return fake_current_packet_id


def _review_status_stub(
    packet: str,
    state: Literal["approved", "rejected", "pending", "invalid"],
) -> Callable[..., ReviewStatus]:
    def fake_review_status(
        _library_dir: Path,
        _spec: PartSpec,
        _packet_id: str,
        **_kwargs: object,
    ) -> ReviewStatus:
        return ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet,
            state=state,
            reasons=[],
            decisions=[],
        )

    return fake_review_status


def test_library_resolution_pass_and_extends(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert result.verdict == "pass"
    assert result.symbols["Device:LED"].pins == ["1", "2"]
    assert symbol_pins(roots.symbol_dirs[0] / "Device.kicad_sym", "LED_ALT") == ["1", "2"]


def test_missing_symbol_library_and_footprint(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    value = _brief().model_dump(mode="json")
    value.update(
        {
            "parts": [
                {"reference": "R1", "lib_id": "Missing:R", "footprint": "Missing:X"},
                {"reference": "D1", "lib_id": "Device:LED", "footprint": "Device:Y"},
            ]
        }
    )
    brief = DesignBrief.model_validate(value)
    path = tmp_path / "brief.json"
    path.write_text("{}", encoding="utf-8")
    result = check_libraries(brief, brief_path=path, roots=roots)
    assert result.verdict == "fail"
    assert result.missing_symbol_libraries == ["Missing"]
    assert result.missing_footprint_libraries == ["Missing"]


def test_missing_symbol_footprint_and_pin(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    value = _brief().model_dump(mode="json")
    value.update(
        {
            "parts": [
                {"reference": "R1", "lib_id": "Device:R", "footprint": "Device:Missing"},
                {"reference": "D1", "lib_id": "Device:LED", "footprint": "Device:Y"},
            ],
            "nets": [{"name": "N1", "pins": ["R1.3", "D1.1"]}],
        }
    )
    brief = DesignBrief.model_validate(value)
    path = tmp_path / "brief.json"
    path.write_text("{}", encoding="utf-8")
    result = check_libraries(brief, brief_path=path, roots=roots)
    assert result.verdict == "fail"
    assert result.missing_footprints == ["Device:Missing"]
    assert result.missing_pins == {"R1": ["3"]}


def test_malformed_symbol_fails_closed(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    (roots.symbol_dirs[0] / "Device.kicad_sym").write_text("(broken", encoding="utf-8")
    path = tmp_path / "brief.json"
    path.write_text("{}", encoding="utf-8")
    result = check_libraries(_brief(), brief_path=path, roots=roots)
    assert result.verdict == "fail"
    assert any("could not parse symbol library" in reason for reason in result.reasons)


def test_search_order_prefers_first_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    footprints = tmp_path / "footprints" / "Device.pretty"
    footprints.mkdir(parents=True)
    for name in ("X", "Y"):
        (footprints / f"{name}.kicad_mod").write_text(f"(footprint {name})", encoding="utf-8")
    first_lib = first / "Device.kicad_sym"
    second_lib = second / "Device.kicad_sym"
    first_lib.write_text(SYMBOLS.replace('(number "2")', '(number "9")'), encoding="utf-8")
    second_lib.write_text(SYMBOLS, encoding="utf-8")
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    assert symbol_pins(first_lib, "R") == ["1", "9"]
    result = check_libraries(
        _brief(),
        brief_path=brief_path,
        roots=LibraryRoots(
            symbol_dirs=[first, second],
            footprint_dirs=[tmp_path / "footprints"],
        ),
    )
    assert result.symbols["Device:R"].pins == ["1", "9"]


def _write_project_library(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    library = tmp_path / "library"
    library.mkdir()
    symbol_path = library / "Device.kicad_sym"
    symbol_path.write_text(SYMBOLS, encoding="utf-8")
    (library / "part-spec.json").write_text(
        _project_spec().model_dump_json(),
        encoding="utf-8",
    )
    footprint_dir = library / "Device.pretty"
    footprint_dir.mkdir()
    footprint_paths: dict[str, Path] = {}
    for name in ("X", "Y"):
        footprint_path = footprint_dir / f"{name}.kicad_mod"
        footprint_path.write_text(
            f'(footprint "{name}" (layer "F.Cu") '
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))',
            encoding="utf-8",
        )
        footprint_paths[name] = footprint_path
    return symbol_path, footprint_paths


def _write_project_verification(
    library: Path,
    symbol_path: Path,
    symbol_name: str,
    footprint_path: Path,
    *,
    verdict: Literal["pass", "fail"] = "pass",
    pin_source_path: Path | None = None,
    pin_sources: list[PinSourceInput] | None = None,
) -> None:
    verification_dir = library / "verification"
    verification_dir.mkdir(exist_ok=True)
    report = LibraryVerification(
        artifact_kind="circuit_library_verification",
        verdict=verdict,
        part_spec_sha256="a" * 64,
        inputs=VerificationInputs(
            part_spec_path=Path("part-spec.json"),
            symbol_lib=Path(symbol_path.name),
            symbol_name=symbol_name,
            footprint_path=Path(footprint_path.relative_to(library)),
            density="nominal",
            tolerance_mm=0.02,
            model_required=True,
            pin_source_path=pin_source_path,
            pin_sources=pin_sources or [],
        ),
        symbol=VerifiedSymbol(
            lib_path=symbol_path,
            name=symbol_name,
            sha256=hashlib.sha256(symbol_path.read_bytes()).hexdigest(),
        ),
        footprint=VerifiedFootprint(
            path=footprint_path,
            name=footprint_path.stem,
            sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
        ),
        models=[],
        findings=[],
    )
    (verification_dir / f"{symbol_name}.verification.json").write_text(
        report.model_dump_json(),
        encoding="utf-8",
    )


def test_project_library_precedes_default_roots_and_requires_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        libraries_module,
        "verify_library_part",
        _fake_project_verifier,
    )
    roots = _empty_roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")

    unverified = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert unverified.verdict == "fail"
    assert "unverified project library part: Device:R" in unverified.reasons
    assert "unverified project library part: Device:LED" in unverified.reasons
    assert unverified.symbol_dirs[0] == tmp_path / "library"

    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "R",
        footprint_paths["X"],
        verdict="fail",
    )
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "LED",
        footprint_paths["Y"],
    )
    (tmp_path / "part.pdf").write_bytes(b"fixture PDF")
    monkeypatch.setattr(libraries_module, "current_packet_id", _packet_id_stub("a" * 16))
    monkeypatch.setattr(
        libraries_module, "review_status", _review_status_stub("a" * 16, "approved")
    )
    verified = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert verified.verdict == "pass"
    assert verified.symbols["Device:R"].library_path == symbol_path
    assert verified.footprints["Device:X"] == footprint_paths["X"]


def test_project_review_identity_preserves_pin_source_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        libraries_module,
        "verify_library_part",
        _fake_project_verifier,
    )
    roots = _empty_roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    (tmp_path / "part.pdf").write_bytes(b"fixture PDF")
    source_dir = tmp_path / "library" / "sources"
    source_dir.mkdir()
    first_source = PinSourceInput(
        path=Path("sources/device.atdf"),
        kind="microchip_atdf",
        pinout_name="QFN32",
        derived_from=["part_spec"],
    )
    second_source = PinSourceInput(
        path=Path("sources/amd.txt"),
        kind="amd_package_file",
        derived_from=["stm32_open_pin_data"],
    )
    (source_dir / "device.atdf").write_text("<device />", encoding="utf-8")
    (source_dir / "amd.txt").write_text("synthetic pin data", encoding="utf-8")
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "R",
        footprint_paths["X"],
        pin_source_path=first_source.path,
        pin_sources=[first_source, second_source],
    )
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "LED",
        footprint_paths["Y"],
    )
    packet_inputs: list[tuple[Path | None, list[PinSourceInput] | None]] = []

    def current_packet_id(_spec_path: Path, **kwargs: object) -> str:
        pin_source_path = kwargs.get("pin_source_path")
        pin_sources = kwargs.get("pin_sources")
        packet_inputs.append(
            (
                pin_source_path if isinstance(pin_source_path, Path) else None,
                cast(list[PinSourceInput], pin_sources) if isinstance(pin_sources, list) else None,
            )
        )
        return "a" * 16

    monkeypatch.setattr(libraries_module, "current_packet_id", current_packet_id)
    monkeypatch.setattr(
        libraries_module,
        "review_status",
        _review_status_stub("a" * 16, "approved"),
    )

    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)

    assert result.verdict == "pass"
    source_root = (tmp_path / "library").resolve()
    expected_sources = [
        first_source.model_copy(update={"path": source_root / first_source.path}),
        second_source.model_copy(update={"path": source_root / second_source.path}),
    ]
    assert (source_root / first_source.path, expected_sources) in packet_inputs


def test_project_nickname_conflicts_and_stale_verification_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        libraries_module,
        "verify_library_part",
        _fake_project_verifier,
    )
    roots = _roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "R",
        footprint_paths["X"],
    )
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "LED",
        footprint_paths["Y"],
    )

    (footprint_paths["X"]).write_text(
        '(footprint "X" (layer "F.Cu") (pad "1" smd rect (at 0.1 0) (size 1 1) (layers "F.Cu")))',
        encoding="utf-8",
    )
    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert result.verdict == "fail"
    assert "unverified project library part: Device:R" in result.reasons
    conflict = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert "library_nickname_conflict" in conflict.reasons


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("pending", "human_review_missing: Device:R"),
        ("rejected", "human_review_rejected: Device:R"),
        ("invalid", "human_review_invalid: Device:R"),
    ],
)
def test_project_library_gate_requires_approval_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: Literal["pending", "rejected", "invalid"],
    reason: str,
) -> None:
    monkeypatch.setattr(
        libraries_module,
        "verify_library_part",
        _fake_project_verifier,
    )
    monkeypatch.setattr(libraries_module, "current_packet_id", _packet_id_stub("b" * 16))
    monkeypatch.setattr(
        libraries_module,
        "review_status",
        _review_status_stub("b" * 16, state),
    )
    roots = _empty_roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    (tmp_path / "part.pdf").write_bytes(b"fixture PDF")
    _write_project_verification(tmp_path / "library", symbol_path, "R", footprint_paths["X"])
    _write_project_verification(tmp_path / "library", symbol_path, "LED", footprint_paths["Y"])

    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)

    assert result.verdict == "fail"
    assert reason in result.reasons


def test_project_library_approval_does_not_override_fresh_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        libraries_module,
        "verify_library_part",
        _fake_project_verifier,
    )
    monkeypatch.setattr(
        libraries_module,
        "current_packet_id",
        _packet_id_stub("c" * 16),
    )
    monkeypatch.setattr(
        libraries_module,
        "review_status",
        _review_status_stub("c" * 16, "approved"),
    )
    roots = _empty_roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    footprint_paths["X"].write_text(
        '(footprint "X" (layer "F.Cu") (pad "1" smd rect (at 0.1 0) (size 1 1) (layers "F.Cu")))',
        encoding="utf-8",
    )
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    (tmp_path / "part.pdf").write_bytes(b"fixture PDF")
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "R",
        footprint_paths["X"],
    )
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "LED",
        footprint_paths["Y"],
    )

    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)

    assert result.verdict == "fail"
    assert "unverified project library part: Device:R" in result.reasons
    assert "human_review_missing: Device:R" not in result.reasons


def test_project_library_gate_rejects_correction_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        libraries_module,
        "verify_library_part",
        _fake_project_verifier,
    )
    monkeypatch.setattr(libraries_module, "current_packet_id", _packet_id_stub("d" * 16))
    monkeypatch.setattr(
        libraries_module,
        "review_status",
        _review_status_stub("d" * 16, "approved"),
    )

    def correction_regressions(_library_dir: Path, _spec: PartSpec) -> list[ReviewFinding]:
        return [
            ReviewFinding(
                code="correction_regressed",
                severity="error",
                field="/manufacturer",
                message="current value differs from the accepted correction",
            )
        ]

    monkeypatch.setattr(libraries_module, "correction_regressions", correction_regressions)
    roots = _empty_roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    (tmp_path / "part.pdf").write_bytes(b"fixture PDF")
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "R",
        footprint_paths["X"],
    )
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "LED",
        footprint_paths["Y"],
    )

    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)

    assert result.verdict == "fail"
    assert "correction_regressed: Device:R" in result.reasons


def test_project_gate_rejects_verification_inputs_outside_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        libraries_module,
        "verify_library_part",
        _fake_project_verifier,
    )
    roots = _empty_roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    library = tmp_path / "library"
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    _write_project_verification(library, symbol_path, "R", footprint_paths["X"])
    _write_project_verification(library, symbol_path, "LED", footprint_paths["Y"])

    report_path = library / "verification" / "R.verification.json"
    report = LibraryVerification.model_validate_json(report_path.read_text(encoding="utf-8"))
    report.inputs = report.inputs.model_copy(update={"part_spec_path": Path("/etc/passwd")})
    report_path.write_text(report.model_dump_json(), encoding="utf-8")

    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert result.verdict == "fail"
    assert "unverified project library part: Device:R" in result.reasons
