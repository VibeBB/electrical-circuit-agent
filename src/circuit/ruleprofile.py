"""Layered, hash-bound manufacturing rule profiles."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .landpattern import Density, Family

Layer = Literal["standard", "manufacturer", "organization", "product"]
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LAYER_ORDER = {"standard": 0, "manufacturer": 1, "organization": 2, "product": 3}


class RuleProfileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "standard",
        "datasheet",
        "prototype",
        "assembly_report",
        "yield_data",
        "org_guideline",
    ]
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256.pattern)
    note: str = ""


class GoalOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    toe: float | None = None
    heel: float | None = None
    side: float | None = None
    courtyard: float | None = None

    @model_validator(mode="after")
    def validate_finite(self) -> GoalOverride:
        if any(
            value is not None and not math.isfinite(value)
            for value in (self.toe, self.heel, self.side, self.courtyard)
        ):
            raise ValueError("goal overrides must be finite")
        return self


class PasteRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage_min: float = Field(ge=0, le=1)
    coverage_max: float = Field(ge=0, le=1)
    ep_coverage_min: float = Field(ge=0, le=1)
    ep_coverage_max: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_ranges(self) -> PasteRule:
        if self.coverage_min > self.coverage_max:
            raise ValueError("paste coverage minimum exceeds maximum")
        if self.ep_coverage_min > self.ep_coverage_max:
            raise ValueError("EP paste coverage minimum exceeds maximum")
        return self


class RuleProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_rule_profile"]
    profile_id: str
    layer: Layer
    parent: str | None
    parent_sha256: str | None = Field(default=None, pattern=_SHA256.pattern)
    standard: Literal["ipc7351b", "kicad_generator", "datasheet", "custom"] | None = None
    density: Density | None = None
    fabrication_tolerance: float | None = None
    placement_tolerance: float | None = None
    goal_overrides: dict[Family, GoalOverride] = Field(
        default_factory=lambda: dict[Family, GoalOverride]()
    )
    min_pad_clearance_mm: float | None = None
    min_ep_to_pad_clearance_mm: float | None = None
    min_mask_web_mm: float | None = None
    paste: PasteRule | None = None
    rationale: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=lambda: list[EvidenceRef]())
    author: str = ""
    created_at: str = ""

    @model_validator(mode="after")
    def validate_profile(self) -> RuleProfile:
        if self.profile_id not in _BUILTIN_IDS and not _PROFILE_ID.fullmatch(self.profile_id):
            raise ValueError("profile_id must match ^[a-z0-9][a-z0-9_.-]*$")
        if self.profile_id.startswith("builtin:") and self.profile_id not in _BUILTIN_IDS:
            raise ValueError("builtin profile ids are reserved")
        if self.parent is None:
            if not self.profile_id.startswith("builtin:"):
                raise ValueError("only built-in profiles may omit parent")
            if self.parent_sha256 is not None:
                raise ValueError("parent_sha256 requires a parent")
        elif self.parent_sha256 is None:
            raise ValueError("parent_sha256 is required when parent is set")
        if self.parent is not None and not (
            self.parent in _BUILTIN_IDS or _PROFILE_ID.fullmatch(self.parent)
        ):
            raise ValueError("parent must be a valid profile id")
        if self.layer in ("organization", "product"):
            if not self.rationale.strip():
                raise ValueError("rationale is required for organization/product profiles")
            if not self.evidence:
                raise ValueError("evidence is required for organization/product profiles")
        for value in (self.fabrication_tolerance, self.placement_tolerance):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("tolerances must be finite and non-negative")
        for value in (
            self.min_pad_clearance_mm,
            self.min_ep_to_pad_clearance_mm,
            self.min_mask_web_mm,
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("clearances must be finite and positive")
        return self


class RuleProfileDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    layer: Layer
    sha256: str = Field(pattern=_SHA256.pattern)


class EffectiveRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chain: list[str]
    chain_sha256: str = Field(pattern=_SHA256.pattern)
    profile_chain: list[RuleProfileDigest]
    layer: Layer
    density: Density
    fabrication_tolerance: float
    placement_tolerance: float
    goal_overrides: dict[Family, GoalOverride]
    min_pad_clearance_mm: float
    min_ep_to_pad_clearance_mm: float
    min_mask_web_mm: float
    paste: PasteRule

    @model_validator(mode="after")
    def validate_values(self) -> EffectiveRules:
        for value in (self.fabrication_tolerance, self.placement_tolerance):
            if not math.isfinite(value) or value < 0:
                raise ValueError("tolerances must be finite and non-negative")
        for value in (
            self.min_pad_clearance_mm,
            self.min_ep_to_pad_clearance_mm,
            self.min_mask_web_mm,
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("clearances must be finite and positive")
        return self


_BUILTIN_IDS = frozenset({"builtin:ipc7351b", "builtin:kicad-generator"})


def profile_sha256(profile: RuleProfile) -> str:
    canonical = json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# KFG eaee2837c34188adbf652ce7e5b2374541108cd1 defaults: F=0.1 mm,
# P=0.05 mm; minimum EP-to-pad clearance=0.2 mm.
_BUILTINS = {
    "builtin:ipc7351b": RuleProfile(
        artifact_kind="circuit_rule_profile",
        profile_id="builtin:ipc7351b",
        layer="standard",
        parent=None,
        standard="ipc7351b",
        density="nominal",
        fabrication_tolerance=0.05,
        placement_tolerance=0.025,
        min_pad_clearance_mm=0.15,
        min_ep_to_pad_clearance_mm=0.2,
        min_mask_web_mm=0.1,
        paste=PasteRule(
            coverage_min=0.5,
            coverage_max=1.0,
            ep_coverage_min=0.5,
            ep_coverage_max=0.8,
        ),
    ),
    "builtin:kicad-generator": RuleProfile(
        artifact_kind="circuit_rule_profile",
        profile_id="builtin:kicad-generator",
        layer="standard",
        parent=None,
        standard="kicad_generator",
        density="nominal",
        fabrication_tolerance=0.1,
        placement_tolerance=0.05,
        min_pad_clearance_mm=0.15,
        min_ep_to_pad_clearance_mm=0.2,
        min_mask_web_mm=0.1,
        paste=PasteRule(
            coverage_min=0.5,
            coverage_max=1.0,
            ep_coverage_min=0.5,
            ep_coverage_max=0.8,
        ),
    ),
}


def _error_code(error: ValidationError) -> str:
    message = " ".join(item["msg"] for item in error.errors())
    if "rationale" in message:
        return "rationale"
    if "evidence" in message:
        return "evidence"
    return "invalid"


def _read_profile(profile_id: str, rules_dir: Path) -> RuleProfile:
    path = rules_dir / f"{profile_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuleProfileError("missing_parent", f"missing rule profile: {profile_id}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise RuleProfileError(
            "invalid", f"cannot read rule profile {profile_id}: {error}"
        ) from error
    try:
        profile = RuleProfile.model_validate(raw)
    except ValidationError as error:
        raise RuleProfileError(
            _error_code(error),
            f"invalid rule profile {profile_id}: {error}",
        ) from error
    if profile.profile_id != profile_id:
        raise RuleProfileError(
            "invalid",
            f"profile id {profile.profile_id!r} does not match file {profile_id!r}",
        )
    return profile


def _verify_evidence(profile: RuleProfile, rules_dir: Path) -> None:
    project_root = rules_dir.resolve().parent.parent
    for evidence in profile.evidence:
        relative = Path(evidence.path)
        if relative.is_absolute():
            raise RuleProfileError(
                "evidence", f"evidence path must be project-relative: {evidence.path}"
            )
        path = (project_root / relative).resolve()
        try:
            path.relative_to(project_root)
        except ValueError as error:
            raise RuleProfileError(
                "evidence", f"evidence path escapes project: {evidence.path}"
            ) from error
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise RuleProfileError(
                "evidence", f"cannot read evidence file {evidence.path}"
            ) from error
        if digest != evidence.sha256:
            raise RuleProfileError("evidence", f"evidence hash mismatch: {evidence.path}")


def load_rules(profile_id: str, rules_dir: Path) -> EffectiveRules:
    """Resolve a profile to its built-in root and verify each profile and evidence file."""

    profiles: list[RuleProfile] = []

    def resolve(current_id: str, stack: tuple[str, ...]) -> None:
        if current_id in stack:
            raise RuleProfileError(
                "rule_profile_cycle",
                f"rule profile cycle: {' -> '.join((*stack, current_id))}",
            )
        if current_id in _BUILTINS:
            profiles.append(_BUILTINS[current_id])
            return
        if current_id.startswith("builtin:"):
            raise RuleProfileError("missing_parent", f"unknown built-in profile: {current_id}")
        profile = _read_profile(current_id, rules_dir)
        _verify_evidence(profile, rules_dir)
        if profile.parent is None:
            raise RuleProfileError("missing_parent", f"profile {current_id} has no built-in parent")
        parent = _BUILTINS.get(profile.parent)
        if parent is None:
            if profile.parent.startswith("builtin:"):
                raise RuleProfileError(
                    "missing_parent",
                    f"unknown built-in parent: {profile.parent}",
                )
            resolve(profile.parent, (*stack, current_id))
            parent = profiles[-1]
        else:
            if parent.profile_id in stack or parent.profile_id == current_id:
                raise RuleProfileError(
                    "rule_profile_cycle",
                    f"rule profile cycle: {' -> '.join((*stack, current_id, parent.profile_id))}",
                )
            profiles.append(parent)
        actual_parent_sha256 = profile_sha256(parent)
        if profile.parent_sha256 != actual_parent_sha256:
            raise RuleProfileError(
                "parent_hash",
                f"parent hash mismatch for {current_id}: expected {actual_parent_sha256}",
            )
        if _LAYER_ORDER[profile.layer] < _LAYER_ORDER[parent.layer]:
            raise RuleProfileError(
                "layer_order",
                (
                    f"profile {current_id} decreases layer order from {parent.layer} "
                    f"to {profile.layer}"
                ),
            )
        profiles.append(profile)

    resolve(profile_id, ())

    resolved: dict[str, object] = {
        "density": "nominal",
        "fabrication_tolerance": 0.05,
        "placement_tolerance": 0.025,
        "min_pad_clearance_mm": 0.15,
        "min_ep_to_pad_clearance_mm": 0.2,
        "min_mask_web_mm": 0.1,
        "paste": _BUILTINS["builtin:ipc7351b"].paste,
    }
    goal_overrides: dict[Family, GoalOverride] = {}
    for profile in profiles:
        for name in (
            "density",
            "fabrication_tolerance",
            "placement_tolerance",
            "min_pad_clearance_mm",
            "min_ep_to_pad_clearance_mm",
            "min_mask_web_mm",
            "paste",
        ):
            value = getattr(profile, name)
            if value is not None:
                resolved[name] = value
        goal_overrides.update(profile.goal_overrides)

    for name in ("fabrication_tolerance", "placement_tolerance"):
        value = resolved[name]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise RuleProfileError("invalid", f"{name} must be finite and non-negative")
    for name in (
        "min_pad_clearance_mm",
        "min_ep_to_pad_clearance_mm",
        "min_mask_web_mm",
    ):
        value = resolved[name]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise RuleProfileError("invalid", f"{name} must be finite and positive")

    chain_hashes = [profile_sha256(profile) for profile in profiles]
    chain_sha256 = hashlib.sha256("\n".join(chain_hashes).encode("utf-8")).hexdigest()
    return EffectiveRules.model_validate(
        {
            "chain": [profile.profile_id for profile in profiles],
            "chain_sha256": chain_sha256,
            "profile_chain": [
                RuleProfileDigest(
                    profile_id=profile.profile_id,
                    layer=profile.layer,
                    sha256=profile_sha256(profile),
                ).model_dump(mode="python")
                for profile in profiles
            ],
            "layer": profiles[-1].layer,
            "density": resolved["density"],
            "fabrication_tolerance": resolved["fabrication_tolerance"],
            "placement_tolerance": resolved["placement_tolerance"],
            "goal_overrides": goal_overrides,
            "min_pad_clearance_mm": resolved["min_pad_clearance_mm"],
            "min_ep_to_pad_clearance_mm": resolved["min_ep_to_pad_clearance_mm"],
            "min_mask_web_mm": resolved["min_mask_web_mm"],
            "paste": resolved["paste"],
        }
    )
