from __future__ import annotations

import ast
from pathlib import Path

import pytest

from circuit import occt
from circuit.libitems import FootprintDef, PadDef, SymbolDef, SymPin
from circuit.mutation import (
    CHECK_FAMILY,
    MUTATION_OPERATORS,
    MutationArtifacts,
    MutationError,
    MutationFinding,
    MutationFixture,
    family_for_code,
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
    PinSpec,
    PinTable,
    Reading,
)

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
    assert family_for_code("F6.3") == "land_geometry"
    with pytest.raises(MutationError, match="unmapped verification finding code"):
        family_for_code("new_unmapped_finding")


def test_mutation_operators_are_seeded_typed_and_reach_two_families(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, seed=41)
    first = run_mutations(fixture)
    second = run_mutations(fixture)

    assert {operator.name for operator in MUTATION_OPERATORS} == EXPECTED_OPERATORS
    assert first.model_dump() == second.model_dump()
    assert len(first.outcomes) == len(MUTATION_OPERATORS)
    assert first.passed is True
    assert first.single_oracle == []
    assert first.undetected == []
    assert all(outcome.status == "detected" for outcome in first.outcomes)
    assert all(outcome.non_vision_family_count >= 2 for outcome in first.outcomes)
    assert all(outcome.finding_codes for outcome in first.outcomes)
    assert all(outcome.mutation.critical for outcome in first.outcomes)


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


def test_mutation_operators_change_their_declared_target(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    for operator in MUTATION_OPERATORS:
        mutated, record = operator.apply(fixture.artifacts, 18)
        assert record.operator == operator.name
        assert record.target == operator.target
        assert mutated != fixture.artifacts
