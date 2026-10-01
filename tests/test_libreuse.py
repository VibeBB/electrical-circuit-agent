import hashlib
from pathlib import Path
from typing import Literal

import pytest

from circuit.landpattern import LandPatternResult, compute_land_pattern
from circuit.libitems import parse_footprint
from circuit.libraries import LibraryRoots
from circuit.libreuse import find_candidates
from circuit.lineage import FootprintBase, FootprintLineage, pad_changes
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
)
from circuit.ruleprofile import EvidenceRef
from pinout_fixtures import pinout_drawing


def _reading(text: str = "drawing") -> Reading:
    return Reading(page=1, bbox=(0, 0, 1, 1), vision=text, vision_record="vision.json")


def _dimension(value: float) -> Dimension:
    return Dimension(nom=value, reading=_reading(str(value)))


def _spec() -> PartSpec:
    package = PackageSpec(
        family="gullwing_dual",
        drawing_id="SOIC-4",
        pin_count=4,
        pitch=_dimension(1.27),
        body_length=_dimension(4.9),
        body_width=_dimension(3.9),
        height=_dimension(1.0),
        lead_span=_dimension(6.0),
        lead_length=_dimension(0.8),
        lead_width=_dimension(0.4),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin one top left"),
    )
    pin_names = ["EN", "VIN", "SW", "GND"]
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TPS62130RGTR",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        pinout=pinout_drawing(
            {str(number): name for number, name in enumerate(pin_names, start=1)}
        ),
        pins=[
            PinSpec(
                number=str(index + 1),
                name=name,
                electrical_type="passive",
                reading=_reading(f"{index + 1} {name}"),
            )
            for index, name in enumerate(pin_names)
        ],
        orderable=[
            OrderableVariant(
                mpn="TPS62130RGTR",
                package_designator="SOIC-4",
                pin_count=4,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("TPS62130RGTR SOIC-4"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def _chip_spec() -> PartSpec:
    reading = _reading()
    package = PackageSpec(
        family="chip",
        drawing_id="1608",
        pin_count=2,
        body_length=_dimension(1.6),
        body_width=_dimension(0.8),
        height=_dimension(0.5),
        lead_length=_dimension(0.25),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin one top left"),
    )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="EXAMPLE1608",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256="b" * 64,
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        pins=[
            PinSpec(
                number=number,
                name=f"PIN{number}",
                electrical_type="passive",
                reading=reading,
            )
            for number in ("1", "2")
        ],
        orderable=[
            OrderableVariant(
                mpn="EXAMPLE1608",
                package_designator="1608",
                pin_count=2,
                row=CellRef(table=0, row=1, col=0),
                reading=reading,
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def _write_footprint(
    path: Path,
    name: str,
    pads: list[tuple[str, float, float, float, float, float]],
    *,
    model: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'(footprint "{name}" (layer "F.Cu") (attr smd)']
    for number, x, y, width, height, rotation in pads:
        lines.append(
            f'  (pad "{number}" smd roundrect (at {x} {y} {rotation}) '
            f'(size {width} {height}) (layers "F.Cu" "F.Mask" "F.Paste"))'
        )
    if model is not None:
        lines.append(f'  (model "{model}")')
    lines.append(")")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_candidate_lineage(
    footprint_path: Path,
    library_dir: Path,
    base_pads: list[tuple[str, float, float, float, float, float]],
    *,
    layer: Literal["organization", "product"],
    product: str | None = None,
) -> Path:
    project_root = library_dir.parent
    base_path = project_root / "base.kicad_mod"
    _write_footprint(base_path, "Base", base_pads)
    evidence_path = project_root / "prototype.txt"
    evidence_path.write_text("prototype results", encoding="utf-8")
    base = parse_footprint(base_path)
    footprint = parse_footprint(footprint_path)
    lineage = FootprintLineage(
        artifact_kind="circuit_footprint_lineage",
        footprint_sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
        layer=layer,
        product=product,
        base=FootprintBase(
            kind="manufacturer",
            path=base_path.name,
            sha256=hashlib.sha256(base_path.read_bytes()).hexdigest(),
        ),
        changes=pad_changes(base, footprint),
        reason="validated prototype tuning",
        evidence=[
            EvidenceRef(
                kind="prototype",
                path=evidence_path.name,
                sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            )
        ],
    )
    sidecar = Path(f"{footprint_path}.lineage.json")
    sidecar.write_text(lineage.model_dump_json(), encoding="utf-8")
    return sidecar


def _symbol_text(
    name: str,
    pins: list[tuple[str, str]],
) -> str:
    pin_text = "\n".join(
        f'      (pin passive line (at 0 0 0) (length 2.54) (name "{pin_name}") (number "{number}"))'
        for number, pin_name in pins
    )
    return f'(symbol "{name}" (symbol "{name}_0_1"\n{pin_text}\n    ))'


def _write_symbol_library(path: Path, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"(kicad_symbol_lib (version 20241209) {' '.join(symbols)})",
        encoding="utf-8",
    )


def _roots(tmp_path: Path) -> LibraryRoots:
    symbols = tmp_path / "symbols"
    symbols.mkdir()
    return LibraryRoots(
        symbol_dirs=[symbols],
        footprint_dirs=[tmp_path / "footprints"],
    )


def _reference(spec: PartSpec) -> LandPatternResult:
    return compute_land_pattern(spec)


def _pad_tuples(
    reference: LandPatternResult,
) -> list[tuple[str, float, float, float, float, float]]:
    return [(pad.number, pad.x, pad.y, pad.width, pad.height, 0.0) for pad in reference.pads]


def test_candidates_classify_geometry_and_report_pin_one_and_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    base = tmp_path / "footprints" / "Package_SO.pretty"
    expected = _pad_tuples(reference)
    _write_footprint(
        base / "SOIC-4_3.9x4.9mm_P1.27mm.kicad_mod",
        "Exact",
        expected,
        model="${TEST_3DMODEL_DIR}/part.step",
    )
    compatible = [
        (number, x + 0.03, y, width + 0.02, height, rotation)
        for number, x, y, width, height, rotation in expected
    ]
    _write_footprint(
        base / "SOIC-4_3.9x4.9mm_P1.27mm_compatible.kicad_mod",
        "Compatible",
        compatible,
    )
    near = [
        (number, x + 0.1, y, width + 0.02, height, rotation)
        for number, x, y, width, height, rotation in expected
    ]
    _write_footprint(
        base / "SOIC-4_3.9x4.9mm_P1.27mm_near.kicad_mod",
        "Near",
        near,
    )
    wrong_pin1 = [
        (number, 0.1 if number == "1" else x, y, width, height, rotation)
        for number, x, y, width, height, rotation in expected
    ]
    _write_footprint(
        base / "SOIC-4_3.9x4.9mm_P1.27mm_pin1.kicad_mod",
        "WrongPinOne",
        wrong_pin1,
    )
    _write_footprint(
        base / "SOIC-6_3.9x4.9mm_P1.27mm.kicad_mod",
        "WrongCount",
        expected,
    )
    model_root = tmp_path / "models"
    model_root.mkdir()
    (model_root / "part.step").write_text("model", encoding="utf-8")
    monkeypatch.setenv("TEST_3DMODEL_DIR", str(model_root))

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
    )

    assert [candidate.classification for candidate in result.footprints] == [
        "exact",
        "compatible",
        "functional",
        "near",
    ]
    exact = result.footprints[0]
    assert exact.pin1_quadrant == "top_left"
    assert exact.pin1_quadrant_ok
    assert exact.models[0].resolved
    assert exact.models[0].resolved_path == str((model_root / "part.step").resolve())
    wrong_pin_one = next(
        candidate for candidate in result.footprints if candidate.name == "WrongPinOne"
    )
    assert not wrong_pin_one.pin1_quadrant_ok
    assert result.reference_source == "ipc7351b"
    assert len(result.part_spec_sha256) == 64


def test_candidate_geometry_accounts_for_rotation_and_caps_results(tmp_path: Path) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    footprint_dir = tmp_path / "footprints" / "Package_SO.pretty"
    footprint_dir.mkdir(parents=True)
    pads = [(pad.number, pad.x, pad.y, pad.height, pad.width, 90.0) for pad in reference.pads]
    for index in range(22):
        _write_footprint(
            footprint_dir / f"SOIC-4_3.9x4.9mm_P1.27mm_variant{index}.kicad_mod",
            f"Rotated{index}",
            pads,
        )

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
    )

    assert len(result.footprints) == 20
    assert {candidate.classification for candidate in result.footprints} == {"exact"}


def test_project_library_is_searched_and_models_can_be_unresolved(tmp_path: Path) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    project = tmp_path / "project" / "library"
    _write_footprint(
        project / "Other.pretty" / "SOIC-4_3.9x4.9mm_P1.27mm.kicad_mod",
        "ProjectFootprint",
        _pad_tuples(reference),
        model="${MISSING_MODEL_ROOT}/part.step",
    )

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=project,
        reference=reference,
    )

    assert len(result.footprints) == 1
    assert result.footprints[0].library_path == project / "Other.pretty"
    assert not result.footprints[0].models[0].resolved
    assert result.footprints[0].models[0].resolved_path is None


def test_chip_candidates_skip_pitch_and_pin_count_name_filters(tmp_path: Path) -> None:
    spec = _chip_spec()
    reference = compute_land_pattern(spec)
    roots = _roots(tmp_path)
    path = tmp_path / "footprints" / "Resistor_SMD.pretty" / "R_0603_1608Metric.kicad_mod"
    _write_footprint(path, "R_0603_1608Metric", _pad_tuples(reference))

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
    )

    assert len(result.footprints) == 1
    assert result.footprints[0].classification == "near"
    assert not result.footprints[0].functional.passed
    assert any(
        failure.startswith("lead_outside_pad:")
        for failure in result.footprints[0].functional.failures
    )
    assert result.footprints[0].pin1_quadrant == "left"
    assert result.footprints[0].pin1_quadrant_ok


def test_symbols_match_mpn_prefix_and_pin_maps(tmp_path: Path) -> None:
    spec = _spec()
    roots = _roots(tmp_path)
    symbol_dir = roots.symbol_dirs[0]
    exact = _symbol_text(
        "TPS62130",
        [("1", "EN"), ("2", "VIN"), ("3", "SW"), ("4", "GND")],
    )
    compatible = _symbol_text(
        "TPS62130_ALT",
        [("1", "~{EN}"), ("2", "VIN"), ("3", "SW"), ("4", "PG")],
    )
    wrong_numbers = _symbol_text(
        "TPS62130_WRONG",
        [("1", "EN"), ("2", "VIN"), ("3", "SW")],
    )
    _write_symbol_library(
        symbol_dir / "Power.kicad_sym",
        [exact, compatible, wrong_numbers],
    )

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=_reference(spec),
    )

    assert [(candidate.name, candidate.classification) for candidate in result.symbols] == [
        ("TPS62130", "exact"),
        ("TPS62130_ALT", "pin_compatible"),
    ]
    assert result.symbols[1].pin_name_differences == ["4: ['gnd'] != ['pg']"]


def test_symbol_search_includes_project_and_cern_roots(tmp_path: Path) -> None:
    spec = _spec()
    roots = _roots(tmp_path)
    cern = tmp_path / "cern" / "SchLib"
    cern.mkdir(parents=True)
    _write_symbol_library(
        cern / "Cern.kicad_sym",
        [
            _symbol_text(
                "TPS62130_CERN",
                [("1", "EN"), ("2", "VIN"), ("3", "SW"), ("4", "GND")],
            )
        ],
    )
    project = tmp_path / "project" / "library"
    project.mkdir(parents=True)
    _write_symbol_library(
        project / "Project.kicad_sym",
        [
            _symbol_text(
                "TPS62130_PROJECT",
                [("1", "EN"), ("2", "VIN"), ("3", "SW"), ("4", "GND")],
            )
        ],
    )
    roots = LibraryRoots(
        symbol_dirs=[roots.symbol_dirs[0], cern],
        footprint_dirs=roots.footprint_dirs,
    )

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=project,
        reference=_reference(spec),
    )

    assert {candidate.name for candidate in result.symbols} == {
        "TPS62130_CERN",
        "TPS62130_PROJECT",
    }


def test_organization_candidates_rank_before_official_within_class(
    tmp_path: Path,
) -> None:
    spec = _spec()
    reference = _reference(spec)
    base_roots = _roots(tmp_path)
    official = base_roots.footprint_dirs[0]
    manufacturer = tmp_path / "manufacturer"
    roots = LibraryRoots(
        symbol_dirs=base_roots.symbol_dirs,
        footprint_dirs=[official, manufacturer],
    )
    expected = _pad_tuples(reference)
    official_library = official / "Package_SO.pretty"
    manufacturer_library = manufacturer / "Vendor_SO.pretty"
    organization = tmp_path / "organization" / "library"
    project = tmp_path / "project" / "library"
    _write_footprint(
        official_library / "SOIC-4_3.9x4.9mm_P1.27mm_official.kicad_mod",
        "Official",
        expected,
    )
    _write_footprint(
        organization / "Package_SO.pretty" / "SOIC-4_3.9x4.9mm_P1.27mm_org.kicad_mod",
        "Organization",
        expected,
    )
    _write_footprint(
        project / "Project.pretty" / "SOIC-4_3.9x4.9mm_P1.27mm_project.kicad_mod",
        "Project",
        expected,
    )
    _write_footprint(
        manufacturer_library / "SOIC-4_3.9x4.9mm_P1.27mm_manufacturer.kicad_mod",
        "Manufacturer",
        expected,
    )

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=project,
        reference=reference,
        organization_dirs=[organization],
    )

    assert [candidate.origin for candidate in result.footprints] == [
        "organization",
        "project",
        "manufacturer",
        "kicad_official",
    ]
    assert result.footprints[0].name == "Organization"


def test_organization_tuned_candidate_ranks_ahead_of_official_exact(
    tmp_path: Path,
) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    pads = _pad_tuples(reference)
    _write_footprint(
        roots.footprint_dirs[0]
        / "Package_SO.pretty"
        / "SOIC-4_3.9x4.9mm_P1.27mm_official.kicad_mod",
        "Official",
        pads,
    )
    organization = tmp_path / "organization" / "library"
    tuned_pads = [
        (number, x + 0.06, y, width, height, rotation)
        for number, x, y, width, height, rotation in pads
    ]
    tuned_path = organization / "Package_SO.pretty" / ("SOIC-4_3.9x4.9mm_P1.27mm_tuned.kicad_mod")
    _write_footprint(tuned_path, "OrganizationTuned", tuned_pads)
    _write_candidate_lineage(
        tuned_path,
        organization,
        pads,
        layer="organization",
    )

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
        organization_dirs=[organization],
    )

    assert result.footprints[0].name == "OrganizationTuned"
    assert result.footprints[0].classification == "functional"
    assert result.footprints[0].functional.passed
    assert result.footprints[0].preferred_tuned
    official = next(candidate for candidate in result.footprints if candidate.name == "Official")
    assert official.classification == "exact"
    assert not official.preferred_tuned


def test_product_tuned_preference_requires_matching_product(tmp_path: Path) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    pads = _pad_tuples(reference)
    _write_footprint(
        roots.footprint_dirs[0]
        / "Package_SO.pretty"
        / "SOIC-4_3.9x4.9mm_P1.27mm_official.kicad_mod",
        "Official",
        pads,
    )
    organization = tmp_path / "organization" / "library"
    tuned_pads = [
        (number, x + 0.06, y, width, height, rotation)
        for number, x, y, width, height, rotation in pads
    ]
    tuned_path = organization / "Package_SO.pretty" / ("SOIC-4_3.9x4.9mm_P1.27mm_tuned.kicad_mod")
    _write_footprint(tuned_path, "ProductTuned", tuned_pads)
    _write_candidate_lineage(
        tuned_path,
        organization,
        pads,
        layer="product",
        product="controller-board",
    )

    matching = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
        organization_dirs=[organization],
        product="controller-board",
    )
    mismatching = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
        organization_dirs=[organization],
        product="other-board",
    )

    assert matching.footprints[0].name == "ProductTuned"
    assert matching.footprints[0].preferred_tuned
    assert mismatching.footprints[0].name == "Official"
    tuned_candidate = next(
        candidate for candidate in mismatching.footprints if candidate.name == "ProductTuned"
    )
    assert not tuned_candidate.preferred_tuned


def test_invalid_lineage_demotes_candidate_and_fails_functional_check(
    tmp_path: Path,
) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    organization = tmp_path / "organization" / "library"
    footprint_path = (
        organization / "Package_SO.pretty" / ("SOIC-4_3.9x4.9mm_P1.27mm_invalid.kicad_mod")
    )
    _write_footprint(footprint_path, "InvalidLineage", _pad_tuples(reference))
    sidecar = Path(f"{footprint_path}.lineage.json")
    sidecar.write_text("{not json", encoding="utf-8")

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
        organization_dirs=[organization],
    )

    candidate = result.footprints[0]
    assert candidate.classification == "near"
    assert not candidate.functional.passed
    assert any(failure.startswith("lineage_invalid:") for failure in candidate.functional.failures)
    assert not candidate.preferred_tuned


def test_geometrically_near_but_functional_candidate_gets_functional_class(
    tmp_path: Path,
) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    shifted = [
        (number, x + 0.06, y, width, height, rotation)
        for number, x, y, width, height, rotation in _pad_tuples(reference)
    ]
    path = (
        roots.footprint_dirs[0]
        / "Package_SO.pretty"
        / "SOIC-4_3.9x4.9mm_P1.27mm_functional.kicad_mod"
    )
    _write_footprint(path, "Functional", shifted)

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
    )

    candidate = result.footprints[0]
    assert candidate.classification == "functional"
    assert candidate.functional.passed
    assert candidate.functional.failures == []


def test_functional_failure_forces_near_and_lists_failures(tmp_path: Path) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    wrong_pad_set = [
        ("9" if number == "4" else number, x, y, width, height, rotation)
        for number, x, y, width, height, rotation in _pad_tuples(reference)
    ]
    _write_footprint(
        roots.footprint_dirs[0] / "Package_SO.pretty" / "SOIC-4_3.9x4.9mm_P1.27mm_wrong.kicad_mod",
        "WrongPadSet",
        wrong_pad_set,
    )

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
    )

    candidate = result.footprints[0]
    assert candidate.classification == "near"
    assert not candidate.functional.passed
    assert any(failure.startswith("pad_set:") for failure in candidate.functional.failures)


def test_valid_lineage_is_reported_on_organization_candidate(tmp_path: Path) -> None:
    spec = _spec()
    reference = _reference(spec)
    roots = _roots(tmp_path)
    organization_root = tmp_path / "organization"
    organization = organization_root / "library"
    footprint_path = organization / "Package_SO.pretty" / "SOIC-4_3.9x4.9mm_P1.27mm_org.kicad_mod"
    _write_footprint(footprint_path, "Organization", _pad_tuples(reference))
    base_path = organization_root / "base.kicad_mod"
    base_pads = [
        (number, x + (0.05 if number == "1" else 0.0), y, width, height, rotation)
        for number, x, y, width, height, rotation in _pad_tuples(reference)
    ]
    _write_footprint(base_path, "ManufacturerBase", base_pads)
    evidence_path = organization_root / "prototype.txt"
    evidence_path.write_text("prototype", encoding="utf-8")
    changes = pad_changes(parse_footprint(base_path), parse_footprint(footprint_path))
    lineage = FootprintLineage(
        artifact_kind="circuit_footprint_lineage",
        footprint_sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
        layer="product",
        product="controller-board",
        base=FootprintBase(
            kind="manufacturer",
            path="base.kicad_mod",
            sha256=hashlib.sha256(base_path.read_bytes()).hexdigest(),
        ),
        changes=changes,
        reason="validated prototype tuning",
        evidence=[
            EvidenceRef(
                kind="prototype",
                path="prototype.txt",
                sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            )
        ],
    )
    sidecar = Path(f"{footprint_path}.lineage.json")
    sidecar.write_text(lineage.model_dump_json(), encoding="utf-8")

    result = find_candidates(
        spec,
        roots=roots,
        project_library_dir=None,
        reference=reference,
        organization_dirs=[organization],
    )

    candidate = result.footprints[0]
    assert candidate.lineage_path == str(sidecar)
    assert candidate.lineage_layer == "product"
    assert candidate.lineage_product == "controller-board"
