import hashlib
from pathlib import Path

import pytest

from circuit import humanrequest, occt
from circuit import libverify as libverify_module
from circuit.landpattern import compute_land_pattern
from circuit.libitems import ModelRef, SymbolDef, parse_footprint
from circuit.libwriter import write_footprint
from circuit.mutation import (
    MUTATION_OPERATORS,
    MutationArtifacts,
    family_for_code,
)
from circuit.partspec import load_part_spec, part_spec_sha256
from connector_fixtures import connector_spec

_CASES = (
    ("connector_numbering_mirror", "kona_mirrored"),
    ("tht_drill_shrink", "header_2x5"),
    ("npth_to_pth", "jst_ph_tht"),
    ("mounting_pad_drop", "jst_ph_right_angle"),
    ("board_edge_offset_shift", "jst_ph_right_angle"),
    ("mating_axis_flip", "jst_ph_right_angle"),
)


def _artifacts(tmp_path: Path, kind: str) -> MutationArtifacts:
    spec_path = tmp_path / f"{kind}.part.spec.json"
    spec_path.write_text(connector_spec(kind).model_dump_json(indent=2) + "\n", encoding="utf-8")
    spec = load_part_spec(spec_path)
    footprint_path = tmp_path / f"{kind}.kicad_mod"
    write_footprint(
        spec,
        compute_land_pattern(spec),
        footprint_path,
        spec_sha256=part_spec_sha256(spec_path),
    )
    model = occt.box(-1.0, -1.0, 0.0, 2.0, 2.0, 1.0)
    model_path = tmp_path / f"{kind}.step"
    occt.write_step(model, model_path, product_name=kind)
    return MutationArtifacts(
        spec=spec,
        symbol=SymbolDef(name=spec.mpn, pins=[], properties={}),
        footprint=parse_footprint(footprint_path),
        model=model,
        model_path=model_path,
    )


@pytest.mark.parametrize(("operator_name", "fixture_kind"), _CASES)
def test_connector_mutations_reach_two_counting_oracle_families(
    tmp_path: Path,
    operator_name: str,
    fixture_kind: str,
) -> None:
    artifacts = _artifacts(tmp_path, fixture_kind)
    operator = next(item for item in MUTATION_OPERATORS if item.name == operator_name)
    mutated, _mutation = operator.apply(artifacts, seed=17)
    findings: list[libverify_module.VerifyFinding] = []
    human_requests: list[humanrequest.HumanRequest] = []
    reference = compute_land_pattern(mutated.spec)
    libverify_module._check_connector(  # pyright: ignore[reportPrivateUsage]
        mutated.spec,
        mutated.footprint,
        reference,
        findings,
        human_requests,
    )
    libverify_module._check_connector_model_features(  # pyright: ignore[reportPrivateUsage]
        mutated.spec,
        mutated.footprint,
        findings,
        "0" * 64,
    )
    if operator_name == "mating_axis_flip":
        model_hash = hashlib.sha256(mutated.model_path.read_bytes()).hexdigest()
        libverify_module._verify_model_geometry(  # pyright: ignore[reportPrivateUsage]
            mutated.spec,
            mutated.footprint,
            tmp_path / "synthetic.kicad_mod",
            ModelRef(
                path=mutated.model_path.name,
                offset=(0.0, 0.0, 0.0),
                scale=(1.0, 1.0, 1.0),
                rotate=(0.0, 0.0, 0.0),
            ),
            mutated.model_path,
            model_hash,
            0.1,
            findings,
        )

    families = {family_for_code(item.code) for item in findings}
    assert (
        len(
            families.intersection(
                {"pin_bijection", "orientation", "land_geometry", "model_geometry"}
            )
        )
        >= 2
    )
