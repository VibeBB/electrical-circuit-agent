"""Deterministic KiCad symbol, footprint, and pin resolution."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import sexpr
from .brief import DesignBrief, brief_sha256
from .landpattern import compute_land_pattern
from .libitems import LibItemError, parse_footprint
from .libreview import correction_regressions, current_packet_id, review_status
from .libverify import LibraryVerification, verify_library_part
from .partspec import load_part_spec


class LibraryRoots(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_dirs: list[Path]
    footprint_dirs: list[Path]


def default_roots() -> LibraryRoots:
    kicad_share = Path(os.environ.get("CIRCUIT_KICAD_SHARE", "/usr/share/kicad-nightly"))
    cern = Path(os.environ.get("CIRCUIT_CERN_LIBS", "/opt/circuit/libraries/cern-kicad-libs"))
    return LibraryRoots(
        symbol_dirs=[kicad_share / "symbols", cern / "SchLib"],
        footprint_dirs=[kicad_share / "footprints", cern / "PcbLib"],
    )


class SymbolInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_path: Path
    pins: list[str]


class LibraryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_path: Path
    brief_sha256: str
    symbol_dirs: list[Path]
    footprint_dirs: list[Path]
    symbols: dict[str, SymbolInfo]
    footprints: dict[str, Path]
    missing_symbol_libraries: list[str]
    missing_symbols: list[str]
    missing_footprint_libraries: list[str]
    missing_footprints: list[str]
    missing_pins: dict[str, list[str]]
    verdict: Literal["pass", "fail"]
    reasons: list[str]


def _field(node: list[sexpr.SExpr], name: str) -> str | None:
    for child in node[1:]:
        if isinstance(child, list) and len(child) == 2 and child[0] == name:
            value = child[1]
            return value if isinstance(value, str) else None
    return None


def _symbol_node(root: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr] | None:
    for child in root[1:]:
        if (
            isinstance(child, list)
            and len(child) >= 2
            and child[0] == "symbol"
            and child[1] == name
        ):
            return child
    return None


def _pins_in(node: list[sexpr.SExpr]) -> set[str]:
    pins: set[str] = set()
    for child in node[1:]:
        if not isinstance(child, list) or not child:
            continue
        if child[0] == "pin":
            number_node = next(
                (
                    item
                    for item in child[1:]
                    if isinstance(item, list) and item and item[0] == "number"
                ),
                None,
            )
            if isinstance(number_node, list) and len(number_node) >= 2:
                number = number_node[1]
                if isinstance(number, str):
                    pins.add(number)
        pins.update(_pins_in(child))
    return pins


def _symbol_pins_from_root(
    root: list[sexpr.SExpr],
    symbol_name: str,
    stack: set[str] | None = None,
) -> list[str] | None:
    stack = set() if stack is None else stack
    if symbol_name in stack:
        return None
    node = _symbol_node(root, symbol_name)
    if node is None:
        return None
    stack.add(symbol_name)
    pins = _pins_in(node)
    parent = _field(node, "extends")
    if parent is not None:
        parent_pins = _symbol_pins_from_root(root, parent, stack)
        if parent_pins is None:
            return None
        pins.update(parent_pins)
    return sorted(pins)


def symbol_pins(library_path: Path, symbol_name: str) -> list[str] | None:
    try:
        root = sexpr.parse_text(library_path.read_text(encoding="utf-8"))
    except (OSError, sexpr.SExprError):
        return None
    return _symbol_pins_from_root(root, symbol_name)


def _find_library(directories: list[Path], nickname: str, suffix: str) -> Path | None:
    filename = f"{nickname}{suffix}"
    for directory in directories:
        candidate = directory / filename
        if candidate.is_file() or (suffix == ".pretty" and candidate.is_dir()):
            return candidate
    return None


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _project_verification_matches(
    verification_dir: Path,
    *,
    project_library: Path,
    project_dir: Path,
    symbol_path: Path,
    symbol_name: str,
    footprint_path: Path,
) -> LibraryVerification | None:
    symbol_sha256 = _file_sha256(symbol_path)
    footprint_sha256 = _file_sha256(footprint_path)
    if symbol_sha256 is None or footprint_sha256 is None:
        return None
    try:
        footprint_name = parse_footprint(footprint_path).name
    except (OSError, LibItemError):
        return None
    project_root = project_dir.resolve()
    library_root = project_library.resolve()

    def input_path(value: Path) -> Path:
        if value.is_absolute():
            raise ValueError("verification inputs must be relative to the project library")
        resolved = (library_root / value).resolve(strict=True)
        resolved.relative_to(project_root)
        return resolved

    failed_match: LibraryVerification | None = None
    for verification_path in sorted(verification_dir.glob("*.verification.json")):
        try:
            report = LibraryVerification.model_validate_json(
                verification_path.read_text(encoding="utf-8")
            )
            inputs = report.inputs
            part_spec_path = input_path(inputs.part_spec_path)
            input_symbol_path = input_path(inputs.symbol_lib)
            input_footprint_path = input_path(inputs.footprint_path)
            if (
                input_symbol_path != symbol_path.resolve()
                or input_footprint_path != footprint_path.resolve()
                or inputs.symbol_name != symbol_name
            ):
                continue
            spec = load_part_spec(part_spec_path)
            with tempfile.TemporaryDirectory(prefix="circuit-library-gate-") as temporary:
                fresh = verify_library_part(
                    spec,
                    spec_path=part_spec_path,
                    symbol_lib=input_symbol_path,
                    symbol_name=inputs.symbol_name,
                    footprint_path=input_footprint_path,
                    library_dir=library_root,
                    reference=compute_land_pattern(spec, inputs.density),
                    tolerance_mm=inputs.tolerance_mm,
                    model_required=inputs.model_required,
                    output_path=Path(temporary) / "fresh.verification.json",
                )
        except Exception:
            continue
        fresh = fresh.model_copy(
            update={
                "inputs": fresh.inputs.model_copy(
                    update={
                        "part_spec_path": part_spec_path,
                        "symbol_lib": input_symbol_path,
                        "footprint_path": input_footprint_path,
                    }
                )
            }
        )
        if (
            fresh.symbol.sha256 == symbol_sha256
            and fresh.footprint.sha256 == footprint_sha256
            and fresh.symbol.name == symbol_name
            and fresh.footprint.name == footprint_name
        ):
            if fresh.verdict == "pass":
                return fresh
            failed_match = fresh
    return failed_match


def check_libraries(
    brief: DesignBrief,
    *,
    brief_path: Path,
    roots: LibraryRoots | None = None,
) -> LibraryReport:
    roots = default_roots() if roots is None else roots
    project_library = brief_path.parent / "library"
    symbols: dict[str, SymbolInfo] = {}
    footprints: dict[str, Path] = {}
    missing_symbol_libraries: list[str] = []
    missing_symbols: list[str] = []
    missing_footprint_libraries: list[str] = []
    missing_footprints: list[str] = []
    missing_pins: dict[str, list[str]] = {}
    reasons: list[str] = []
    parsed_symbols: dict[Path, list[sexpr.SExpr] | None] = {}
    selected_symbol_paths: dict[str, Path] = {}
    selected_footprint_paths: dict[str, Path] = {}
    project_symbol_nicknames: set[str] = set()
    project_footprint_nicknames: set[str] = set()
    requested_symbols = {part.lib_id for part in brief.parts}
    for lib_id in sorted(requested_symbols):
        nickname, symbol_name = lib_id.split(":", 1)
        project_path = project_library / f"{nickname}.kicad_sym"
        default_path = _find_library(roots.symbol_dirs, nickname, ".kicad_sym")
        if (
            project_path.is_file()
            and default_path is not None
            and "library_nickname_conflict" not in reasons
        ):
            reasons.append("library_nickname_conflict")
        if project_path.is_file():
            library_path = project_path
            project_symbol_nicknames.add(nickname)
        else:
            library_path = default_path
        if library_path is None:
            missing_symbol_libraries.append(nickname)
            reasons.append(f"missing symbol library: {nickname}")
            continue
        selected_symbol_paths[nickname] = library_path
        if library_path not in parsed_symbols:
            try:
                parsed_symbols[library_path] = sexpr.parse_text(
                    library_path.read_text(encoding="utf-8")
                )
            except (OSError, sexpr.SExprError) as exc:
                parsed_symbols[library_path] = None
                reasons.append(f"could not parse symbol library {library_path}: {exc}")
        root = parsed_symbols[library_path]
        pins = None if root is None else _symbol_pins_from_root(root, symbol_name)
        if pins is None:
            missing_symbols.append(lib_id)
            reasons.append(f"missing symbol: {lib_id}")
        else:
            symbols[lib_id] = SymbolInfo(library_path=library_path, pins=pins)

    requested_footprints = {part.footprint for part in brief.parts}
    for footprint_id in sorted(requested_footprints):
        nickname, footprint_name = footprint_id.split(":", 1)
        project_path = project_library / f"{nickname}.pretty"
        default_path = _find_library(roots.footprint_dirs, nickname, ".pretty")
        if (
            project_path.is_dir()
            and default_path is not None
            and "library_nickname_conflict" not in reasons
        ):
            reasons.append("library_nickname_conflict")
        if project_path.is_dir():
            library_path = project_path
            project_footprint_nicknames.add(nickname)
        else:
            library_path = default_path
        if library_path is None:
            missing_footprint_libraries.append(nickname)
            reasons.append(f"missing footprint library: {nickname}")
            continue
        footprint_path = library_path / f"{footprint_name}.kicad_mod"
        if not footprint_path.is_file():
            missing_footprints.append(footprint_id)
            reasons.append(f"missing footprint: {footprint_id}")
        else:
            footprints[footprint_id] = footprint_path
            selected_footprint_paths[footprint_id] = footprint_path

    references_to_pins: dict[str, set[str]] = {}
    for net in brief.nets:
        for token in net.pins:
            reference, pin = token.split(".", 1)
            references_to_pins.setdefault(reference, set()).add(pin)
    parts_by_reference = {part.reference: part for part in brief.parts}
    for reference, pins in references_to_pins.items():
        part = parts_by_reference[reference]
        info = symbols.get(part.lib_id)
        if info is not None:
            missing = sorted(pins - set(info.pins))
            if missing:
                missing_pins[reference] = missing
                reasons.append(f"missing pins for {reference}: {', '.join(missing)}")

    project_parts = {
        part.lib_id: part
        for part in brief.parts
        if part.lib_id.split(":", 1)[0] in project_symbol_nicknames
        or part.footprint.split(":", 1)[0] in project_footprint_nicknames
    }
    for lib_id, part in sorted(project_parts.items()):
        nickname, symbol_name = lib_id.split(":", 1)
        symbol_path = selected_symbol_paths.get(nickname)
        footprint_path = selected_footprint_paths.get(part.footprint)
        fresh: LibraryVerification | None = None
        if symbol_path is not None and footprint_path is not None:
            fresh = _project_verification_matches(
                project_library / "verification",
                project_library=project_library,
                project_dir=brief_path.parent,
                symbol_path=symbol_path,
                symbol_name=symbol_name,
                footprint_path=footprint_path,
            )
        if fresh is None:
            reasons.append(f"unverified project library part: {lib_id}")
            continue
        try:
            spec = load_part_spec(fresh.inputs.part_spec_path)
            regressions = correction_regressions(project_library, spec)
        except (OSError, ValueError):
            reasons.append(f"human_review_invalid: {lib_id}")
            continue
        if regressions:
            reasons.append(f"correction_regressed: {lib_id}")
        if fresh.verdict != "pass":
            reasons.append(f"unverified project library part: {lib_id}")
            continue
        try:
            current_id = current_packet_id(
                fresh.inputs.part_spec_path,
                symbol_lib=fresh.inputs.symbol_lib,
                symbol_name=fresh.inputs.symbol_name,
                footprint_path=fresh.inputs.footprint_path,
                library_dir=project_library,
                density=fresh.inputs.density,
                tolerance_mm=fresh.inputs.tolerance_mm,
                model_required=fresh.inputs.model_required,
            )
            status = review_status(project_library, spec, current_id)
        except (OSError, ValueError):
            reasons.append(f"human_review_invalid: {lib_id}")
            continue
        if status.state != "approved":
            state = {
                "rejected": "rejected",
                "invalid": "invalid",
                "pending": "missing",
            }[status.state]
            reasons.append(f"human_review_{state}: {lib_id}")

    failed = bool(
        missing_symbol_libraries
        or missing_symbols
        or missing_footprint_libraries
        or missing_footprints
        or missing_pins
        or any("could not parse" in reason for reason in reasons)
        or "library_nickname_conflict" in reasons
        or any(reason.startswith("unverified project library part:") for reason in reasons)
        or any(reason.startswith("human_review_") for reason in reasons)
        or any(reason.startswith("correction_regressed:") for reason in reasons)
    )
    return LibraryReport(
        brief_path=brief_path,
        brief_sha256=brief_sha256(brief_path),
        symbol_dirs=[project_library, *roots.symbol_dirs],
        footprint_dirs=[project_library, *roots.footprint_dirs],
        symbols=symbols,
        footprints=footprints,
        missing_symbol_libraries=sorted(set(missing_symbol_libraries)),
        missing_symbols=sorted(set(missing_symbols)),
        missing_footprint_libraries=sorted(set(missing_footprint_libraries)),
        missing_footprints=sorted(set(missing_footprints)),
        missing_pins=missing_pins,
        verdict="fail" if failed else "pass",
        reasons=reasons,
    )
