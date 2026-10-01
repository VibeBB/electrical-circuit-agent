"""Deterministic search for reusable KiCad library items."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import sexpr
from .landpattern import LandPatternResult
from .libitems import LibItemError, PadDef, parse_footprint, parse_symbol
from .libraries import LibraryRoots
from .libverify import (
    VerifyFinding,
    _validate_lineage,  # pyright: ignore[reportPrivateUsage]
    functional_findings,
)
from .lineage import FootprintLineage, lineage_path_for
from .partspec import Dimension, LandPad, PartSpec
from .ruleprofile import EffectiveRules, load_rules

FootprintMatch = Literal["exact", "compatible", "functional", "near"]
SymbolMatch = Literal["exact", "pin_compatible"]
FootprintOrigin = Literal["kicad_official", "project", "organization", "manufacturer"]


class CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    resolved: bool
    resolved_path: str | None


class FunctionalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    failures: list[str]


class FootprintCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    library_path: Path
    path: Path
    origin: FootprintOrigin
    classification: FootprintMatch
    max_center_delta: float
    max_size_delta: float
    pin1_quadrant: str
    pin1_quadrant_ok: bool
    models: list[CandidateModel]
    lineage_path: str | None
    lineage_layer: Literal["organization", "product"] | None
    lineage_product: str | None
    functional: FunctionalCheck
    preferred_tuned: bool


class SymbolCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    library_path: Path
    classification: SymbolMatch
    pin_name_differences: list[str]


class CandidateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_candidates"]
    part_spec_sha256: str
    reference_source: Literal["datasheet", "ipc7351b"]
    footprints: list[FootprintCandidate]
    symbols: list[SymbolCandidate]


_FAMILY_LIBRARIES: dict[str, tuple[str, ...]] = {
    "no_lead_dual": ("package_dfn_qfn",),
    "no_lead_quad": ("package_dfn_qfn",),
    "gullwing_dual": ("package_so",),
    "gullwing_quad": ("package_qfp",),
    "chip": (
        "resistor_smd",
        "capacitor_smd",
        "inductor_smd",
        "diode_smd",
        "led_smd",
    ),
}
_CLASS_RANK: dict[FootprintMatch, int] = {
    "exact": 0,
    "compatible": 1,
    "functional": 2,
    "near": 3,
}
_ORIGIN_RANK: dict[FootprintOrigin, int] = {
    "organization": 0,
    "project": 1,
    "manufacturer": 2,
    "kicad_official": 3,
}


def _spec_digest(spec: PartSpec) -> str:
    payload = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_under(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _footprint_paths(
    spec: PartSpec,
    roots: LibraryRoots,
    project_library_dir: Path | None,
    organization_dirs: Sequence[Path],
) -> list[Path]:
    directories = [*roots.footprint_dirs, *organization_dirs]
    if project_library_dir is not None:
        directories.append(project_library_dir)
    unique_dirs = list(dict.fromkeys(path.resolve() for path in directories))
    family = spec.package.family
    prefixes = _FAMILY_LIBRARIES.get(family, ())
    pitch = _nominal(spec.package.pitch) if spec.package.pitch is not None else None
    matching: set[Path] = set()
    for directory in unique_dirs:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.kicad_mod"):
            library_name = path.parent.name.casefold()
            in_project = _is_under(path, project_library_dir)
            in_organization = any(_is_under(path, root) for root in organization_dirs)
            in_manufacturer_root = any(_is_under(path, root) for root in roots.footprint_dirs[1:])
            family_match = any(library_name.startswith(prefix) for prefix in prefixes)
            if not (in_project or in_organization or in_manufacturer_root or family_match):
                continue
            name = path.stem
            if family != "chip":
                if not _has_pin_count(name, spec.package.pin_count):
                    continue
                if pitch is None or not _has_pitch(name, pitch):
                    continue
            matching.add(path)
    return sorted(matching, key=lambda path: path.as_posix().casefold())


def _has_pin_count(name: str, pin_count: int) -> bool:
    return (
        re.search(
            rf"(?:^|[-_]){pin_count}(?=[-_.]|$)",
            name,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _has_pitch(name: str, pitch: float) -> bool:
    for match in re.finditer(
        r"(?:^|[-_])P([0-9]+(?:\.[0-9]+)?)mm(?=[-_.]|$)",
        name,
        flags=re.IGNORECASE,
    ):
        try:
            if math.isclose(float(match.group(1)), pitch, rel_tol=0.0, abs_tol=1e-6):
                return True
        except ValueError:
            continue
    return False


def _nominal(dimension: Dimension) -> float | None:
    if dimension.nom is not None:
        return float(dimension.nom)
    if dimension.min is not None and dimension.max is not None:
        return (float(dimension.min) + float(dimension.max)) / 2
    value = dimension.min if dimension.min is not None else dimension.max
    return float(value) if value is not None else None


def _pad_box(pad: PadDef) -> tuple[float, float, float, float]:
    angle = math.radians(pad.rotation % 180)
    width = abs(pad.width * math.cos(angle)) + abs(pad.height * math.sin(angle))
    height = abs(pad.width * math.sin(angle)) + abs(pad.height * math.cos(angle))
    return (
        pad.x - width / 2,
        pad.y - height / 2,
        pad.x + width / 2,
        pad.y + height / 2,
    )


def _numbered_boxes(pads: list[PadDef]) -> dict[str, tuple[float, float, float, float]]:
    boxes: dict[str, list[tuple[float, float, float, float]]] = {}
    for pad in pads:
        if pad.number:
            boxes.setdefault(pad.number, []).append(_pad_box(pad))
    return {
        number: (
            min(box[0] for box in group),
            min(box[1] for box in group),
            max(box[2] for box in group),
            max(box[3] for box in group),
        )
        for number, group in boxes.items()
    }


def _reference_boxes(
    reference: LandPatternResult,
) -> dict[str, tuple[float, float, float, float]]:
    boxes: dict[str, list[tuple[float, float, float, float]]] = {}
    for pad in reference.pads:
        boxes.setdefault(pad.number, []).append(
            (
                pad.x - pad.width / 2,
                pad.y - pad.height / 2,
                pad.x + pad.width / 2,
                pad.y + pad.height / 2,
            )
        )
    return {
        number: (
            min(box[0] for box in group),
            min(box[1] for box in group),
            max(box[2] for box in group),
            max(box[3] for box in group),
        )
        for number, group in boxes.items()
    }


def _geometry_deltas(
    reference: dict[str, tuple[float, float, float, float]],
    candidate: dict[str, tuple[float, float, float, float]],
) -> tuple[float, float]:
    center_delta = 0.0
    size_delta = 0.0
    for number in reference.keys() & candidate.keys():
        rx0, ry0, rx1, ry1 = reference[number]
        cx0, cy0, cx1, cy1 = candidate[number]
        center_delta = max(
            center_delta,
            abs((rx0 + rx1 - cx0 - cx1) / 2),
            abs((ry0 + ry1 - cy0 - cy1) / 2),
        )
        size_delta = max(
            size_delta,
            abs((rx1 - rx0) - (cx1 - cx0)),
            abs((ry1 - ry0) - (cy1 - cy0)),
        )
    return center_delta, size_delta


def _footprint_match(
    reference_pads: list[LandPad],
    reference: dict[str, tuple[float, float, float, float]],
    candidate_pads: list[PadDef],
) -> tuple[FootprintMatch, float, float] | None:
    candidate = _numbered_boxes(candidate_pads)
    center_delta, size_delta = _geometry_deltas(reference, candidate)
    same_keys = reference.keys() == candidate.keys()
    same_multiplicity = Counter(pad.number for pad in reference_pads) == Counter(
        pad.number for pad in candidate_pads if pad.number
    )
    if same_keys and same_multiplicity and center_delta <= 0.01 and size_delta <= 0.01:
        classification: FootprintMatch = "exact"
    elif same_keys and same_multiplicity and center_delta <= 0.05 and size_delta <= 0.05:
        classification = "compatible"
    else:
        classification = "near"
    return classification, center_delta, size_delta


def _pin1_quadrant(
    pads: list[PadDef],
    family: str,
    expected_corner: str,
) -> tuple[str, bool]:
    pin1 = _numbered_boxes(
        [pad for pad in pads if pad.number == "1" and pad.type != "np_thru_hole"]
    ).get("1")
    if pin1 is None:
        return "missing", False
    x0, y0, x1, y1 = pin1
    x = (x0 + x1) / 2
    y = (y0 + y1) / 2
    if family == "chip":
        quadrant = "left" if x < 0 else "right" if x > 0 else "center"
        return quadrant, x < 0
    horizontal = "left" if x < 0 else "right" if x > 0 else "center"
    vertical = "top" if y < 0 else "bottom" if y > 0 else "center"
    quadrant = f"{vertical}_{horizontal}"
    return quadrant, quadrant == expected_corner


def _resolve_model(path: str, footprint_path: Path) -> CandidateModel:
    expanded = os.path.expandvars(path)
    resolved_path: str | None = None
    if "$" not in expanded and "${" not in expanded:
        candidate_path = Path(expanded)
        if not candidate_path.is_absolute():
            candidate_path = footprint_path.parent / candidate_path
        try:
            actual = candidate_path.resolve(strict=True)
        except OSError:
            actual = None
        if actual is not None and actual.is_file():
            resolved_path = str(actual)
    return CandidateModel(
        path=path,
        resolved=resolved_path is not None,
        resolved_path=resolved_path,
    )


def _footprint_candidates(
    spec: PartSpec,
    roots: LibraryRoots,
    project_library_dir: Path | None,
    reference: LandPatternResult,
    organization_dirs: Sequence[Path],
    rules: EffectiveRules,
    product: str | None,
) -> list[FootprintCandidate]:
    reference_boxes = _reference_boxes(reference)
    reference_pads = reference.pads
    candidates: list[FootprintCandidate] = []
    source_dirs: list[tuple[Path, FootprintOrigin]] = [
        *((directory, "organization") for directory in organization_dirs),
        *((directory, "project") for directory in (project_library_dir,) if directory is not None),
        *(
            (directory, "kicad_official" if index == 0 else "manufacturer")
            for index, directory in enumerate(roots.footprint_dirs)
        ),
    ]
    for path in _footprint_paths(
        spec,
        roots,
        project_library_dir,
        organization_dirs,
    ):
        try:
            footprint = parse_footprint(path)
        except (LibItemError, OSError):
            continue
        match = _footprint_match(reference_pads, reference_boxes, footprint.pads)
        if match is None:
            continue
        classification, center_delta, size_delta = match
        functional_findings_result = functional_findings(spec, footprint, rules)
        failures = [
            f"{finding.code}: {finding.subject}: {finding.message}"
            for finding in functional_findings_result
            if finding.severity == "error"
        ]
        functional = FunctionalCheck(passed=not failures, failures=failures)
        if not functional.passed:
            classification = "near"
        elif classification == "near":
            classification = "functional"
        quadrant, quadrant_ok = _pin1_quadrant(
            footprint.pads,
            spec.package.family,
            spec.package.pin1_corner,
        )
        origin: FootprintOrigin = "kicad_official"
        for source_dir, source_origin in source_dirs:
            if _is_under(path, source_dir):
                origin = source_origin
                break
        lineage_sidecar = lineage_path_for(path)
        lineage_path = (
            str(lineage_sidecar)
            if lineage_sidecar.exists() or lineage_sidecar.is_symlink()
            else None
        )
        lineage: FootprintLineage | None = None
        preferred_tuned = False
        if lineage_path is not None:
            lineage_library_dir = next(
                (
                    directory
                    for directory, _source_origin in source_dirs
                    if _is_under(path, directory)
                ),
                path.parent.parent,
            )
            lineage_findings: list[VerifyFinding] = []
            candidate_lineage, _base, valid_lineage = _validate_lineage(
                path,
                footprint,
                lineage_library_dir,
                lineage_findings,
            )
            if valid_lineage and candidate_lineage is not None:
                lineage = candidate_lineage
                preferred_tuned = functional.passed and (
                    lineage.layer == "organization"
                    or (lineage.layer == "product" and lineage.product == product)
                )
            else:
                lineage_failures = [
                    f"{finding.code}: {finding.subject}: {finding.message}"
                    for finding in lineage_findings
                ]
                if not lineage_failures:
                    lineage_failures.append("lineage_invalid: validation failed")
                functional = FunctionalCheck(
                    passed=False,
                    failures=[*functional.failures, *lineage_failures],
                )
                classification = "near"
        candidates.append(
            FootprintCandidate(
                name=footprint.name,
                library_path=path.parent,
                path=path,
                origin=origin,
                classification=classification,
                max_center_delta=round(center_delta, 6),
                max_size_delta=round(size_delta, 6),
                pin1_quadrant=quadrant,
                pin1_quadrant_ok=quadrant_ok,
                models=[_resolve_model(model.path, path) for model in footprint.models],
                lineage_path=lineage_path,
                lineage_layer=lineage.layer if lineage is not None else None,
                lineage_product=lineage.product if lineage is not None else None,
                functional=functional,
                preferred_tuned=preferred_tuned,
            )
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            not candidate.preferred_tuned,
            _CLASS_RANK[candidate.classification],
            _ORIGIN_RANK[candidate.origin],
            max(candidate.max_center_delta, candidate.max_size_delta),
            candidate.library_path.as_posix().casefold(),
            candidate.name.casefold(),
        ),
    )[:20]


def _symbol_paths(
    roots: LibraryRoots,
    project_library_dir: Path | None,
) -> list[Path]:
    directories = [*roots.symbol_dirs]
    if project_library_dir is not None:
        directories.append(project_library_dir)
    unique_dirs = list(dict.fromkeys(path.resolve() for path in directories))
    paths = {
        path
        for directory in unique_dirs
        if directory.is_dir()
        for path in directory.rglob("*.kicad_sym")
    }
    return sorted(paths, key=lambda path: path.as_posix().casefold())


def _symbol_names(path: Path) -> list[str]:
    try:
        root = sexpr.parse_text(path.read_text(encoding="utf-8"))
    except (OSError, sexpr.SExprError):
        return []
    if not root or root[0] != "kicad_symbol_lib":
        return []
    return [
        child[1]
        for child in root[1:]
        if isinstance(child, list)
        and len(child) >= 2
        and child[0] == "symbol"
        and isinstance(child[1], str)
    ]


def _symbol_prefix_matches(symbol_name: str, mpn: str) -> bool:
    match = re.match(r"[A-Za-z0-9]+", symbol_name)
    return (
        match is not None
        and len(match.group(0)) >= 5
        and mpn.casefold().startswith(match.group(0).casefold())
    )


def _normalize_pin_name(name: str) -> str:
    return re.sub(r"~\{([^}]*)\}", r"\1", name).casefold()


def _pin_map(pins: list[tuple[str, str]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for number, name in pins:
        result.setdefault(number, set()).add(_normalize_pin_name(name))
    return result


def _symbol_candidates(
    spec: PartSpec,
    roots: LibraryRoots,
    project_library_dir: Path | None,
) -> list[SymbolCandidate]:
    expected = _pin_map([(pin.number, pin.name) for pin in spec.pins])
    candidates: list[SymbolCandidate] = []
    for path in _symbol_paths(roots, project_library_dir):
        for name in _symbol_names(path):
            if not _symbol_prefix_matches(name, spec.mpn):
                continue
            try:
                symbol = parse_symbol(path, name)
            except (LibItemError, OSError):
                continue
            actual = _pin_map([(pin.number, pin.name) for pin in symbol.pins])
            if actual.keys() != expected.keys():
                continue
            differences = [
                f"{number}: {sorted(expected[number])} != {sorted(actual[number])}"
                for number in sorted(expected)
                if expected[number] != actual[number]
            ]
            candidates.append(
                SymbolCandidate(
                    name=name,
                    library_path=path,
                    classification="pin_compatible" if differences else "exact",
                    pin_name_differences=differences,
                )
            )
    rank: dict[SymbolMatch, int] = {"exact": 0, "pin_compatible": 1}
    return sorted(
        candidates,
        key=lambda candidate: (
            rank[candidate.classification],
            candidate.library_path.as_posix().casefold(),
            candidate.name.casefold(),
        ),
    )[:20]


def find_candidates(
    spec: PartSpec,
    *,
    roots: LibraryRoots,
    project_library_dir: Path | None,
    reference: LandPatternResult,
    organization_dirs: Sequence[Path] = (),
    rules: EffectiveRules | None = None,
    product: str | None = None,
) -> CandidateReport:
    """Find geometrically and electrically compatible KiCad library items."""

    effective_rules = rules or load_rules("builtin:ipc7351b", Path("."))
    return CandidateReport(
        artifact_kind="circuit_library_candidates",
        part_spec_sha256=_spec_digest(spec),
        reference_source=reference.source,
        footprints=_footprint_candidates(
            spec,
            roots,
            project_library_dir,
            reference,
            organization_dirs,
            effective_rules,
            product,
        ),
        symbols=_symbol_candidates(spec, roots, project_library_dir),
    )
