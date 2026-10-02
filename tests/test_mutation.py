from __future__ import annotations

import ast
import re
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from circuit import kicad_cli, libverify, occt
from circuit.libitems import FootprintDef, PadDef, SymbolDef, SymPin, parse_symbol
from circuit.mutation import (
    CHECK_FAMILY,
    MUTATION_OPERATORS,
    MutationArtifacts,
    MutationError,
    MutationFinding,
    MutationFixture,
    family_for_code,
    library_mutation_fixture,
    library_verifier,
    run_mutations,
)
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    ExposedPad,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PartSpecReport,
    PinSpec,
    PinTable,
    Reading,
    part_spec_sha256,
)
from pinout_fixtures import geometry_for_names
from test_libverify import _vqfn_spec, _write_case  # pyright: ignore[reportPrivateUsage]

REPO_ROOT = Path(__file__).parents[1]
FINDING_MODULES = ("libverify", "partspec", "model3d", "ruleprofile", "libtestboard")
EXPECTED_OPERATORS = {
    "symbol_adjacent_pin_swap",
    "symbol_pin_name_swap",
    "symbol_pin_number_offset",
    "symbol_reversed_pin_order",
    "footprint_mirror_x",
    "footprint_mirror_y",
    "footprint_rotate_90",
    "footprint_rotate_180",
    "footprint_rotate_270",
    "footprint_pad_shift_0_1mm",
    "footprint_pitch_scale_1_02",
    "footprint_ep_size_delta_20_percent",
    "footprint_mm_to_inch",
    "footprint_inch_to_mm",
    "footprint_removed_pad",
    "footprint_duplicated_pad_number",
    "footprint_swapped_pad_numbers",
    "partspec_min_nom_max_column_shift",
    "partspec_drawing_view_flip",
    "partspec_pin1_corner_rotation",
    "partspec_sibling_package_mpn",
    "model_mirror_x",
    "model_rotate_90",
    "model_rotate_180",
    "model_offset_0_1mm",
    "model_scale_25_4",
    "model_removed_pin1_marker",
}


def _reading(value: str) -> Reading:
    return Reading(
        page=1,
        bbox=(1.0, 1.0, 2.0, 2.0),
        vision=value,
        vision_record="fixture.json",
    )


def _dimension(value: float) -> Dimension:
    return Dimension(nom=value, reading=_reading(str(value)))


def _fixture(tmp_path: Path, *, seed: int = 0) -> MutationFixture:
    package = PackageSpec(
        family="no_lead_quad",
        drawing_id="mutation-fixture",
        pin_count=4,
        pitch=_dimension(0.5),
        body_length=_dimension(3.0),
        body_width=_dimension(3.0),
        height=_dimension(0.8),
        lead_length=_dimension(0.3),
        lead_width=_dimension(0.25),
        exposed_pad=ExposedPad(
            number="5",
            length=_dimension(0.8),
            width=_dimension(0.8),
        ),
        pins_per_side=(1, 1, 1, 1),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin 1 top left"),
    )
    spec = PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="MUTATION-FIXTURE",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="fixture.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="fixture-extraction.json",
        ),
        package=package,
        pins=[
            PinSpec(
                number=str(index),
                name=f"PIN{index}",
                electrical_type="passive",
                reading=_reading(f"{index} PIN{index}"),
            )
            for index in range(1, 5)
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="MUTATION-FIXTURE",
                package_designator="QFN",
                pin_count=4,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("MUTATION-FIXTURE QFN"),
            )
        ],
    )
    pads = [
        PadDef(
            number=str(index),
            type="smd",
            shape="rect",
            x=x,
            y=y,
            rotation=0,
            width=0.5,
            height=0.3,
            drill=None,
            layers=["F.Cu", "F.Mask", "F.Paste"],
        )
        for index, (x, y) in enumerate(
            ((-1.3, -0.5), (0.5, -1.3), (1.3, 0.5), (-0.5, 1.3)),
            start=1,
        )
    ]
    pads.append(
        PadDef(
            number="5",
            type="smd",
            shape="rect",
            x=0,
            y=0,
            rotation=0,
            width=0.8,
            height=0.8,
            drill=None,
            layers=["F.Cu", "F.Mask", "F.Paste"],
        )
    )
    footprint = FootprintDef(
        name="mutation-fixture",
        attributes=["smd"],
        pads=pads,
        graphics=[],
        models=[],
        properties={},
    )
    symbol = SymbolDef(
        name="MUTATION-FIXTURE",
        pins=[
            SymPin(
                number=str(index),
                name=f"PIN{index}",
                electrical_type="passive",
                x=float(index),
                y=0.0,
                length=2.54,
                orientation=0.0,
                unit=1,
            )
            for index in range(1, 5)
        ],
        properties={},
    )
    body = occt.cylinder_cut(
        occt.box(-1.0, -1.0, 0.2, 2.0, 2.0, 0.6),
        x=-0.55,
        y=-0.55,
        top_z=0.8,
        radius=0.2,
        depth=0.05,
    )
    terminals = (
        occt.box(-1.4, -0.15, 0.0, 0.3, 0.3, 0.2),
        occt.box(-0.15, -1.4, 0.0, 0.3, 0.3, 0.2),
        occt.box(1.1, 0.35, 0.0, 0.3, 0.3, 0.2),
        occt.box(-0.65, 1.1, 0.0, 0.3, 0.3, 0.2),
        occt.box(-0.4, -0.4, 0.0, 0.8, 0.8, 0.2),
    )
    model = occt.compound((body, *terminals))
    model_path = tmp_path / "generated.step"
    occt.write_step(model, model_path, product_name=spec.mpn)
    artifacts = MutationArtifacts(
        spec=spec,
        symbol=symbol,
        footprint=footprint,
        model=occt.read_step(model_path),
        model_path=model_path,
    )

    def verify(mutated: MutationArtifacts) -> list[MutationFinding]:
        findings: list[MutationFinding] = []
        if mutated.model_path != artifacts.model_path:
            assert mutated.model_path.is_file()
            assert mutated.model_path.suffix == ".step"
            assert mutated.footprint == artifacts.footprint
            exported_volume = sum(
                solid.volume for solid in occt.inspect(occt.read_step(mutated.model_path)).solids
            )
            mutated_volume = sum(solid.volume for solid in occt.inspect(mutated.model).solids)
            assert exported_volume == pytest.approx(mutated_volume, rel=1e-8)
        elif mutated.footprint != artifacts.footprint:
            assert mutated.model_path == artifacts.model_path
            assert mutated.spec == artifacts.spec
        if mutated.symbol != artifacts.symbol:
            findings.extend(
                (
                    MutationFinding(code="symbol_pin_set", severity="error"),
                    MutationFinding(code="symbol_permutation_diagnosis", severity="error"),
                )
            )
        if mutated.footprint != artifacts.footprint:
            findings.extend(
                (
                    MutationFinding(code="pad_geometry", severity="error"),
                    MutationFinding(code="pin_pad_mapping", severity="error"),
                )
            )
        if mutated.spec != artifacts.spec:
            findings.extend(
                (
                    MutationFinding(code="mechanical_mismatch", severity="error"),
                    MutationFinding(code="pin1_location", severity="error"),
                )
            )
        if mutated.model is not artifacts.model:
            findings.extend(
                (
                    MutationFinding(code="model_body_dimension", severity="error"),
                    MutationFinding(code="model_export_mismatch", severity="error"),
                )
            )
        return findings

    return MutationFixture(artifacts=artifacts, verify=verify, seed=seed)


def _known_good_library_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: int = 0,
    run_export_oracle: bool = False,
) -> MutationFixture:
    spec = _vqfn_spec()
    lead_width = spec.package.lead_width
    assert lead_width is not None
    spec.package.lead_width = Dimension(
        min=lead_width.min,
        nom=0.24,
        max=lead_width.max,
        reading=_reading("0.24 mm"),
    )
    case = _write_case(
        tmp_path,
        monkeypatch,
        spec=spec,
        stub_cli=not run_export_oracle,
    )
    spec, _, spec_path, check_path, symbol_path, footprint_path = case
    exposed_pad = spec.package.exposed_pad
    assert exposed_pad is not None
    footprint_text = footprint_path.read_text(encoding="utf-8")
    footprint_text, paste_margin_count = re.subn(
        rf'(\(pad "{re.escape(exposed_pad.number)}".*?\(size [^)]+\))',
        r"\1 (solder_paste_margin -0.15)",
        footprint_text,
        count=1,
    )
    assert paste_margin_count == 1
    footprint_path.write_text(footprint_text, encoding="utf-8")
    assert spec.pinout is not None
    check = PartSpecReport(
        artifact_kind="circuit_part_spec_check",
        verdict="pass",
        part_spec_sha256=part_spec_sha256(spec_path),
        extraction_sha256="c" * 64,
        pdf_sha256=spec.datasheet.sha256,
        checked_readings=1,
        findings=[],
        pinout=geometry_for_names(
            spec.pinout.labels_vision,
            pin_count=spec.package.pin_count,
            topology="quad",
            page=spec.pinout.page,
        ),
    )
    check_path.write_text(check.model_dump_json(indent=2) + "\n", encoding="utf-8")
    monkeypatch.delenv("CIRCUIT_AUTHORING_LANE", raising=False)
    model_path = next((tmp_path / "models").rglob(f"{spec.package.drawing_id}.step"))
    return library_mutation_fixture(
        spec_path=spec_path,
        spec_check_path=check_path,
        symbol_lib=symbol_path,
        symbol_name=spec.mpn,
        footprint_path=footprint_path,
        model_path=model_path,
        work_dir=tmp_path / "real-verifier",
        run_export_oracle=run_export_oracle,
        seed=seed,
    )


def _finding_codes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        for keyword in node.keywords:
            if (
                keyword.arg == "code"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                codes.add(keyword.value.value)
        if (
            function_name == "_finding"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            codes.add(node.args[1].value)
    return codes


def test_every_static_verification_code_has_a_family() -> None:
    codes = {
        code
        for module in FINDING_MODULES
        for code in _finding_codes(REPO_ROOT / "src" / "circuit" / f"{module}.py")
    }
    assert codes - CHECK_FAMILY.keys() == set()
    assert family_for_code("testboard_drc_clearance") == "rule_profile"
    assert family_for_code("testboard_erc_pin_not_connected") == "pin_bijection"
    assert family_for_code("footprint_chirality_mismatch") == "orientation"
    assert family_for_code("footprint_order_mismatch") == "orientation"
    assert family_for_code("footprint_rotation_mismatch") == "orientation"
    assert family_for_code("F6.3") == "land_geometry"
    with pytest.raises(MutationError, match="unmapped verification finding code"):
        family_for_code("new_unmapped_finding")


def test_mutation_operators_use_real_verifier_and_match_expected_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=41)
    first = run_mutations(fixture)

    assert {operator.name for operator in MUTATION_OPERATORS} == EXPECTED_OPERATORS
    assert len(first.outcomes) == len(MUTATION_OPERATORS)
    assert first.baseline_findings == ["pin_source_single"]
    assert first.excluded_vision_findings > 0
    assert first.export_oracle_run is False
    assert first.passed is False
    assert all(outcome.finding_codes for outcome in first.outcomes)
    assert all(outcome.mutation.critical for outcome in first.outcomes)
    assert {outcome.mutation.operator: tuple(outcome.families) for outcome in first.outcomes} == {
        # This non-Docker matrix intentionally records the production verifier's
        # exact coverage without KiCad's export oracle.
        "symbol_adjacent_pin_swap": ("orientation", "pin_bijection"),
        "symbol_pin_name_swap": ("orientation", "pin_bijection"),
        "symbol_pin_number_offset": ("orientation", "pin_bijection"),
        "symbol_reversed_pin_order": ("orientation", "pin_bijection"),
        "footprint_mirror_x": ("land_geometry", "model_geometry", "orientation"),
        "footprint_mirror_y": ("land_geometry", "model_geometry", "orientation"),
        "footprint_rotate_90": ("land_geometry", "model_geometry", "orientation"),
        "footprint_rotate_180": ("land_geometry", "model_geometry", "orientation"),
        "footprint_rotate_270": ("land_geometry", "model_geometry", "orientation"),
        "footprint_pad_shift_0_1mm": ("land_geometry", "model_geometry"),
        "footprint_pitch_scale_1_02": ("land_geometry", "model_geometry"),
        "footprint_ep_size_delta_20_percent": ("land_geometry",),
        "footprint_mm_to_inch": ("land_geometry", "model_geometry"),
        "footprint_inch_to_mm": ("land_geometry", "model_geometry"),
        "footprint_removed_pad": (
            "land_geometry",
            "model_geometry",
            "orientation",
            "pin_bijection",
        ),
        "footprint_duplicated_pad_number": (
            "land_geometry",
            "model_geometry",
            "orientation",
            "pin_bijection",
        ),
        "footprint_swapped_pad_numbers": ("land_geometry", "model_geometry", "orientation"),
        "partspec_min_nom_max_column_shift": (
            "evidence",
            "land_geometry",
            "model_geometry",
        ),
        "partspec_drawing_view_flip": ("evidence", "model_geometry"),
        "partspec_pin1_corner_rotation": ("evidence", "model_geometry"),
        "partspec_sibling_package_mpn": ("evidence", "model_geometry"),
        "model_mirror_x": ("model_geometry",),
        "model_rotate_90": ("model_geometry",),
        "model_rotate_180": ("model_geometry",),
        "model_offset_0_1mm": ("model_geometry",),
        "model_scale_25_4": ("model_geometry",),
        "model_removed_pin1_marker": ("model_geometry",),
    }


def test_symbol_mutations_are_serialized_and_reach_real_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=41)
    original_verify = libverify.verify_library_part
    called: list[Path] = []

    def capture_verify(*args: object, **kwargs: object) -> libverify.LibraryVerification:
        called.append(cast(Path, kwargs["symbol_lib"]))
        return original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(libverify, "verify_library_part", capture_verify)
    assert not [
        finding for finding in fixture.verify(fixture.artifacts) if finding.severity == "error"
    ]

    expected = {
        "symbol_adjacent_pin_swap": {
            "symbol_pin_name",
            "symbol_pinout_name_mismatch",
        },
        "symbol_pin_name_swap": {
            "symbol_pin_name",
            "symbol_pinout_name_mismatch",
        },
        "symbol_reversed_pin_order": {
            "symbol_pin_name",
            "symbol_pinout_name_mismatch",
        },
    }
    operators = {operator.name: operator for operator in MUTATION_OPERATORS}
    for name, expected_codes in expected.items():
        mutated, _ = operators[name].apply(fixture.artifacts, fixture.seed)
        findings = list(fixture.verify(mutated))
        codes = {finding.code for finding in findings}
        assert expected_codes <= codes
        assert {family_for_code(code) for code in expected_codes} == {
            "pin_bijection",
            "orientation",
        }
        staged_symbol = called[-1]
        parsed = parse_symbol(staged_symbol, fixture.artifacts.symbol.name)
        assert parsed == mutated.symbol

    assert len(called) == 4
    assert len({path.parent for path in called}) == len(called)


def test_symbol_mutations_reject_unchanged_number_name_maps(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, seed=9)
    numeric_indices = [
        index for index, pin in enumerate(fixture.artifacts.symbol.pins) if pin.number.isdecimal()
    ]
    assert len(numeric_indices) >= 3
    operators = {operator.name: operator for operator in MUTATION_OPERATORS}

    duplicated_number_pins = list(fixture.artifacts.symbol.pins)
    for index in numeric_indices:
        duplicated_number_pins[index] = duplicated_number_pins[index].model_copy(
            update={"number": "1", "name": f"PIN{index}"}
        )
    duplicate_number_artifacts = replace(
        fixture.artifacts,
        symbol=fixture.artifacts.symbol.model_copy(update={"pins": duplicated_number_pins}),
    )
    for name in ("symbol_adjacent_pin_swap", "symbol_pin_name_swap"):
        with pytest.raises(MutationError):
            operators[name].apply(duplicate_number_artifacts, fixture.seed)

    reversed_pins = list(fixture.artifacts.symbol.pins)
    for offset, index in enumerate(numeric_indices):
        mirror = min(offset, len(numeric_indices) - 1 - offset)
        reversed_pins[index] = reversed_pins[index].model_copy(
            update={
                "number": str(offset + 1),
                "name": "PIN_A" if mirror != 1 else "PIN_B",
            }
        )
    reversed_artifacts = replace(
        fixture.artifacts,
        symbol=fixture.artifacts.symbol.model_copy(update={"pins": reversed_pins}),
    )
    with pytest.raises(MutationError, match="number-to-name map"):
        operators["symbol_reversed_pin_order"].apply(reversed_artifacts, fixture.seed)


def test_library_verifier_fails_closed_without_kicad_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch, seed=9)
    monkeypatch.setattr(kicad_cli, "command_prefix", lambda: ["/missing/kicad-cli"])
    verifier = library_verifier(
        work_dir=tmp_path / "missing-cli",
        run_export_oracle=True,
    )

    with pytest.raises(MutationError, match="kicad-cli is unavailable"):
        verifier(fixture.artifacts)


def test_single_family_critical_mutations_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, seed=9)

    def one_family_verifier(artifacts: MutationArtifacts) -> list[MutationFinding]:
        if artifacts == fixture.artifacts:
            return []
        return [MutationFinding(code="model_body_dimension", severity="error")]

    report = run_mutations(
        MutationFixture(
            artifacts=fixture.artifacts,
            verify=one_family_verifier,
            seed=fixture.seed,
        )
    )

    assert report.passed is False
    assert len(report.single_oracle) == len(MUTATION_OPERATORS)
    assert report.undetected == []
    assert all(outcome.status == "single_oracle" for outcome in report.outcomes)


def test_mutation_operators_change_their_declared_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _known_good_library_fixture(tmp_path, monkeypatch)
    for operator in MUTATION_OPERATORS:
        mutated, record = operator.apply(fixture.artifacts, 18)
        assert record.operator == operator.name
        assert record.target == operator.target
        assert mutated != fixture.artifacts
