"""Per-part assembly handling export (MSL, peak reflow, ESD) for production-engineering.

Values come verbatim from the brief ``handling`` of each part; nothing is
defaulted. A part without ``handling`` is listed in ``unknown`` and the export
verdict is ``unknown`` so the consumer cannot plan reflow or ESD control on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .brief import DesignBrief, brief_sha256

ARTIFACT_KIND = "circuit_handling"
SCHEMA_VERSION = 1


class HandledPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    msl: Literal["1", "2", "2a", "3", "4", "5", "5a", "6"]
    peak_reflow_c: float
    esd_sensitive: bool
    source: str


class CircuitHandling(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_handling"] = ARTIFACT_KIND
    schema_version: Literal[1] = SCHEMA_VERSION
    design: str
    brief_sha256: str
    parts: list[HandledPart]
    unknown: list[str]
    verdict: Literal["pass", "unknown"]


def export_handling(design: DesignBrief, brief_path: Path) -> CircuitHandling:
    """Project the brief part handling facts; parts without them are unknown."""
    parts = [
        HandledPart(
            ref=part.reference,
            msl=part.handling.msl,
            peak_reflow_c=part.handling.peak_reflow_c,
            esd_sensitive=part.handling.esd_sensitive,
            source=part.handling.source,
        )
        for part in design.parts
        if part.handling is not None
    ]
    unknown = sorted(part.reference for part in design.parts if part.handling is None)
    return CircuitHandling(
        design=design.name,
        brief_sha256=brief_sha256(brief_path),
        parts=sorted(parts, key=lambda item: item.ref),
        unknown=unknown,
        verdict="unknown" if unknown else "pass",
    )
