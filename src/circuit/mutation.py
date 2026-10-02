"""Seeded library-artifact mutations and independent-oracle accounting."""

from __future__ import annotations

import hashlib
import math
import random
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from . import occt
from .libitems import FootprintDef, GraphicDef, PadDef, SymbolDef, SymPin
from .partspec import Dimension, PartSpec

CheckFamily = Literal[
    "evidence",
    "pin_bijection",
    "orientation",
    "land_geometry",
    "export_oracle",
    "model_geometry",
    "rule_profile",
    "vision",
]
MutationTarget = Literal["symbol", "footprint", "part_spec", "model"]
MutationStatus = Literal["detected", "single_oracle", "undetected"]
_NON_VISION_FAMILIES: tuple[CheckFamily, ...] = (
    "evidence",
    "pin_bijection",
    "orientation",
    "land_geometry",
    "export_oracle",
    "model_geometry",
    "rule_profile",
)


_FAMILY_CODES: dict[CheckFamily, tuple[str, ...]] = {
    "evidence": (
        "authoring_consensus_violated",
        "authoring_disagreement",
        "authoring_invalid",
        "authoring_missing",
        "authoring_models_not_diverse",
        "bbox_missing",
        "bbox_outside_page",
        "bbox_too_large",
        "cell_header_mismatch",
        "cell_label_mismatch",
        "cell_unit_mismatch",
        "cell_value_mismatch",
        "datasheet_missing",
        "datasheet_sha_mismatch",
        "drawing_id_missing",
        "drawing_revision_missing",
        "evidence_missing",
        "exposed_pad_table_mismatch",
        "evidence_sha_mismatch",
        "extraction_stale",
        "glyph_loss",
        "height_nonpositive",
        "invisible_text",
        "kind_mismatch",
        "lineage_base",
        "lineage_evidence",
        "lineage_footprint_hash",
        "lineage_invalid",
        "lineage_stale_change",
        "lineage_unrecorded_change",
        "mechanical_mismatch",
        "mechanical_single_lane",
        "orderable_designator_mismatch",
        "orderable_mpn_mismatch",
        "package_variant_unbound",
        "page_not_extracted",
        "part_spec_unchecked",
        "pin_reading_page_mismatch",
        "provenance_missing",
        "provenance_sha_mismatch",
        "reading_order_divergence",
        "redistribution_review",
        "rederivation_failed",
        "stacked_limit_order",
        "table_lane_disagreement",
        "value_mismatch",
    ),
    "pin_bijection": (
        "duplicate_pin",
        "exposed_pad_unmapped",
        "kicad_parse",
        "orderable_pin_count_mismatch",
        "pad_set",
        "pin_count_family",
        "pin_count_mismatch",
        "pin_numbering_incomplete",
        "pin_pad_mapping",
        "pin_table_bijection",
        "pin_table_column_ambiguous",
        "pin_table_column_mismatch",
        "pin_table_missing",
        "pinout_missing",
        "symbol_pin_grid",
        "symbol_pin_set",
        "symbol_pin_type",
        "symbol_property",
        "testboard_erc",
        "testboard_pinmap",
    ),
    "orientation": (
        "pin1_location",
        "pin1_mismatch",
        "pin1_unparseable",
        "pinout_name_mismatch",
        "pinout_permutation_diagnosis",
        "pinout_pin1_corner_mismatch",
        "pinout_unverified",
        "pinout_view_unverified",
        "pinout_winding_nonstandard",
        "symbol_permutation_diagnosis",
        "symbol_pin_name",
        "symbol_pinout_name_mismatch",
    ),
    "land_geometry": (
        "courtyard_enclosure",
        "courtyard_missing",
        "ep_pad_clearance",
        "exposed_pad_exceeds_body",
        "exposed_pad_size",
        "fab_outline",
        "fp_attribute",
        "lead_outside_pad",
        "lead_width_exceeds_pad",
        "pad_clearance",
        "pad_geometry",
        "pad_type",
        "pitch_exceeds_body",
        "silk_over_pad",
    ),
    "export_oracle": (
        "assembly_attribute",
        "model_export_mismatch",
        "model_export_missing",
        "model_export_unavailable",
        "testboard_kicad_cli",
        "testboard_pad_readback",
        "testboard_setup",
        "testboard_unavailable",
        "testboard_output_unavailable",
        "verification_output_unavailable",
    ),
    "model_geometry": (
        "model_body_dimension",
        "model_courtyard",
        "model_fab_outline",
        "model_footprint_unavailable",
        "model_format",
        "model_height",
        "model_inspection_unavailable",
        "model_invalid",
        "model_manifest_invalid",
        "model_missing",
        "model_pad_unmatched",
        "model_pin1_mismatch",
        "model_pin1_unverifiable",
        "model_pitch",
        "model_roundtrip",
        "model_terminal_mismatch",
        "model_terminal_outside_pad",
        "model_terminal_unmatched",
        "model_terminals_unseparable",
        "model_transform_not_identity",
        "model_unresolved",
    ),
    "rule_profile": (
        "ep_paste_coverage",
        "intentional_tuning",
        "mask_web",
        "missing_parent",
        "invalid",
        "rationale",
        "evidence",
        "paste_coverage",
        "testboard_drc",
        "rule_profile_cycle",
        "parent_hash",
        "layer_order",
    ),
    "vision": (
        "glyph_loss_ambiguous",
        "pinout_vision_mismatch",
        "vision_compare_mismatch",
        "vision_compare_stale",
        "vision_control_failed",
        "vision_impression_missing",
        "vision_not_observed",
        "vision_observation_log_missing",
        "vision_pinout_mismatch",
        "vision_read_invalid",
        "vision_read_kind_mismatch",
        "vision_read_mismatch",
        "vision_read_missing",
        "vision_read_not_observed",
        "vision_read_region_mismatch",
        "vision_record_mismatch",
        "vision_record_missing",
        "vision_table_mismatch",
        "vision_unparseable",
    ),
}


def _build_check_family() -> dict[str, CheckFamily]:
    result: dict[str, CheckFamily] = {}
    for family, codes in _FAMILY_CODES.items():
        for code in codes:
            if code in result:
                raise RuntimeError(f"finding code {code!r} has multiple oracle families")
            result[code] = family
    return result


CHECK_FAMILY: dict[str, CheckFamily] = _build_check_family()
_DYNAMIC_KLC_CODE = re.compile(r"^[FW]\d+(?:\.\d+)+$")


class MutationError(ValueError):
    """Raised when a mutation cannot be applied or a finding is unmapped."""


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operator: str
    target: MutationTarget
    params: dict[str, str | int | float | bool]
    critical: bool
    seed: int


class MutationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: Literal["error", "warning", "info"]


class MutationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mutation: Mutation
    finding_codes: list[str]
    families: list[CheckFamily]
    non_vision_family_count: int
    status: MutationStatus

    @model_validator(mode="after")
    def enforce_family_gate(self) -> MutationOutcome:
        actual_count = len(set(self.families).intersection(_NON_VISION_FAMILIES))
        if actual_count != self.non_vision_family_count:
            raise ValueError("non_vision_family_count does not match families")
        if self.status == "single_oracle" and (
            not self.mutation.critical or not self.families or actual_count >= 2
        ):
            raise ValueError(
                "single_oracle requires a critical mutation with fewer than two families"
            )
        if self.status == "detected" and not self.families:
            raise ValueError("detected mutations must have at least one oracle family")
        if self.status == "detected" and self.mutation.critical and actual_count < 2:
            raise ValueError("critical mutations require two non-vision oracle families")
        if self.status == "undetected" and self.families:
            raise ValueError("undetected mutations cannot have oracle families")
        return self


class MutationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_mutation_report"] = "circuit_mutation_report"
    seed: int
    baseline_findings: list[str]
    outcomes: list[MutationOutcome]
    family_detection_rates: dict[CheckFamily, float]
    single_oracle: list[str]
    undetected: list[str]
    passed: bool

    @model_validator(mode="after")
    def enforce_report_gate(self) -> MutationReport:
        expected_single = [
            outcome.mutation.operator
            for outcome in self.outcomes
            if outcome.status == "single_oracle"
        ]
        expected_undetected = [
            outcome.mutation.operator for outcome in self.outcomes if outcome.status == "undetected"
        ]
        expected_pass = not expected_single and not expected_undetected
        if self.single_oracle != expected_single or self.undetected != expected_undetected:
            raise ValueError("mutation report failure lists do not match operator outcomes")
        if self.passed is not expected_pass:
            raise ValueError("mutation report passed flag does not satisfy the two-family gate")
        return self


@dataclass(frozen=True)
class MutationArtifacts:
    spec: PartSpec
    symbol: SymbolDef
    footprint: FootprintDef
    model: occt.Shape
    model_path: Path


MutationVerifier = Callable[[MutationArtifacts], Sequence[MutationFinding]]


@dataclass(frozen=True)
class MutationFixture:
    artifacts: MutationArtifacts
    verify: MutationVerifier
    seed: int = 0


MutationTransform = Callable[
    [MutationArtifacts, random.Random],
    tuple[MutationArtifacts, dict[str, str | int | float | bool]],
]


@dataclass(frozen=True)
class MutationOperator:
    name: str
    target: MutationTarget
    critical: bool
    transform: MutationTransform

    def apply(self, artifacts: MutationArtifacts, seed: int) -> tuple[MutationArtifacts, Mutation]:
        operator_seed = int.from_bytes(
            hashlib.sha256(f"{seed}:{self.name}".encode()).digest()[:8],
            "big",
        )
        mutated, params = self.transform(artifacts, random.Random(operator_seed))
        return mutated, Mutation(
            operator=self.name,
            target=self.target,
            params=params,
            critical=self.critical,
            seed=seed,
        )


def family_for_code(code: str) -> CheckFamily:
    family = CHECK_FAMILY.get(code)
    if family is not None:
        return family
    if code.startswith("testboard_drc_"):
        return "rule_profile"
    if code.startswith("testboard_erc_"):
        return "pin_bijection"
    if _DYNAMIC_KLC_CODE.fullmatch(code):
        return "land_geometry"
    raise MutationError(f"unmapped verification finding code: {code}")


def _replace_symbol_pins(
    artifacts: MutationArtifacts,
    pins: list[SymPin],
) -> MutationArtifacts:
    symbol = artifacts.symbol.model_copy(update={"pins": pins})
    return MutationArtifacts(
        artifacts.spec, symbol, artifacts.footprint, artifacts.model, artifacts.model_path
    )


def _numeric_symbol_indices(symbol: SymbolDef) -> list[int]:
    indexed = [
        (int(pin.number), index) for index, pin in enumerate(symbol.pins) if pin.number.isdecimal()
    ]
    return [index for _, index in sorted(indexed)]


def _symbol_adjacent_pin_swap(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    indices = _numeric_symbol_indices(artifacts.symbol)
    if len(indices) < 2:
        raise MutationError("symbol requires at least two numeric pins")
    offset = rng.randrange(len(indices) - 1)
    left, right = indices[offset], indices[offset + 1]
    pins = list(artifacts.symbol.pins)
    left_pin, right_pin = pins[left], pins[right]
    pins[left] = left_pin.model_copy(update={"x": right_pin.x, "y": right_pin.y})
    pins[right] = right_pin.model_copy(update={"x": left_pin.x, "y": left_pin.y})
    return _replace_symbol_pins(artifacts, pins), {
        "first_pin": left_pin.number,
        "second_pin": right_pin.number,
    }


def _symbol_pin_name_swap(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    indices = _numeric_symbol_indices(artifacts.symbol)
    if len(indices) < 2:
        raise MutationError("symbol requires at least two numeric pins")
    offset = rng.randrange(len(indices) - 1)
    left, right = indices[offset], indices[offset + 1]
    pins = list(artifacts.symbol.pins)
    left_pin, right_pin = pins[left], pins[right]
    pins[left] = left_pin.model_copy(update={"name": right_pin.name})
    pins[right] = right_pin.model_copy(update={"name": left_pin.name})
    return _replace_symbol_pins(artifacts, pins), {
        "first_pin": left_pin.number,
        "second_pin": right_pin.number,
    }


def _symbol_pin_number_offset(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    pins: list[SymPin] = []
    for pin in artifacts.symbol.pins:
        if pin.number.isdecimal():
            pins.append(pin.model_copy(update={"number": str(int(pin.number) + 1)}))
        else:
            pins.append(pin)
    return _replace_symbol_pins(artifacts, pins), {"offset": 1}


def _symbol_reversed_pin_order(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    indices = _numeric_symbol_indices(artifacts.symbol)
    if len(indices) < 2:
        raise MutationError("symbol requires at least two numeric pins")
    pins = list(artifacts.symbol.pins)
    positions = [(pins[index].x, pins[index].y) for index in indices][::-1]
    for index, (x, y) in zip(indices, positions, strict=True):
        pins[index] = pins[index].model_copy(update={"x": x, "y": y})
    return _replace_symbol_pins(artifacts, pins), {"pin_count": len(indices)}


def _transform_footprint(
    artifacts: MutationArtifacts,
    pads: list[PadDef],
    graphics: list[GraphicDef] | None = None,
) -> MutationArtifacts:
    footprint = artifacts.footprint.model_copy(
        update={
            "pads": pads,
            "graphics": graphics if graphics is not None else artifacts.footprint.graphics,
        }
    )
    return MutationArtifacts(
        artifacts.spec, artifacts.symbol, footprint, artifacts.model, artifacts.model_path
    )


def _footprint_reflection(
    axis: Literal["x", "y"],
) -> MutationTransform:
    def transform(
        artifacts: MutationArtifacts,
        _rng: random.Random,
    ) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
        if axis == "x":
            pads = [
                pad.model_copy(update={"x": -pad.x, "rotation": (180 - pad.rotation) % 360})
                for pad in artifacts.footprint.pads
            ]
            graphics = [
                graphic.model_copy(update={"points": [(-x, y) for x, y in graphic.points]})
                for graphic in artifacts.footprint.graphics
            ]
        else:
            pads = [
                pad.model_copy(update={"y": -pad.y, "rotation": (-pad.rotation) % 360})
                for pad in artifacts.footprint.pads
            ]
            graphics = [
                graphic.model_copy(update={"points": [(x, -y) for x, y in graphic.points]})
                for graphic in artifacts.footprint.graphics
            ]
        return _transform_footprint(artifacts, pads, graphics), {"axis": axis}

    return transform


def _footprint_rotation(angle: int) -> MutationTransform:
    def transform(
        artifacts: MutationArtifacts,
        _rng: random.Random,
    ) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
        radians = math.radians(angle)

        def rotate(x: float, y: float) -> tuple[float, float]:
            return (
                math.cos(radians) * x - math.sin(radians) * y,
                math.sin(radians) * x + math.cos(radians) * y,
            )

        pads = [
            pad.model_copy(
                update={
                    "x": rotate(pad.x, pad.y)[0],
                    "y": rotate(pad.x, pad.y)[1],
                    "rotation": (pad.rotation + angle) % 360,
                }
            )
            for pad in artifacts.footprint.pads
        ]
        graphics = [
            graphic.model_copy(update={"points": [rotate(x, y) for x, y in graphic.points]})
            for graphic in artifacts.footprint.graphics
        ]
        return _transform_footprint(artifacts, pads, graphics), {"rotation_deg": angle}

    return transform


def _select_pad_indices(
    artifacts: MutationArtifacts,
    rng: random.Random,
    *,
    count: int,
    exclude_exposed: bool = False,
) -> list[int]:
    exposed_number = (
        artifacts.spec.package.exposed_pad.number
        if artifacts.spec.package.exposed_pad is not None
        else None
    )
    choices = [
        index
        for index, pad in enumerate(artifacts.footprint.pads)
        if pad.number and not (exclude_exposed and pad.number == exposed_number)
    ]
    if len(choices) < count:
        raise MutationError(f"footprint requires at least {count} eligible pads")
    return rng.sample(choices, count)


def _footprint_pad_shift(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    index = _select_pad_indices(artifacts, rng, count=1, exclude_exposed=True)[0]
    pads = list(artifacts.footprint.pads)
    pad = pads[index]
    pads[index] = pad.model_copy(update={"x": pad.x + 0.1})
    return _transform_footprint(artifacts, pads), {"pad": pad.number, "dx_mm": 0.1}


def _footprint_pitch_scale(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    exposed_number = (
        artifacts.spec.package.exposed_pad.number
        if artifacts.spec.package.exposed_pad is not None
        else None
    )
    pads = [
        pad.model_copy(update={"x": pad.x * 1.02, "y": pad.y * 1.02})
        if pad.number != exposed_number
        else pad
        for pad in artifacts.footprint.pads
    ]
    return _transform_footprint(artifacts, pads), {"scale": 1.02}


def _footprint_ep_size(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    exposed = artifacts.spec.package.exposed_pad
    if exposed is None:
        raise MutationError("PartSpec has no exposed pad")
    index = next(
        (
            index
            for index, pad in enumerate(artifacts.footprint.pads)
            if pad.number == exposed.number
        ),
        None,
    )
    if index is None:
        raise MutationError("footprint has no pad matching the exposed-pad number")
    factor = rng.choice((0.8, 1.2))
    pads = list(artifacts.footprint.pads)
    pad = pads[index]
    pads[index] = pad.model_copy(
        update={"width": pad.width * factor, "height": pad.height * factor}
    )
    return _transform_footprint(artifacts, pads), {"pad": pad.number, "scale": factor}


def _footprint_unit_scale(factor: float) -> MutationTransform:
    def transform(
        artifacts: MutationArtifacts,
        _rng: random.Random,
    ) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
        pads = [
            pad.model_copy(
                update={
                    "x": pad.x * factor,
                    "y": pad.y * factor,
                    "width": pad.width * factor,
                    "height": pad.height * factor,
                    "drill": pad.drill * factor if pad.drill is not None else None,
                    "paste_margin": (
                        pad.paste_margin * factor if pad.paste_margin is not None else None
                    ),
                    "mask_margin": (
                        pad.mask_margin * factor if pad.mask_margin is not None else None
                    ),
                }
            )
            for pad in artifacts.footprint.pads
        ]
        graphics = [
            graphic.model_copy(
                update={
                    "points": [(x * factor, y * factor) for x, y in graphic.points],
                    "width": graphic.width * factor,
                }
            )
            for graphic in artifacts.footprint.graphics
        ]
        return _transform_footprint(artifacts, pads, graphics), {"scale": factor}

    return transform


def _footprint_remove_pad(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    index = _select_pad_indices(artifacts, rng, count=1, exclude_exposed=True)[0]
    pads = list(artifacts.footprint.pads)
    removed = pads.pop(index)
    return _transform_footprint(artifacts, pads), {"pad": removed.number}


def _footprint_duplicate_pad_number(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    first, second = _select_pad_indices(artifacts, rng, count=2, exclude_exposed=True)
    pads = list(artifacts.footprint.pads)
    original = pads[second].number
    pads[second] = pads[second].model_copy(update={"number": pads[first].number})
    return _transform_footprint(artifacts, pads), {
        "source_pad": pads[first].number,
        "renumbered_pad": original,
    }


def _footprint_swap_pad_numbers(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    first, second = _select_pad_indices(artifacts, rng, count=2, exclude_exposed=True)
    pads = list(artifacts.footprint.pads)
    left, right = pads[first], pads[second]
    pads[first] = left.model_copy(update={"number": right.number})
    pads[second] = right.model_copy(update={"number": left.number})
    return _transform_footprint(artifacts, pads), {
        "first_pad": left.number,
        "second_pad": right.number,
    }


def _part_spec_package_update(
    artifacts: MutationArtifacts,
    updates: dict[str, object],
) -> MutationArtifacts:
    package = artifacts.spec.package.model_copy(update=updates)
    spec = artifacts.spec.model_copy(update={"package": package})
    return MutationArtifacts(
        spec, artifacts.symbol, artifacts.footprint, artifacts.model, artifacts.model_path
    )


def _part_spec_column_shift(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    fields = (
        "body_length",
        "body_width",
        "height",
        "pitch",
        "standoff",
        "lead_span",
        "lead_length",
        "lead_width",
    )
    available = [
        field
        for field in fields
        if (dimension := getattr(artifacts.spec.package, field, None)) is not None
        and any(value is not None for value in (dimension.min, dimension.nom, dimension.max))
    ]
    if not available:
        raise MutationError("PartSpec has no populated mechanical dimension")
    field = rng.choice(available)
    dimension = getattr(artifacts.spec.package, field)
    if not isinstance(dimension, Dimension):
        raise MutationError(f"PartSpec dimension {field} is unavailable")
    shifted = dimension.model_copy(
        update={"min": dimension.max, "nom": dimension.min, "max": dimension.nom}
    )
    return _part_spec_package_update(artifacts, {field: shifted}), {
        "field": field,
        "shift": "min<-max,nom<-min,max<-nom",
    }


def _part_spec_flip_drawing_view(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    current = artifacts.spec.package.drawing_view
    updated = "bottom" if current == "top" else "top"
    return _part_spec_package_update(artifacts, {"drawing_view": updated}), {
        "from": current,
        "to": updated,
    }


def _part_spec_rotate_pin1_corner(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    corners = ("top_left", "top_right", "bottom_right", "bottom_left")
    current = artifacts.spec.package.pin1_corner
    next_corner = corners[(corners.index(current) + 1) % len(corners)]
    return _part_spec_package_update(artifacts, {"pin1_corner": next_corner}), {
        "from": current,
        "to": next_corner,
    }


def _part_spec_sibling_mpn(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    updated = f"{artifacts.spec.mpn}-SIBLING-PACKAGE"
    spec = artifacts.spec.model_copy(update={"mpn": updated})
    return (
        MutationArtifacts(
            spec,
            artifacts.symbol,
            artifacts.footprint,
            artifacts.model,
            artifacts.model_path,
        ),
        {"mpn": updated},
    )


def _model_transform(
    *,
    mirror_x: bool = False,
    rotation_z_deg: float = 0.0,
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    scale: float = 1.0,
) -> MutationTransform:
    def transform(
        artifacts: MutationArtifacts,
        _rng: random.Random,
    ) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
        model = occt.transform(
            artifacts.model,
            mirror_x=mirror_x,
            rotation_z_deg=rotation_z_deg,
            translation=translation,
            scale=scale,
        )
        params: dict[str, str | int | float | bool] = {}
        if mirror_x:
            params["mirror"] = "x"
        if rotation_z_deg:
            params["rotation_deg"] = rotation_z_deg
        if translation != (0.0, 0.0, 0.0):
            params["offset_x_mm"] = translation[0]
            params["offset_y_mm"] = translation[1]
            params["offset_z_mm"] = translation[2]
        if scale != 1.0:
            params["scale"] = scale
        return (
            MutationArtifacts(
                artifacts.spec,
                artifacts.symbol,
                artifacts.footprint,
                model,
                artifacts.model_path,
            ),
            params,
        )

    return transform


def _model_remove_pin1_marker(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    solids = list(occt.solids(artifacts.model))
    facts = occt.inspect(artifacts.model).solids
    if not solids or len(solids) != len(facts):
        raise MutationError("model has no inspectable solids")
    body_index = max(range(len(facts)), key=lambda index: facts[index].volume)
    bounds = facts[body_index].bbox
    solids[body_index] = occt.box(
        bounds.x_min,
        bounds.y_min,
        bounds.z_min,
        bounds.x_max - bounds.x_min,
        bounds.y_max - bounds.y_min,
        bounds.z_max - bounds.z_min,
    )
    model = occt.compound(solids)
    return (
        MutationArtifacts(
            artifacts.spec,
            artifacts.symbol,
            artifacts.footprint,
            model,
            artifacts.model_path,
        ),
        {"body_solid": body_index},
    )


MUTATION_OPERATORS: tuple[MutationOperator, ...] = (
    MutationOperator("symbol_adjacent_pin_swap", "symbol", True, _symbol_adjacent_pin_swap),
    MutationOperator("symbol_pin_name_swap", "symbol", True, _symbol_pin_name_swap),
    MutationOperator("symbol_pin_number_offset", "symbol", True, _symbol_pin_number_offset),
    MutationOperator("symbol_reversed_pin_order", "symbol", True, _symbol_reversed_pin_order),
    MutationOperator("footprint_mirror_x", "footprint", True, _footprint_reflection("x")),
    MutationOperator("footprint_mirror_y", "footprint", True, _footprint_reflection("y")),
    MutationOperator("footprint_rotate_90", "footprint", True, _footprint_rotation(90)),
    MutationOperator("footprint_rotate_180", "footprint", True, _footprint_rotation(180)),
    MutationOperator("footprint_rotate_270", "footprint", True, _footprint_rotation(270)),
    MutationOperator("footprint_pad_shift_0_1mm", "footprint", True, _footprint_pad_shift),
    MutationOperator("footprint_pitch_scale_1_02", "footprint", True, _footprint_pitch_scale),
    MutationOperator("footprint_ep_size_delta_20_percent", "footprint", True, _footprint_ep_size),
    MutationOperator("footprint_mm_to_inch", "footprint", True, _footprint_unit_scale(1 / 25.4)),
    MutationOperator("footprint_inch_to_mm", "footprint", True, _footprint_unit_scale(25.4)),
    MutationOperator("footprint_removed_pad", "footprint", True, _footprint_remove_pad),
    MutationOperator(
        "footprint_duplicated_pad_number", "footprint", True, _footprint_duplicate_pad_number
    ),
    MutationOperator(
        "footprint_swapped_pad_numbers", "footprint", True, _footprint_swap_pad_numbers
    ),
    MutationOperator(
        "partspec_min_nom_max_column_shift", "part_spec", True, _part_spec_column_shift
    ),
    MutationOperator("partspec_drawing_view_flip", "part_spec", True, _part_spec_flip_drawing_view),
    MutationOperator(
        "partspec_pin1_corner_rotation", "part_spec", True, _part_spec_rotate_pin1_corner
    ),
    MutationOperator("partspec_sibling_package_mpn", "part_spec", True, _part_spec_sibling_mpn),
    MutationOperator("model_mirror_x", "model", True, _model_transform(mirror_x=True)),
    MutationOperator("model_rotate_90", "model", True, _model_transform(rotation_z_deg=90)),
    MutationOperator("model_rotate_180", "model", True, _model_transform(rotation_z_deg=180)),
    MutationOperator(
        "model_offset_0_1mm",
        "model",
        True,
        _model_transform(translation=(0.1, 0.0, 0.0)),
    ),
    MutationOperator("model_scale_25_4", "model", True, _model_transform(scale=25.4)),
    MutationOperator("model_removed_pin1_marker", "model", True, _model_remove_pin1_marker),
)


def run_mutations(fixture: MutationFixture) -> MutationReport:
    """Run every seeded mutation against the fixture's complete verification stack."""
    if (
        not fixture.artifacts.model_path.is_file()
        or fixture.artifacts.model_path.suffix.casefold()
        not in {
            ".step",
            ".stp",
        }
    ):
        raise MutationError("mutation fixture requires an existing generated STEP model")
    baseline = list(fixture.verify(fixture.artifacts))
    for item in baseline:
        family_for_code(item.code)
    baseline_errors = [item.code for item in baseline if item.severity == "error"]
    if baseline_errors:
        details = ", ".join(sorted(baseline_errors))
        raise MutationError(f"mutation fixture is not known-good; baseline errors: {details}")
    baseline_counts = Counter((item.code, item.severity) for item in baseline)
    outcomes: list[MutationOutcome] = []
    family_hits: Counter[CheckFamily] = Counter()
    single_oracle: list[str] = []
    undetected: list[str] = []

    for operator in MUTATION_OPERATORS:
        mutated, record = operator.apply(fixture.artifacts, fixture.seed)
        if record.target == "model":
            with tempfile.TemporaryDirectory(prefix="circuit-mutation-") as directory:
                model_path = Path(directory) / "mutated.step"
                occt.write_step(mutated.model, model_path, product_name=mutated.spec.mpn)
                verified = list(fixture.verify(replace(mutated, model_path=model_path)))
        else:
            verified = list(fixture.verify(mutated))
        remaining_baseline = dict(baseline_counts)
        new_codes: list[str] = []
        for finding in verified:
            key = (finding.code, finding.severity)
            remaining = remaining_baseline.get(key, 0)
            if remaining:
                remaining_baseline[key] = remaining - 1
            else:
                new_codes.append(finding.code)
        unique_codes = sorted(set(new_codes))
        families: list[CheckFamily] = sorted({family_for_code(code) for code in unique_codes})
        family_hits.update(families)
        non_vision_count = len(set(families).intersection(_NON_VISION_FAMILIES))
        if not families:
            status: MutationStatus = "undetected"
            undetected.append(record.operator)
        elif record.critical and non_vision_count < 2:
            status = "single_oracle"
            single_oracle.append(record.operator)
        else:
            status = "detected"
        outcomes.append(
            MutationOutcome(
                mutation=record,
                finding_codes=unique_codes,
                families=families,
                non_vision_family_count=non_vision_count,
                status=status,
            )
        )

    total = len(outcomes)
    family_keys: tuple[CheckFamily, ...] = (*_NON_VISION_FAMILIES, "vision")
    rates: dict[CheckFamily, float] = {
        family: family_hits[family] / total if total else 0.0 for family in family_keys
    }
    return MutationReport(
        seed=fixture.seed,
        baseline_findings=sorted(item.code for item in baseline),
        outcomes=outcomes,
        family_detection_rates=rates,
        single_oracle=single_oracle,
        undetected=undetected,
        passed=not single_oracle and not undetected,
    )
