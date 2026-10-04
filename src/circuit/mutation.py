"""Seeded library-artifact mutations and independent-oracle accounting."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import kicad_cli, occt, sexpr
from .landpattern import Density, compute_land_pattern
from .libitems import (
    FootprintDef,
    GraphicDef,
    ModelRef,
    PadDef,
    SymbolDef,
    SymPin,
    parse_footprint,
    parse_symbol,
)
from .libverify import VerifyFinding
from .modeloracle import verify_model_export
from .packageid import sibling_package_mpn
from .partspec import Dimension, LandPad, PartSpec, PartSpecReport, load_part_spec
from .ruleprofile import load_rules

CheckFamily = Literal[
    "evidence",
    "pin_bijection",
    "orientation",
    "land_geometry",
    "export_oracle",
    "model_geometry",
    "rule_profile",
    "package_identity",
    "vision",
    "integrity",
]
MutationTarget = Literal["symbol", "footprint", "part_spec", "model"]
MutationStatus = Literal["detected", "single_oracle", "undetected"]
_COUNTING_FAMILIES: tuple[CheckFamily, ...] = (
    "evidence",
    "pin_bijection",
    "orientation",
    "land_geometry",
    "export_oracle",
    "model_geometry",
    "rule_profile",
    "package_identity",
)


_FAMILY_CODES: dict[CheckFamily, tuple[str, ...]] = {
    "evidence": (
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
        "drawing_id_missing",
        "drawing_revision_missing",
        "evidence_missing",
        "exposed_pad_table_mismatch",
        "glyph_loss",
        "height_nonpositive",
        "invisible_text",
        "kind_mismatch",
        "mechanical_mismatch",
        "mechanical_single_lane",
        "orderable_designator_mismatch",
        "orderable_mpn_mismatch",
        "package_variant_unbound",
        "page_not_extracted",
        "pin_reading_page_mismatch",
        "pin1_mismatch",
        "reading_order_divergence",
        "redistribution_review",
        "rederivation_failed",
        "stacked_limit_order",
        "table_lane_disagreement",
        "value_mismatch",
        "view_label_mismatch",
        "connector_mating_envelope_unknown",
        "connector_plating_unresolved",
        "connector_placement_part_spec_missing",
    ),
    "pin_bijection": (
        "duplicate_pin",
        "exposed_pad_unmapped",
        "tab_unmapped",
        "kicad_cli_unavailable",
        "kicad_parse",
        "orderable_pin_count_mismatch",
        "pad_set",
        "pin_count_family",
        "pin_count_mismatch",
        "pin_numbering_incomplete",
        "pin_pad_mapping",
        "pin_source_invalid",
        "pin_source_missing",
        "pin_source_name_mismatch",
        "pin_source_single",
        "pin_table_bijection",
        "pin_table_column_ambiguous",
        "pin_table_column_mismatch",
        "pin_table_missing",
        "pinout_missing",
        "corpus_pad_numbers_mismatch",
        "depopulated_pin_present",
        "corpus_pin_map_mismatch",
        "corpus_symbol_pin_map_mismatch",
        "symbol_pin_grid",
        "symbol_pin_name",
        "symbol_bank_pin_set",
        "symbol_pin_set",
        "symbol_pin_type",
        "symbol_property",
        "testboard_erc",
        "testboard_pinmap",
        "connector_numbering_mismatch",
    ),
    "orientation": (
        "pin1_location",
        "pin1_unparseable",
        "corpus_drawing_view_mismatch",
        "corpus_pin1_corner_mismatch",
        "pinout_name_ambiguous",
        "pinout_name_mismatch",
        "pinout_name_unresolved",
        "pinout_number_duplicate",
        "pinout_number_missing",
        "pinout_permutation_diagnosis",
        "bga_grid_mismatch",
        "pinout_pin1_corner_mismatch",
        "pinout_unverified",
        "pinout_view_unverified",
        "pinout_winding_nonstandard",
        "symbol_permutation_diagnosis",
        "symbol_pinout_name_mismatch",
        "connector_numbering_mirrored",
        "connector_board_edge_mismatch",
        "connector_board_edge_property_mismatch",
        "connector_not_at_board_edge",
        "connector_mating_clearance",
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
        "corpus_pad_geometry_mismatch",
        "land_pad_shape_mismatch",
        "exposed_pad_missing",
        "exposed_pad_mismatch",
        "pad_clearance",
        "pad_geometry",
        "pad_position",
        "pad_type",
        "pitch_exceeds_body",
        "footprint_pitch",
        "ep_size",
        "silk_over_pad",
        "connector_pad_type_mismatch",
        "connector_drill_mismatch",
        "connector_annular_ring",
        "connector_mechanical_missing",
        "connector_mechanical_pad_type",
        "connector_mechanical_mismatch",
        "tht_drill_mismatch",
        "tht_annular_ring",
        "footprint_pad_layer_mismatch",
        "connector_board_edge_graphic_mismatch",
        "connector_mating_board_interference",
        "coax_keepout_missing",
    ),
    "export_oracle": (
        "assembly_attribute",
        "export_gerber_pad_mismatch",
        "export_gerber_layer_mismatch",
        "export_mask_mismatch",
        "export_paste_mismatch",
        "export_drill_mismatch",
        "export_drill_missing",
        "export_oracle_unparsed",
        "model_export_mismatch",
        "model_export_missing",
        "model_export_unavailable",
        "model_export_pin1",
        "testboard_terminal_outside_pad",
        "testboard_kicad_cli",
        "testboard_pad_readback",
        "testboard_setup",
        "testboard_unavailable",
        "testboard_output_unavailable",
        "verification_output_unavailable",
    ),
    "model_geometry": (
        "model_geometry_mismatch",
        "model_body_dimension",
        "model_courtyard",
        "model_fab_outline",
        "model_footprint_unavailable",
        "model_format",
        "model_height",
        "model_inspection_unavailable",
        "model_invalid",
        "model_missing",
        "model_pad_unmatched",
        "model_pin1_mismatch",
        "model_pin1_unverifiable",
        "model_pitch",
        "model_roundtrip",
        "model_terminal_mismatch",
        "model_terminal_orientation_mismatch",
        "model_terminal_outside_pad",
        "model_terminal_unmatched",
        "model_terminals_unseparable",
        "model_transform_not_identity",
        "model_unresolved",
        "connector_model_mating_face",
        "model_mating_axis_mismatch",
        "connector_model_mechanical_mismatch",
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
        "corpus_canary_leak",
        "corpus_dimension_mismatch",
        "corpus_expected_pads_unavailable",
        "corpus_package_family_mismatch",
        "corpus_truth_incomplete",
        "datasheet_not_available",
        "footprint_unavailable",
        "model_unavailable",
        "partspec_unavailable",
        "symbol_unavailable",
        "testboard_drc",
        "rule_profile_cycle",
        "layer_order",
    ),
    "vision": (
        "glyph_loss_ambiguous",
        "pinout_vision_mismatch",
        "vision_compare_mismatch",
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
        "vision_table_mismatch",
        "vision_unparseable",
    ),
    "integrity": (
        "alternative_evidence_missing",
        "alternative_evidence_scope_exceeded",
        "alternative_evidence_stale",
        "alternative_evidence_used",
        "authoring_commit_unobserved",
        "authoring_lane_input_mismatch",
        "authoring_consensus_violated",
        "confidential_artifact_outside_store",
        "corpus_approval_binding_mismatch",
        "corpus_approval_event_invalid",
        "corpus_approval_unavailable",
        "corpus_confirmation_fields_missing",
        "corpus_confirmation_time_invalid",
        "corpus_manifest_changed_during_scoring",
        "corpus_truth_changed_during_scoring",
        "corpus_truth_unconfirmed",
        "datasheet_hash_mismatch",
        "datasheet_sha_mismatch",
        "evidence_sha_mismatch",
        "extraction_stale",
        "lineage_base",
        "lineage_evidence",
        "lineage_footprint_hash",
        "lineage_invalid",
        "lineage_stale_change",
        "lineage_unrecorded_change",
        "model_manifest_invalid",
        "package_identity_pdf_hash_mismatch",
        "parent_hash",
        "part_spec_unchecked",
        "provenance_missing",
        "provenance_sha_mismatch",
        "substitute_permission_missing",
        "substitute_permission_stale",
        "substitute_scope_exceeded",
        "vision_compare_missing",
        "vision_compare_stale",
        "vision_record_mismatch",
        "vision_record_missing",
    ),
    "package_identity": (
        "package_identity_ambiguous",
        "package_identity_body_mismatch",
        "package_identity_drawing_unresolved",
        "package_identity_lane_mismatch",
        "package_identity_mpn_unresolved",
        "package_identity_pin_count_mismatch",
        "package_identity_pin_count_unresolved",
        "package_identity_verification_unavailable",
        "pin_source_identity_mismatch",
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


def _number(value: float) -> str:
    return format(value, ".12g")


def _dimension_value(dimension: Dimension) -> float:
    if dimension.nom is not None:
        return dimension.nom
    if dimension.min is not None and dimension.max is not None:
        return (dimension.min + dimension.max) / 2
    if dimension.min is not None:
        return dimension.min
    if dimension.max is not None:
        return dimension.max
    raise MutationError("dimension has no usable value")


def _quoted(value: str) -> sexpr.QuotedString:
    return sexpr.quoted(value)


def _property(key: str, value: str) -> list[sexpr.SExpr]:
    return [
        "property",
        _quoted(key),
        _quoted(value),
        ["at", "0", "0", "0"],
        ["layer", _quoted("F.Fab")],
        ["effects", ["font", ["size", "1", "1"], ["thickness", "0.15"]]],
    ]


def _graphic_node(graphic: GraphicDef) -> list[sexpr.SExpr]:
    points = [[_number(x), _number(y)] for x, y in graphic.points]
    geometry: list[sexpr.SExpr]
    if graphic.kind == "poly":
        geometry = [["pts", *[["xy", *point] for point in points]]]
    else:
        keys = {
            "line": ("start", "end"),
            "rect": ("start", "end"),
            "circle": ("center", "end"),
            "arc": ("start", "mid", "end"),
        }[graphic.kind]
        geometry = [[key, *point] for key, point in zip(keys, points, strict=True)]
    name = {"poly": "fp_poly"}.get(graphic.kind, f"fp_{graphic.kind}")
    return [
        name,
        *geometry,
        ["stroke", ["width", _number(graphic.width)], ["type", "default"]],
        ["fill", "none"],
        ["layer", _quoted(graphic.layer)],
    ]


def _write_footprint(path: Path, footprint: FootprintDef) -> None:
    root: list[sexpr.SExpr] = [
        "footprint",
        _quoted(footprint.name),
        ["version", "20240108"],
        ["generator", _quoted("circuit_mutation")],
        ["layer", _quoted("F.Cu")],
        ["attr", *footprint.attributes],
        *[_property(key, value) for key, value in footprint.properties.items()],
    ]
    for graphic in footprint.graphics:
        root.append(_graphic_node(graphic))
    for pad in footprint.pads:
        node: list[sexpr.SExpr] = [
            "pad",
            _quoted(pad.number),
            pad.type,
            pad.shape,
            ["at", _number(pad.x), _number(pad.y), _number(pad.rotation)],
            ["size", _number(pad.width), _number(pad.height)],
            ["layers", *[_quoted(layer) for layer in pad.layers]],
        ]
        if pad.drill is not None:
            node.append(["drill", _number(pad.drill)])
        if pad.roundrect_ratio is not None:
            node.append(["roundrect_rratio", _number(pad.roundrect_ratio)])
        if pad.paste_margin is not None:
            node.append(["solder_paste_margin", _number(pad.paste_margin)])
        if pad.mask_margin is not None:
            node.append(["solder_mask_margin", _number(pad.mask_margin)])
        if pad.shape == "custom":
            if pad.polygon is None:
                raise MutationError(f"custom pad {pad.number} has no polygon")
            node.extend(
                [
                    ["options", ["clearance", "outline"], ["anchor", "rect"]],
                    [
                        "primitives",
                        [
                            "gr_poly",
                            [
                                "pts",
                                *[["xy", _number(x), _number(y)] for x, y in pad.polygon],
                            ],
                            ["width", "0.05"],
                            ["fill", "yes"],
                        ],
                    ],
                ]
            )
        root.append(node)
    for model in footprint.models:
        root.append(
            [
                "model",
                _quoted(model.path),
                ["offset", ["xyz", *[_number(value) for value in model.offset]]],
                ["scale", ["xyz", *[_number(value) for value in model.scale]]],
                ["rotate", ["xyz", *[_number(value) for value in model.rotate]]],
            ]
        )
    path.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")


def _write_symbol(path: Path, symbol: SymbolDef) -> None:
    properties = {"Reference": "U", "Value": symbol.name, **symbol.properties}
    property_nodes: list[sexpr.SExpr] = [
        ["property", _quoted(key), _quoted(value)] for key, value in properties.items()
    ]
    pins_by_unit: dict[int, list[sexpr.SExpr]] = {}
    for pin in symbol.pins:
        pins_by_unit.setdefault(pin.unit, []).append(
            [
                "pin",
                pin.electrical_type,
                pin.graphic_style,
                ["at", _number(pin.x), _number(pin.y), _number(pin.orientation)],
                ["length", _number(pin.length)],
                ["name", _quoted(pin.name)],
                ["number", _quoted(pin.number)],
            ]
        )
    symbol_units: list[sexpr.SExpr] = [
        [
            "symbol",
            _quoted(f"{symbol.name}_0_{unit}"),
            *pins_by_unit[unit],
        ]
        for unit in sorted(pins_by_unit)
    ]
    node: sexpr.SExpr = [
        "symbol",
        _quoted(symbol.name),
        *property_nodes,
        *symbol_units,
    ]
    root: sexpr.SExpr = [
        "kicad_symbol_lib",
        ["version", "20241209"],
        node,
    ]
    path.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")


def _source_path(source_spec_path: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else source_spec_path.parent / path


def _prepare_spec(
    spec: PartSpec,
    *,
    source_spec_path: Path | None,
    spec_unchanged: bool,
) -> PartSpec:
    if source_spec_path is None or spec_unchanged:
        return spec
    datasheet = spec.datasheet
    updates: dict[str, object] = {}
    if datasheet.path:
        updates["path"] = str(_source_path(source_spec_path, datasheet.path))
    updates["extraction_path"] = str(_source_path(source_spec_path, datasheet.extraction_path))
    if not updates:
        return spec
    return spec.model_copy(update={"datasheet": datasheet.model_copy(update=updates)})


def _copy_spec_evidence(spec: PartSpec, source_dir: Path, target_dir: Path) -> None:
    def visit(value: object, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in cast(dict[object, object], value).items():
                visit(child, child_key if isinstance(child_key, str) else None)
            return
        if isinstance(value, list):
            for child in cast(list[object], value):
                visit(child, key)
            return
        if key not in {
            "vision_record",
            "vision_read",
            "labels_vision_read",
            "orderable_vision_read",
        }:
            return
        if not isinstance(value, str):
            return
        reference = value.partition("#")[0] if "#" in value else value
        relative = Path(reference)
        if relative.is_absolute():
            return
        source = (source_dir / relative).resolve()
        if not source.is_relative_to(source_dir.resolve()) or not source.is_file():
            return
        target = target_dir / relative
        if key in {"vision_read", "labels_vision_read", "orderable_vision_read"}:
            source_batch_dir = source.parent
            relative_batch_dir = relative.parent
            target_batch_dir = target_dir / relative_batch_dir
            if not target_batch_dir.exists():
                shutil.copytree(source_batch_dir, target_batch_dir)
            try:
                batch_payload = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if not isinstance(batch_payload, dict):
                return
            state_reference = cast(dict[str, object], batch_payload).get("control_state_path")
            if not isinstance(state_reference, str):
                return
            state_path = Path(state_reference)
            if state_path.is_absolute():
                return
            source_state = (source.parent / state_path).resolve()
            if not source_state.is_relative_to(source_dir.resolve()) or not source_state.is_file():
                return
            relative_state = source_state.relative_to(source_dir.resolve())
            target_state = target_dir / relative_state
            target_state.parent.mkdir(parents=True, exist_ok=True)
            if not target_state.exists():
                shutil.copyfile(source_state, target_state)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    visit(spec.model_dump(mode="python"))


class _LibraryVerifier:
    def __init__(
        self,
        *,
        work_dir: Path,
        run_export_oracle: bool,
        density: Density,
        rules_profile: str,
        rules_dir: Path | None,
    ) -> None:
        self.work_dir = work_dir.resolve()
        self.run_export_oracle = run_export_oracle
        self.density: Density = density
        self.rules_profile = rules_profile
        self.rules_dir = rules_dir.resolve() if rules_dir is not None else None
        self.excluded_vision_findings = 0

    def __call__(self, artifacts: MutationArtifacts) -> Sequence[MutationFinding]:
        if self.run_export_oracle:
            command = kicad_cli.command_prefix()
            executable = command[0] if command else ""
            if not executable or (
                not Path(executable).is_file() and shutil.which(executable) is None
            ):
                raise MutationError("export oracle was requested but kicad-cli is unavailable")
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="library-mutation-", dir=self.work_dir))
            spec_unchanged = (
                artifacts.source_spec_path is not None
                and artifacts.spec_check_path is not None
                and artifacts.spec == load_part_spec(artifacts.source_spec_path)
            )
            spec = _prepare_spec(
                artifacts.spec,
                source_spec_path=artifacts.source_spec_path,
                spec_unchanged=spec_unchanged,
            )
            spec_path = directory / "part.spec.json"
            if spec_unchanged and artifacts.source_spec_path is not None:
                shutil.copyfile(artifacts.source_spec_path, spec_path)
            else:
                spec_path.write_text(
                    spec.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
            spec = spec.model_copy(deep=True)
            spec.bind_source_file(spec_path)
            if artifacts.source_spec_path is not None:
                _copy_spec_evidence(
                    spec,
                    artifacts.source_spec_path.resolve().parent,
                    directory,
                )
            symbol_lib = directory / "library.kicad_sym"
            _write_symbol(symbol_lib, artifacts.symbol)
            footprint_path = (
                directory / "Footprint.pretty" / f"{artifacts.footprint.name}.kicad_mod"
            )
            footprint_path.parent.mkdir(parents=True, exist_ok=True)
            model_path = directory / "model.step"
            shutil.copyfile(artifacts.model_path, model_path)
            manifest_source = Path(f"{artifacts.model_path}.gen.json")
            if manifest_source.is_file():
                shutil.copyfile(manifest_source, Path(f"{model_path}.gen.json"))
            model_reference = ModelRef(
                path=str(Path("..") / "model.step"),
                offset=(0.0, 0.0, 0.0),
                scale=(1.0, 1.0, 1.0),
                rotate=(0.0, 0.0, 0.0),
            )
            footprint = artifacts.footprint.model_copy(update={"models": [model_reference]})
            _write_footprint(footprint_path, footprint)
            check_path: Path | None = None
            if spec_unchanged and artifacts.spec_check_path is not None:
                check_path = directory / "part.spec.check.json"
                check = PartSpecReport.model_validate_json(
                    artifacts.spec_check_path.read_text(encoding="utf-8")
                )
                check_path.write_text(
                    check.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
            if artifacts.source_spec_path is not None and spec.authoring is not None:
                authoring_run_path = Path(spec.authoring)
                authoring_source = _source_path(
                    artifacts.source_spec_path,
                    authoring_run_path,
                )
                if not authoring_source.is_dir():
                    raise MutationError(f"authoring run is unavailable: {authoring_source}")
                authoring_target = directory / authoring_run_path
                authoring_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(authoring_source, authoring_target)
            rules = load_rules(self.rules_profile, self.rules_dir or directory)
            reference = compute_land_pattern(spec, self.density, rules=rules)
            from . import libverify

            verification = libverify.verify_library_part(
                spec,
                spec_path=spec_path,
                spec_check_path=check_path,
                symbol_lib=symbol_lib,
                symbol_name=artifacts.symbol.name,
                footprint_path=footprint_path,
                library_dir=None,
                reference=reference,
                rules=rules,
                model_required=True,
                klc=False,
                test_board=self.run_export_oracle,
                run_export_oracle=False,
            )
            findings = list(verification.findings)
            if self.run_export_oracle:
                export_report = verify_model_export(
                    spec,
                    footprint_path,
                    model_reference=str(model_path),
                    model_path=model_path,
                    rules=rules,
                    out_dir=directory / "export-oracle",
                )
                findings.extend(
                    VerifyFinding(
                        code=item.code,
                        severity=item.severity,
                        subject="model.export",
                        message=item.message,
                    )
                    for item in export_report.findings
                )
            filtered: list[MutationFinding] = []
            for finding in findings:
                if family_for_code(finding.code) == "vision":
                    self.excluded_vision_findings += 1
                else:
                    filtered.append(MutationFinding(code=finding.code, severity=finding.severity))
            return filtered
        except MutationError:
            raise
        except Exception as error:
            raise MutationError(f"real library verification failed: {error}") from error


def library_verifier(
    *,
    work_dir: Path,
    run_export_oracle: bool,
    density: Density = "nominal",
    rules_profile: str = "builtin:ipc7351b",
    rules_dir: Path | None = None,
) -> MutationVerifier:
    """Build a verifier backed by the production library and model oracles."""

    return _LibraryVerifier(
        work_dir=work_dir,
        run_export_oracle=run_export_oracle,
        density=density,
        rules_profile=rules_profile,
        rules_dir=rules_dir,
    )


def library_mutation_fixture(
    *,
    spec_path: Path,
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    model_path: Path,
    spec_check_path: Path | None = None,
    work_dir: Path,
    run_export_oracle: bool,
    density: Density = "nominal",
    seed: int = 0,
    rules_profile: str = "builtin:ipc7351b",
    rules_dir: Path | None = None,
    unexercised_codes: frozenset[str] = frozenset(),
) -> MutationFixture:
    """Load a library-part fixture and bind it to the real verification stack."""

    try:
        spec = load_part_spec(spec_path)
        symbol = parse_symbol(symbol_lib, symbol_name)
        footprint = parse_footprint(footprint_path)
        model = occt.read_step(model_path)
    except (OSError, ValueError) as error:
        raise MutationError(f"cannot load mutation fixture: {error}") from error
    verifier = library_verifier(
        work_dir=work_dir,
        run_export_oracle=run_export_oracle,
        density=density,
        rules_profile=rules_profile,
        rules_dir=rules_dir,
    )
    return MutationFixture(
        artifacts=MutationArtifacts(
            spec=spec,
            symbol=symbol,
            footprint=footprint,
            model=model,
            model_path=model_path,
            source_spec_path=spec_path,
            spec_check_path=spec_check_path,
        ),
        verify=verifier,
        seed=seed,
        export_oracle_run=run_export_oracle,
        rules_profile=rules_profile,
        rules_dir=rules_dir,
        unexercised_codes=unexercised_codes,
    )


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
    counting_family_count: int
    status: MutationStatus

    @model_validator(mode="after")
    def enforce_family_gate(self) -> MutationOutcome:
        actual_count = len(set(self.families).intersection(_COUNTING_FAMILIES))
        if actual_count != self.counting_family_count:
            raise ValueError("counting_family_count does not match families")
        if self.status == "single_oracle" and (
            not self.mutation.critical or not self.families or actual_count >= 2
        ):
            raise ValueError(
                "single_oracle requires a critical mutation with fewer than two counting families"
            )
        if self.status == "detected" and not self.families:
            raise ValueError("detected mutations must have at least one oracle family")
        if self.status == "detected" and self.mutation.critical and actual_count < 2:
            raise ValueError("critical mutations require two counting oracle families")
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
    excluded_vision_findings: int = 0
    export_oracle_run: bool = False
    unexercised_codes: list[str] = Field(default_factory=list)

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
    source_spec_path: Path | None = None
    spec_check_path: Path | None = None


MutationVerifier = Callable[[MutationArtifacts], Sequence[MutationFinding]]


@dataclass(frozen=True)
class MutationFixture:
    artifacts: MutationArtifacts
    verify: MutationVerifier
    seed: int = 0
    export_oracle_run: bool = False
    rules_profile: str = "builtin:ipc7351b"
    rules_dir: Path | None = None
    unexercised_codes: frozenset[str] = frozenset()


MutationTransform = Callable[
    [MutationArtifacts, random.Random],
    tuple[MutationArtifacts, dict[str, str | int | float | bool]],
]


def _always_applies(_artifacts: MutationArtifacts) -> bool:
    return True


@dataclass(frozen=True)
class MutationOperator:
    name: str
    target: MutationTarget
    critical: bool
    transform: MutationTransform
    applies: Callable[[MutationArtifacts], bool] = _always_applies

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
    if code in {
        "footprint_chirality_mismatch",
        "footprint_order_mismatch",
        "footprint_rotation_mismatch",
    }:
        return "orientation"
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
        artifacts.spec,
        symbol,
        artifacts.footprint,
        artifacts.model,
        artifacts.model_path,
        artifacts.source_spec_path,
        artifacts.spec_check_path,
    )


def _numeric_symbol_indices(symbol: SymbolDef) -> list[int]:
    indexed = [
        (int(pin.number), index) for index, pin in enumerate(symbol.pins) if pin.number.isdecimal()
    ]
    return [index for _, index in sorted(indexed)]


def _number_name_map(symbol: SymbolDef) -> dict[str, frozenset[str]]:
    mapping: dict[str, set[str]] = {}
    for pin in symbol.pins:
        if pin.number.isdecimal():
            mapping.setdefault(pin.number, set()).add(pin.name)
    return {number: frozenset(names) for number, names in mapping.items()}


def _symbol_adjacent_pin_swap(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    indices = _numeric_symbol_indices(artifacts.symbol)
    if len(indices) < 2:
        raise MutationError("symbol requires at least two numeric pins")
    pins = list(artifacts.symbol.pins)
    original_map = _number_name_map(artifacts.symbol)
    candidates: list[tuple[int, int]] = []
    for left, right in pairwise(indices):
        left_pin, right_pin = pins[left], pins[right]
        if (
            not left_pin.name.strip()
            or not right_pin.name.strip()
            or left_pin.name == right_pin.name
        ):
            continue
        candidate_pins = list(pins)
        candidate_pins[left] = left_pin.model_copy(update={"number": right_pin.number})
        candidate_pins[right] = right_pin.model_copy(update={"number": left_pin.number})
        candidate_symbol = artifacts.symbol.model_copy(update={"pins": candidate_pins})
        if _number_name_map(candidate_symbol) != original_map:
            candidates.append((left, right))
    if not candidates:
        raise MutationError("symbol has no adjacent pins whose number-to-name map can change")
    left, right = rng.choice(candidates)
    left_pin, right_pin = pins[left], pins[right]
    pins[left] = left_pin.model_copy(update={"number": right_pin.number})
    pins[right] = right_pin.model_copy(update={"number": left_pin.number})
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
    pins = list(artifacts.symbol.pins)
    original_map = _number_name_map(artifacts.symbol)

    def changes_mapping(left: int, right: int) -> bool:
        candidate_pins = list(pins)
        candidate_pins[left] = candidate_pins[left].model_copy(update={"name": pins[right].name})
        candidate_pins[right] = candidate_pins[right].model_copy(update={"name": pins[left].name})
        candidate_symbol = artifacts.symbol.model_copy(update={"pins": candidate_pins})
        return _number_name_map(candidate_symbol) != original_map

    pairs = [
        (left, right)
        for position, left in enumerate(indices)
        for right in indices[position + 1 :]
        if pins[left].name.strip()
        and pins[right].name.strip()
        and pins[left].name != pins[right].name
        and changes_mapping(left, right)
    ]
    if not pairs:
        raise MutationError("symbol name swap would not change its number-to-name map")
    left, right = rng.choice(pairs)
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
    original_map = _number_name_map(artifacts.symbol)
    pins: list[SymPin] = []
    for pin in artifacts.symbol.pins:
        if pin.number.isdecimal():
            pins.append(pin.model_copy(update={"number": str(int(pin.number) + 1)}))
        else:
            pins.append(pin)
    if _number_name_map(artifacts.symbol.model_copy(update={"pins": pins})) == original_map:
        raise MutationError("symbol number offset would not change its number-to-name map")
    return _replace_symbol_pins(artifacts, pins), {"offset": 1}


def _symbol_reversed_pin_order(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    indices = _numeric_symbol_indices(artifacts.symbol)
    if len(indices) < 2:
        raise MutationError("symbol requires at least two numeric pins")
    pins = list(artifacts.symbol.pins)
    distinct_names = {pins[index].name.strip() for index in indices if pins[index].name.strip()}
    if len(distinct_names) < 2:
        raise MutationError("symbol requires at least two distinct names")
    original_map = _number_name_map(artifacts.symbol)
    numbers = [pins[index].number for index in indices][::-1]
    for index, number in zip(indices, numbers, strict=True):
        pins[index] = pins[index].model_copy(update={"number": number})
    if _number_name_map(artifacts.symbol.model_copy(update={"pins": pins})) == original_map:
        raise MutationError("reversing symbol pin numbers would not change its number-to-name map")
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
        artifacts.spec,
        artifacts.symbol,
        footprint,
        artifacts.model,
        artifacts.model_path,
        artifacts.source_spec_path,
        artifacts.spec_check_path,
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
    auxiliary_numbers = artifacts.spec.package.auxiliary_pad_numbers
    choices = [
        index
        for index, pad in enumerate(artifacts.footprint.pads)
        if pad.number and not (exclude_exposed and pad.number in auxiliary_numbers)
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
    shift_x = pad.number in artifacts.spec.package.auxiliary_pad_numbers or abs(pad.x) < abs(pad.y)
    update = {"x": pad.x + 0.1} if shift_x else {"y": pad.y + 0.1}
    pads[index] = pad.model_copy(update=update)
    shift_axis = "x" if shift_x else "y"
    return _transform_footprint(artifacts, pads), {
        "pad": pad.number,
        f"d{shift_axis}_mm": 0.1,
    }


def _footprint_pad_wrong_copper_layer(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    candidates = [
        index
        for index, pad in enumerate(artifacts.footprint.pads)
        if pad.type == "smd" and "F.Cu" in pad.layers
    ]
    if not candidates:
        raise MutationError("footprint has no front-side SMD pad")
    index = rng.choice(candidates)
    pads = list(artifacts.footprint.pads)
    pad = pads[index]
    layers = ["B.Cu" if layer == "F.Cu" else layer for layer in pad.layers]
    pads[index] = pad.model_copy(update={"layers": layers})
    return _transform_footprint(artifacts, pads), {
        "pad": pad.number,
        "from_layer": "F.Cu",
        "to_layer": "B.Cu",
    }


def _footprint_pitch_scale(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    pads = [
        pad.model_copy(update={"x": pad.x * 1.02, "y": pad.y * 1.02})
        if pad.number not in artifacts.spec.package.auxiliary_pad_numbers
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


def _footprint_pad_rotation_change(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    candidates = [
        index
        for index, pad in enumerate(artifacts.footprint.pads)
        if pad.shape != "custom" and not math.isclose(pad.width, pad.height, abs_tol=0.02)
    ]
    if not candidates:
        raise MutationError("footprint has no asymmetric non-custom pad to rotate")
    index = candidates[0]
    pads = list(artifacts.footprint.pads)
    pad = pads[index]
    rotation = (pad.rotation + 90.0) % 360.0
    pads[index] = pad.model_copy(update={"rotation": rotation})
    return _transform_footprint(artifacts, pads), {
        "pad": pad.number,
        "from_rotation_deg": pad.rotation,
        "to_rotation_deg": rotation,
    }


def _footprint_polygon_vertex_shift(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    candidates = [
        (index, pad)
        for index, pad in enumerate(artifacts.footprint.pads)
        if pad.shape == "custom" and pad.polygon is not None
    ]
    if not candidates:
        raise MutationError("footprint has no custom polygon pad")
    index, pad = next(
        (
            (index, candidate)
            for index, candidate in candidates
            if candidate.number in artifacts.spec.package.auxiliary_pad_numbers
        ),
        candidates[0],
    )
    polygon = pad.polygon
    if polygon is None:
        raise MutationError("custom pad polygon is unavailable")
    right_edge = max(x for x, _ in polygon)
    shift = max(0.2, pad.width * 0.75)
    shifted = [
        (x - shift, y) if math.isclose(x, right_edge, abs_tol=1e-9) else (x, y) for x, y in polygon
    ]
    if shifted == polygon:
        raise MutationError("custom polygon has no shiftable rightmost vertex")
    pads = list(artifacts.footprint.pads)
    pads[index] = pad.model_copy(update={"polygon": shifted})
    return _transform_footprint(artifacts, pads), {
        "pad": pad.number,
        "vertex_shift_x_mm": -shift,
    }


def _footprint_exposed_pad_drop(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    numbers = {pad.number for pad in artifacts.spec.package.all_exposed_pads}
    index = next(
        (index for index, pad in enumerate(artifacts.footprint.pads) if pad.number in numbers),
        None,
    )
    if index is None:
        raise MutationError("footprint has no exposed pad to remove")
    pads = list(artifacts.footprint.pads)
    removed = pads.pop(index)
    return _transform_footprint(artifacts, pads), {"pad": removed.number}


def _pad_from_land_pad(pad: LandPad) -> PadDef:
    return PadDef(
        number=pad.number,
        type="smd",
        shape="custom" if pad.shape == "polygon" else pad.shape,
        x=pad.x,
        y=pad.y,
        rotation=pad.rotation,
        width=pad.width,
        height=pad.height,
        drill=None,
        layers=["F.Cu", "F.Paste", "F.Mask"],
        roundrect_ratio=0.25 if pad.shape == "roundrect" else None,
        paste_margin=None,
        mask_margin=None,
        polygon=pad.polygon,
    )


def _footprint_depopulated_pin_added(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    missing = artifacts.spec.package.missing_pins
    if not missing:
        raise MutationError("PartSpec has no depopulated pin")
    number = missing[0]
    package = artifacts.spec.package
    position_package = package.model_copy(
        update={
            "pin_count": package.pin_count + 1,
            "missing_pins": missing[1:],
        }
    )
    position_spec = artifacts.spec.model_copy(update={"package": position_package})
    position = next(
        (pad for pad in compute_land_pattern(position_spec).pads if pad.number == number),
        None,
    )
    if position is None:
        raise MutationError(f"cannot derive the position of depopulated pin {number}")
    return (
        _transform_footprint(artifacts, [*artifacts.footprint.pads, _pad_from_land_pad(position)]),
        {"pin": number},
    )


def _footprint_bga_row_swap(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    grid = artifacts.spec.package.ball_grid
    if grid is None or len(grid.rows) < 2:
        raise MutationError("BGA grid requires at least two rows")
    first, second = grid.rows[:2]
    pads = [
        pad.model_copy(
            update={
                "number": (
                    f"{second}{pad.number[len(first) :]}"
                    if pad.number.startswith(first)
                    else f"{first}{pad.number[len(second) :]}"
                    if pad.number.startswith(second)
                    else pad.number
                )
            }
        )
        for pad in artifacts.footprint.pads
    ]
    if pads == artifacts.footprint.pads:
        raise MutationError("footprint has no populated sites in the first two BGA rows")
    return _transform_footprint(artifacts, pads), {
        "first_row": first,
        "second_row": second,
    }


def _part_spec_tab_offset_shift(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    def nominal(dimension: Dimension) -> float | None:
        if dimension.nom is not None:
            return dimension.nom
        if dimension.min is not None and dimension.max is not None:
            return (dimension.min + dimension.max) / 2
        return dimension.min if dimension.min is not None else dimension.max

    tab = artifacts.spec.package.tab
    if tab is None:
        raise MutationError("PartSpec has no tab")
    offset = nominal(tab.offset)
    tab_width = nominal(tab.width)
    tab_length = nominal(tab.length)
    if offset is None or tab_width is None or tab_length is None:
        raise MutationError("tab offset has no usable value")
    shift = 0.5
    shifted_offset = tab.offset.model_copy(
        update={
            "min": None if tab.offset.min is None else tab.offset.min + shift,
            "nom": offset + shift,
            "max": None if tab.offset.max is None else tab.offset.max + shift,
        }
    )
    shifted_tab = tab.model_copy(update={"offset": shifted_offset})
    package = artifacts.spec.package.model_copy(update={"tab": shifted_tab})
    spec = artifacts.spec.model_copy(update={"package": package})
    old_y = -offset
    solids = list(occt.solids(artifacts.model))
    facts = occt.inspect(artifacts.model).solids
    matches = [
        index
        for index, fact in enumerate(facts)
        if abs((fact.bbox.x_min + fact.bbox.x_max) / 2) <= 0.02
        and abs((fact.bbox.y_min + fact.bbox.y_max) / 2 - old_y) <= 0.02
        and abs((fact.bbox.x_max - fact.bbox.x_min) - tab_width) <= 0.05
        and abs((fact.bbox.y_max - fact.bbox.y_min) - tab_length) <= 0.05
    ]
    if len(matches) != 1:
        raise MutationError("cannot uniquely locate the tab terminal in the 3D model")
    solids[matches[0]] = occt.transform(solids[matches[0]], translation=(0.0, -shift, 0.0))
    return (
        MutationArtifacts(
            spec,
            artifacts.symbol,
            artifacts.footprint,
            occt.compound(solids),
            artifacts.model_path,
            artifacts.source_spec_path,
            artifacts.spec_check_path,
        ),
        {"offset_shift_mm": shift},
    )


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


def _connector_numbering_mirror(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    connector = artifacts.spec.connector
    if connector is None:
        raise MutationError("connector PartSpec is required")
    pads = list(artifacts.footprint.pads)
    mirrored_numbers: list[str] = []
    for row in connector.contacts:
        numbers = [connector.numbering.manufacturer_to_kicad[number] for number in row.numbers]
        indices = [
            index
            for index, pad in enumerate(pads)
            if pad.number in numbers and pad.type != "np_thru_hole"
        ]
        if len(indices) < 2:
            continue
        x0 = min(pads[index].x for index in indices)
        x1 = max(pads[index].x for index in indices)
        for index in indices:
            pad = pads[index]
            pads[index] = pad.model_copy(update={"x": x0 + x1 - pad.x})
            mirrored_numbers.append(pad.number)
    if not mirrored_numbers:
        raise MutationError("connector has no multi-contact row to mirror")
    return _transform_footprint(artifacts, pads), {
        "pads": ",".join(sorted(mirrored_numbers)),
    }


def _connector_tht_drill_shrink(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    index = next(
        (
            index
            for index, pad in enumerate(artifacts.footprint.pads)
            if pad.type == "thru_hole" and pad.drill is not None and pad.drill > 0.2
        ),
        None,
    )
    if index is None:
        raise MutationError("connector footprint has no shrinkable plated drill")
    pads = list(artifacts.footprint.pads)
    pad = pads[index]
    drill = pad.drill
    if drill is None:
        raise MutationError("connector footprint has no plated drill")
    pads[index] = pad.model_copy(update={"drill": drill - 0.2})
    return _transform_footprint(artifacts, pads), {
        "pad": pad.number,
        "drill_delta_mm": -0.2,
    }


def _connector_npth_to_pth(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    pads = list(artifacts.footprint.pads)
    index = next(
        (index for index, pad in enumerate(pads) if pad.type == "np_thru_hole"),
        None,
    )
    if index is None:
        raise MutationError("connector footprint has no non-plated hole")
    pad = pads[index]
    pads[index] = pad.model_copy(update={"type": "thru_hole"})
    return _transform_footprint(artifacts, pads), {"pad": pad.number}


def _connector_mounting_pad_drop(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    connector = artifacts.spec.connector
    if connector is None:
        raise MutationError("connector PartSpec is required")
    feature = next(
        (feature for feature in connector.mechanical if feature.kind == "mounting"),
        None,
    )
    if feature is None:
        raise MutationError("connector has no mounting feature")
    x, y = _dimension_value(feature.x), _dimension_value(feature.y)
    candidates = [
        (index, pad)
        for index, pad in enumerate(artifacts.footprint.pads)
        if (pad.number == feature.number if feature.number is not None else not pad.number)
    ]
    selected = min(
        candidates,
        key=lambda item: math.dist((item[1].x, item[1].y), (x, y)),
        default=None,
    )
    if selected is None or math.dist((selected[1].x, selected[1].y), (x, y)) > 0.02:
        raise MutationError("connector mounting pad is absent")
    pads = list(artifacts.footprint.pads)
    removed = pads.pop(selected[0])
    return _transform_footprint(artifacts, pads), {
        "pad": removed.number,
        "kind": "mounting",
    }


def _connector_board_edge_offset_shift(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    connector = artifacts.spec.connector
    if connector is None or connector.board_edge is None:
        raise MutationError("connector board-edge specification is required")
    edge = connector.board_edge
    offset = _dimension_value(edge.offset)
    properties = dict(artifacts.footprint.properties)
    properties["circuit_board_edge"] = f"{edge.side} {offset + 0.5:.4f}".rstrip("0").rstrip(".")
    graphics = [
        graphic.model_copy(
            update={
                "points": [
                    (
                        x + 0.5 if edge.side in {"+x", "-x"} else x,
                        y + 0.5 if edge.side in {"+y", "-y"} else y,
                    )
                    for x, y in graphic.points
                ]
            }
        )
        if (
            graphic.layer == "Dwgs.User"
            and graphic.kind == "line"
            and graphic.points
            and abs(
                (
                    sum(point[0] for point in graphic.points) / len(graphic.points)
                    if edge.side in {"+x", "-x"}
                    else sum(point[1] for point in graphic.points) / len(graphic.points)
                )
                - offset
            )
            <= 0.1
        )
        else graphic
        for graphic in artifacts.footprint.graphics
    ]
    footprint = artifacts.footprint.model_copy(
        update={"properties": properties, "graphics": graphics}
    )
    changed = MutationArtifacts(
        artifacts.spec,
        artifacts.symbol,
        footprint,
        artifacts.model,
        artifacts.model_path,
        artifacts.source_spec_path,
        artifacts.spec_check_path,
    )
    return changed, {"side": edge.side, "offset_delta_mm": 0.5}


def _connector_mating_axis_flip(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    connector = artifacts.spec.connector
    if connector is None or connector.board_edge is None:
        raise MutationError("connector board-edge specification is required")
    old_axis = connector.mating_axis
    new_axis = {
        "+x": "-x",
        "-x": "+x",
        "+y": "-y",
        "-y": "+y",
    }[old_axis]
    edge = connector.board_edge
    offset = edge.offset
    flipped_offset = offset.model_copy(
        update={
            "min": -offset.max if offset.max is not None else None,
            "nom": -offset.nom if offset.nom is not None else None,
            "max": -offset.min if offset.min is not None else None,
        }
    )
    flipped_edge = edge.model_copy(update={"side": new_axis, "offset": flipped_offset})
    flipped_connector = connector.model_copy(
        update={"mating_axis": new_axis, "board_edge": flipped_edge}
    )
    spec = artifacts.spec.model_copy(update={"connector": flipped_connector})
    changed = MutationArtifacts(
        spec,
        artifacts.symbol,
        artifacts.footprint,
        artifacts.model,
        artifacts.model_path,
        artifacts.source_spec_path,
        artifacts.spec_check_path,
    )
    return changed, {"from": old_axis, "to": new_axis}


def _part_spec_package_update(
    artifacts: MutationArtifacts,
    updates: dict[str, object],
) -> MutationArtifacts:
    package = artifacts.spec.package.model_copy(update=updates)
    spec = artifacts.spec.model_copy(update={"package": package})
    return MutationArtifacts(
        spec,
        artifacts.symbol,
        artifacts.footprint,
        artifacts.model,
        artifacts.model_path,
        artifacts.source_spec_path,
        artifacts.spec_check_path,
    )


def _part_spec_column_shift(
    artifacts: MutationArtifacts,
    rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    dimensions = (
        ("body_length", artifacts.spec.package.body_length),
        ("body_width", artifacts.spec.package.body_width),
        ("height", artifacts.spec.package.height),
        ("pitch", artifacts.spec.package.pitch),
        ("standoff", artifacts.spec.package.standoff),
        ("lead_span", artifacts.spec.package.lead_span),
        ("lead_length", artifacts.spec.package.lead_length),
        ("lead_width", artifacts.spec.package.lead_width),
    )
    available = [
        (field, dimension)
        for field, dimension in dimensions
        if dimension is not None
        and len(
            {value for value in (dimension.min, dimension.nom, dimension.max) if value is not None}
        )
        > 1
    ]
    if not available:
        raise MutationError("PartSpec has no populated mechanical dimension")
    field, dimension = rng.choice(available)
    source_columns: list[tuple[str, float]] = []
    if dimension.min is not None and dimension.min != dimension.nom:
        source_columns.append(("min", dimension.min))
    if dimension.max is not None and dimension.max != dimension.nom:
        source_columns.append(("max", dimension.max))
    source_column, shifted_value = rng.choice(source_columns)
    shifted = dimension.model_copy(
        update={"min": shifted_value, "nom": shifted_value, "max": shifted_value}
    )
    return _part_spec_package_update(artifacts, {field: shifted}), {
        "field": field,
        "shift": f"min,nom,max<-{source_column}",
    }


def _part_spec_flip_drawing_view(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    current = artifacts.spec.package.drawing_view
    updated = "bottom" if current == "top" else "top"
    mutated = _part_spec_package_update(artifacts, {"drawing_view": updated})
    if mutated.spec.pinout is not None:
        pinout = mutated.spec.pinout.model_copy(update={"view": updated})
        mutated = replace(
            mutated,
            spec=mutated.spec.model_copy(update={"pinout": pinout}),
        )
    return mutated, {
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
            artifacts.source_spec_path,
            artifacts.spec_check_path,
        ),
        {"mpn": updated},
    )


def _part_spec_sibling_package_variant(
    artifacts: MutationArtifacts,
    _rng: random.Random,
) -> tuple[MutationArtifacts, dict[str, str | int | float | bool]]:
    datasheet_path = Path(artifacts.spec.datasheet.path)
    if not datasheet_path.is_absolute() and artifacts.source_spec_path is not None:
        datasheet_path = artifacts.source_spec_path.resolve().parent / datasheet_path
    sibling = sibling_package_mpn(datasheet_path, artifacts.spec.mpn)
    sibling_source = "pdf"
    if sibling is None:
        sibling = f"{artifacts.spec.mpn}-SIBLING-PACKAGE"
        sibling_source = "synthetic"
    orderable = [
        item.model_copy(update={"mpn": sibling}) if index == 0 else item
        for index, item in enumerate(artifacts.spec.orderable)
    ]
    spec = artifacts.spec.model_copy(update={"mpn": sibling, "orderable": orderable})
    return (
        MutationArtifacts(
            spec,
            artifacts.symbol,
            artifacts.footprint,
            artifacts.model,
            artifacts.model_path,
            artifacts.source_spec_path,
            artifacts.spec_check_path,
        ),
        {"mpn": sibling, "sibling_source": sibling_source},
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
                artifacts.source_spec_path,
                artifacts.spec_check_path,
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
            artifacts.source_spec_path,
            artifacts.spec_check_path,
        ),
        {"body_solid": body_index},
    )


def _has_asymmetric_pad(artifacts: MutationArtifacts) -> bool:
    return any(
        pad.shape != "custom" and not math.isclose(pad.width, pad.height, abs_tol=0.02)
        for pad in artifacts.footprint.pads
    )


def _has_front_smd_pad(artifacts: MutationArtifacts) -> bool:
    return any(pad.type == "smd" and "F.Cu" in pad.layers for pad in artifacts.footprint.pads)


def _has_custom_pad(artifacts: MutationArtifacts) -> bool:
    return any(
        pad.shape == "custom" and pad.polygon is not None for pad in artifacts.footprint.pads
    )


def _has_exposed_pad(artifacts: MutationArtifacts) -> bool:
    return bool(artifacts.spec.package.all_exposed_pads)


def _has_depopulated_pin(artifacts: MutationArtifacts) -> bool:
    return bool(artifacts.spec.package.missing_pins)


def _has_bga_rows(artifacts: MutationArtifacts) -> bool:
    grid = artifacts.spec.package.ball_grid
    return grid is not None and len(grid.rows) >= 2


def _has_tab(artifacts: MutationArtifacts) -> bool:
    return artifacts.spec.package.tab is not None


def _has_connector_numbered_row(artifacts: MutationArtifacts) -> bool:
    connector = artifacts.spec.connector
    return connector is not None and any(len(row.numbers) > 1 for row in connector.contacts)


def _has_connector_tht_pad(artifacts: MutationArtifacts) -> bool:
    return any(
        pad.type == "thru_hole" and pad.drill is not None and pad.drill > 0.2
        for pad in artifacts.footprint.pads
    )


def _has_connector_npth_pad(artifacts: MutationArtifacts) -> bool:
    return any(pad.type == "np_thru_hole" for pad in artifacts.footprint.pads)


def _has_connector_mounting_feature(artifacts: MutationArtifacts) -> bool:
    connector = artifacts.spec.connector
    return connector is not None and any(
        feature.kind == "mounting" for feature in connector.mechanical
    )


def _has_connector_board_edge(artifacts: MutationArtifacts) -> bool:
    connector = artifacts.spec.connector
    return connector is not None and connector.board_edge is not None


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
    MutationOperator(
        "footprint_pad_wrong_copper_layer",
        "footprint",
        True,
        _footprint_pad_wrong_copper_layer,
        _has_front_smd_pad,
    ),
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
        "connector_numbering_mirror",
        "footprint",
        True,
        _connector_numbering_mirror,
        _has_connector_numbered_row,
    ),
    MutationOperator(
        "tht_drill_shrink",
        "footprint",
        True,
        _connector_tht_drill_shrink,
        _has_connector_tht_pad,
    ),
    MutationOperator(
        "npth_to_pth",
        "footprint",
        True,
        _connector_npth_to_pth,
        _has_connector_npth_pad,
    ),
    MutationOperator(
        "mounting_pad_drop",
        "footprint",
        True,
        _connector_mounting_pad_drop,
        _has_connector_mounting_feature,
    ),
    MutationOperator(
        "board_edge_offset_shift",
        "footprint",
        True,
        _connector_board_edge_offset_shift,
        _has_connector_board_edge,
    ),
    MutationOperator(
        "mating_axis_flip",
        "part_spec",
        True,
        _connector_mating_axis_flip,
        _has_connector_board_edge,
    ),
    MutationOperator(
        "pad_rotation_change",
        "footprint",
        True,
        _footprint_pad_rotation_change,
        _has_asymmetric_pad,
    ),
    MutationOperator(
        "polygon_vertex_shift",
        "footprint",
        True,
        _footprint_polygon_vertex_shift,
        _has_custom_pad,
    ),
    MutationOperator(
        "exposed_pad_drop",
        "footprint",
        True,
        _footprint_exposed_pad_drop,
        _has_exposed_pad,
    ),
    MutationOperator(
        "depopulated_pin_added",
        "footprint",
        True,
        _footprint_depopulated_pin_added,
        _has_depopulated_pin,
    ),
    MutationOperator(
        "bga_row_swap",
        "footprint",
        True,
        _footprint_bga_row_swap,
        _has_bga_rows,
    ),
    MutationOperator(
        "tab_offset_shift",
        "part_spec",
        True,
        _part_spec_tab_offset_shift,
        _has_tab,
    ),
    MutationOperator(
        "partspec_min_nom_max_column_shift", "part_spec", True, _part_spec_column_shift
    ),
    MutationOperator("partspec_drawing_view_flip", "part_spec", True, _part_spec_flip_drawing_view),
    MutationOperator(
        "partspec_pin1_corner_rotation", "part_spec", True, _part_spec_rotate_pin1_corner
    ),
    MutationOperator("partspec_sibling_package_mpn", "part_spec", True, _part_spec_sibling_mpn),
    MutationOperator(
        "partspec_sibling_package_variant",
        "part_spec",
        True,
        _part_spec_sibling_package_variant,
    ),
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
    baseline_errors = [
        item.code
        for item in baseline
        if item.code not in fixture.unexercised_codes
        and item.severity == "error"
        and family_for_code(item.code) in _COUNTING_FAMILIES
    ]
    if baseline_errors:
        details = ", ".join(sorted(baseline_errors))
        raise MutationError(f"mutation fixture is not known-good; baseline errors: {details}")
    baseline_counts = Counter(
        (item.code, item.severity)
        for item in baseline
        if item.code not in fixture.unexercised_codes
    )
    outcomes: list[MutationOutcome] = []
    family_hits: Counter[CheckFamily] = Counter()
    single_oracle: list[str] = []
    undetected: list[str] = []

    for operator in (
        candidate for candidate in MUTATION_OPERATORS if candidate.applies(fixture.artifacts)
    ):
        mutated, record = operator.apply(fixture.artifacts, fixture.seed)
        if record.target == "model" or mutated.model is not fixture.artifacts.model:
            with tempfile.TemporaryDirectory(prefix="circuit-mutation-") as directory:
                model_path = Path(directory) / "mutated.step"
                occt.write_step(mutated.model, model_path, product_name=mutated.spec.mpn)
                manifest_source = Path(f"{fixture.artifacts.model_path}.gen.json")
                if record.target != "model" and manifest_source.is_file():
                    shutil.copyfile(manifest_source, Path(f"{model_path}.gen.json"))
                verified = list(fixture.verify(replace(mutated, model_path=model_path)))
        else:
            verified = list(fixture.verify(mutated))
        remaining_baseline = dict(baseline_counts)
        new_codes: list[str] = []
        for finding in verified:
            if finding.code in fixture.unexercised_codes:
                continue
            key = (finding.code, finding.severity)
            remaining = remaining_baseline.get(key, 0)
            if remaining:
                remaining_baseline[key] = remaining - 1
            else:
                new_codes.append(finding.code)
        unique_codes = sorted(set(new_codes))
        families: list[CheckFamily] = sorted({family_for_code(code) for code in unique_codes})
        family_hits.update(families)
        counting_count = len(set(families).intersection(_COUNTING_FAMILIES))
        if not families:
            status: MutationStatus = "undetected"
            undetected.append(record.operator)
        elif record.critical and counting_count < 2:
            status = "single_oracle"
            single_oracle.append(record.operator)
        else:
            status = "detected"
        outcomes.append(
            MutationOutcome(
                mutation=record,
                finding_codes=unique_codes,
                families=families,
                counting_family_count=counting_count,
                status=status,
            )
        )

    total = len(outcomes)
    family_keys: tuple[CheckFamily, ...] = (*_COUNTING_FAMILIES, "vision", "integrity")
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
        unexercised_codes=sorted(fixture.unexercised_codes),
        excluded_vision_findings=(
            fixture.verify.excluded_vision_findings
            if isinstance(fixture.verify, _LibraryVerifier)
            else 0
        ),
        export_oracle_run=fixture.export_oracle_run,
    )
