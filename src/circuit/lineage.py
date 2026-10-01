"""Hash-bound lineage and deterministic pad-change records."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .libitems import FootprintDef, PadDef
from .ruleprofile import EvidenceRef

PadChangeField = Literal[
    "x",
    "y",
    "width",
    "height",
    "shape",
    "roundrect_ratio",
    "paste_margin",
    "mask_margin",
    "added",
    "removed",
]
_PAD_FIELDS: tuple[PadChangeField, ...] = (
    "x",
    "y",
    "width",
    "height",
    "shape",
    "roundrect_ratio",
    "paste_margin",
    "mask_margin",
)
_CHANGE_TOLERANCE_MM = 1e-4


class PadChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pad: str
    field: PadChangeField
    before: float | str | None
    after: float | str | None


class FootprintBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "generated",
        "manufacturer",
        "organization_library",
        "kicad_official",
    ]
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_chain_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_generated_chain(self) -> FootprintBase:
        if self.kind == "generated" and self.rule_chain_sha256 is None:
            raise ValueError("generated footprint bases require rule_chain_sha256")
        return self


class FootprintLineage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_footprint_lineage"]
    footprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layer: Literal["organization", "product"]
    product: str | None = None
    base: FootprintBase
    changes: list[PadChange] = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_product(self) -> FootprintLineage:
        if not self.reason.strip():
            raise ValueError("reason must not be empty")
        if self.layer == "product" and (self.product is None or not self.product.strip()):
            raise ValueError("product layer requires a product name")
        if self.layer == "organization" and self.product is not None:
            raise ValueError("organization lineage must not specify a product name")
        return self


def lineage_path_for(footprint_path: Path) -> Path:
    return Path(f"{footprint_path}.lineage.json")


def _pad_sort_key(pad: PadDef) -> tuple[float | str, ...]:
    return (
        pad.x,
        pad.y,
        pad.width,
        pad.height,
        pad.shape,
        pad.roundrect_ratio if pad.roundrect_ratio is not None else -math.inf,
        pad.paste_margin if pad.paste_margin is not None else -math.inf,
        pad.mask_margin if pad.mask_margin is not None else -math.inf,
    )


def _pad_signature(pad: PadDef) -> str:
    return json.dumps(
        {field: getattr(pad, field) for field in _PAD_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )


def pad_changes(base: FootprintDef, current: FootprintDef) -> list[PadChange]:
    """Return stable base-to-current pad changes grouped by pad number."""

    base_by_number: dict[str, list[PadDef]] = defaultdict(list)
    current_by_number: dict[str, list[PadDef]] = defaultdict(list)
    for pad in base.pads:
        base_by_number[pad.number].append(pad)
    for pad in current.pads:
        current_by_number[pad.number].append(pad)

    changes: list[PadChange] = []
    for number in sorted(base_by_number.keys() | current_by_number.keys()):
        before_pads = sorted(base_by_number.get(number, []), key=_pad_sort_key)
        after_pads = sorted(current_by_number.get(number, []), key=_pad_sort_key)
        for before, after in zip(before_pads, after_pads, strict=False):
            for field in _PAD_FIELDS:
                before_value = getattr(before, field)
                after_value = getattr(after, field)
                if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                    changed = abs(float(before_value) - float(after_value)) > _CHANGE_TOLERANCE_MM
                else:
                    changed = before_value != after_value
                if changed:
                    changes.append(
                        PadChange(
                            pad=number,
                            field=field,
                            before=before_value,
                            after=after_value,
                        )
                    )
        for pad in before_pads[len(after_pads) :]:
            changes.append(
                PadChange(
                    pad=number,
                    field="removed",
                    before=_pad_signature(pad),
                    after=None,
                )
            )
        for pad in after_pads[len(before_pads) :]:
            changes.append(
                PadChange(
                    pad=number,
                    field="added",
                    before=None,
                    after=_pad_signature(pad),
                )
            )
    return sorted(
        changes,
        key=lambda change: (
            change.pad,
            _PAD_FIELDS.index(change.field) if change.field in _PAD_FIELDS else len(_PAD_FIELDS),
            str(change.before),
            str(change.after),
        ),
    )


def change_matches(actual: PadChange, recorded: PadChange) -> bool:
    if actual.pad != recorded.pad or actual.field != recorded.field:
        return False
    for actual_value, recorded_value in (
        (actual.before, recorded.before),
        (actual.after, recorded.after),
    ):
        if isinstance(actual_value, (int, float)) and isinstance(recorded_value, (int, float)):
            if abs(float(actual_value) - float(recorded_value)) > _CHANGE_TOLERANCE_MM:
                return False
        elif actual_value != recorded_value:
            return False
    return True


def compare_recorded_changes(
    actual: list[PadChange], recorded: list[PadChange]
) -> tuple[list[PadChange], list[PadChange]]:
    """Return actual unrecorded changes and recorded changes no longer present."""

    unmatched_actual = list(actual)
    stale: list[PadChange] = []
    for change in recorded:
        match_index = next(
            (
                index
                for index, actual_change in enumerate(unmatched_actual)
                if change_matches(actual_change, change)
            ),
            None,
        )
        if match_index is None:
            stale.append(change)
        else:
            unmatched_actual.pop(match_index)
    return unmatched_actual, stale
