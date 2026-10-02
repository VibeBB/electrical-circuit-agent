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

from pydantic import BaseModel, ConfigDict, model_validator

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
from .partspec import Dimension, PartSpec, PartSpecReport, load_part_spec
from .ruleprofile import load_rules

CheckFamily = Literal[
    "evidence",
    "pin_bijection",
    "orientation",
    "land_geometry",
    "export_oracle",
    "model_geometry",
    "rule_profile",
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
    ),
    "pin_bijection": (
        "duplicate_pin",
        "exposed_pad_unmapped",
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
        "corpus_pin_map_mismatch",
        "corpus_symbol_pin_map_mismatch",
        "symbol_pin_grid",
        "symbol_pin_name",
        "symbol_pin_set",
        "symbol_pin_type",
        "symbol_property",
        "testboard_erc",
        "testboard_pinmap",
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
        "pinout_pin1_corner_mismatch",
        "pinout_unverified",
        "pinout_view_unverified",
        "pinout_winding_nonstandard",
        "symbol_permutation_diagnosis",
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
        "corpus_pad_geometry_mismatch",
        "pad_clearance",
        "pad_geometry",
        "pad_position",
        "pad_type",
        "pitch_exceeds_body",
        "footprint_pitch",
        "ep_size",
        "silk_over_pad",
    ),
    "export_oracle": (
        "assembly_attribute",
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
        "authoring_commit_unobserved",
        "authoring_lane_input_mismatch",
        "authoring_consensus_violated",
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
        "parent_hash",
        "part_spec_unchecked",
        "provenance_missing",
        "provenance_sha_mismatch",
        "vision_compare_missing",
        "vision_compare_stale",
        "vision_record_mismatch",
        "vision_record_missing",
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
    pins: list[sexpr.SExpr] = []
    for pin in symbol.pins:
        pins.append(
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
    symbol_unit: sexpr.SExpr = [
        "symbol",
        _quoted(f"{symbol.name}_0_1"),
        *pins,
    ]
    node: sexpr.SExpr = [
        "symbol",
        _quoted(symbol.name),
        *property_nodes,
        symbol_unit,
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
    spec_check_path: Path | None,
) -> PartSpec:
    if source_spec_path is None or spec_check_path is not None:
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
    ) -> None:
        self.work_dir = work_dir.resolve()
        self.run_export_oracle = run_export_oracle
        self.density: Density = density
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
            spec = _prepare_spec(
                artifacts.spec,
                source_spec_path=artifacts.source_spec_path,
                spec_check_path=artifacts.spec_check_path,
            )
            spec_path = directory / "part.spec.json"
            if (
                artifacts.source_spec_path is not None
                and artifacts.spec_check_path is not None
                and spec == load_part_spec(artifacts.source_spec_path)
            ):
                shutil.copyfile(artifacts.source_spec_path, spec_path)
            else:
                spec_path.write_text(
                    spec.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
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
            if artifacts.spec_check_path is not None:
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
            rules = load_rules("builtin:ipc7351b", directory)
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
) -> MutationVerifier:
    """Build a verifier backed by the production library and model oracles."""

    return _LibraryVerifier(
        work_dir=work_dir,
        run_export_oracle=run_export_oracle,
        density=density,
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
        and dimension.nom is not None
        and any(
            value is not None and value != dimension.nom for value in (dimension.min, dimension.max)
        )
    ]
    if not available:
        raise MutationError("PartSpec has no populated mechanical dimension")
    field = rng.choice(available)
    dimension = getattr(artifacts.spec.package, field)
    if not isinstance(dimension, Dimension):
        raise MutationError(f"PartSpec dimension {field} is unavailable")
    source_column = rng.choice(
        [
            column
            for column in ("min", "max")
            if (value := getattr(dimension, column)) is not None and value != dimension.nom
        ]
    )
    shifted_value = getattr(dimension, source_column)
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
    baseline_errors = [
        item.code
        for item in baseline
        if item.severity == "error" and family_for_code(item.code) in _COUNTING_FAMILIES
    ]
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
        excluded_vision_findings=(
            fixture.verify.excluded_vision_findings
            if isinstance(fixture.verify, _LibraryVerifier)
            else 0
        ),
        export_oracle_run=fixture.export_oracle_run,
    )
