"""Conversation provenance checks for design briefs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .brief import DesignBrief, brief_sha256


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^R[0-9]+$")
    text: str = Field(min_length=1)
    source: Literal["user", "agent"]
    speaker: str = Field(min_length=1)


class Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^A[0-9]+$")
    text: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class OpenQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^Q[0-9]+$")
    text: str = Field(min_length=1)


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirements: list[Requirement] = Field(min_length=1)
    assumptions: list[Assumption] = Field(default_factory=lambda: list[Assumption]())
    open_questions: list[OpenQuestion] = Field(default_factory=lambda: list[OpenQuestion]())
    part_sources: dict[str, list[str]]
    net_sources: dict[str, list[str]]

    @model_validator(mode="after")
    def validate_ids_and_sources(self) -> Intake:
        records = [*self.requirements, *self.assumptions, *self.open_questions]
        ids = [record.id for record in records]
        if len(set(ids)) != len(ids):
            raise ValueError("intake ids must be unique")
        for mapping_name, mapping in (
            ("part_sources", self.part_sources),
            ("net_sources", self.net_sources),
        ):
            for key, sources in mapping.items():
                if not sources:
                    raise ValueError(f"{mapping_name}[{key}] must not be empty")
        return self


class IntakeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_path: Path
    intake_path: Path
    brief_sha256: str
    intake_sha256: str
    sha_matches: bool
    unmapped_parts: list[str]
    unmapped_nets: list[str]
    unknown_parts: list[str]
    unknown_nets: list[str]
    unknown_sources: dict[str, list[str]]
    assumption_only_parts: list[str]
    assumption_only_nets: list[str]
    open_questions: list[OpenQuestion]
    verdict: Literal["ready", "blocked"]
    reasons: list[str]


def load_intake(path: Path) -> Intake:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not load intake {path}: {exc}") from exc
    return Intake.model_validate(value)


def check_intake(
    brief: DesignBrief,
    intake: Intake,
    *,
    brief_path: Path,
    intake_path: Path,
) -> IntakeReport:
    actual_brief_sha256 = brief_sha256(brief_path)
    actual_intake_sha256 = brief_sha256(intake_path)
    sha_matches = intake.brief_sha256 == actual_brief_sha256
    part_names = {part.reference for part in brief.parts}
    net_names = {net.name for net in brief.nets}
    unknown_parts = sorted(set(intake.part_sources) - part_names)
    unknown_nets = sorted(set(intake.net_sources) - net_names)
    unmapped_parts = sorted(part_names - set(intake.part_sources))
    unmapped_nets = sorted(net_names - set(intake.net_sources))
    defined_ids = {
        record.id for record in [*intake.requirements, *intake.assumptions, *intake.open_questions]
    }
    unknown_sources: dict[str, list[str]] = {}
    for key, sources in [*intake.part_sources.items(), *intake.net_sources.items()]:
        unknown = sorted(set(sources) - defined_ids)
        if unknown:
            unknown_sources[key] = sorted(set(unknown_sources.get(key, [])) | set(unknown))
    assumptions = {assumption.id for assumption in intake.assumptions}
    assumption_only_parts = sorted(
        key
        for key, sources in intake.part_sources.items()
        if key in part_names and sources and set(sources) <= assumptions
    )
    assumption_only_nets = sorted(
        key
        for key, sources in intake.net_sources.items()
        if key in net_names and sources and set(sources) <= assumptions
    )
    reasons: list[str] = []
    if not sha_matches:
        reasons.append("brief sha256 mismatch")
    if unmapped_parts:
        reasons.append(f"unmapped parts: {', '.join(unmapped_parts)}")
    if unmapped_nets:
        reasons.append(f"unmapped nets: {', '.join(unmapped_nets)}")
    if unknown_parts:
        reasons.append(f"unknown parts: {', '.join(unknown_parts)}")
    if unknown_nets:
        reasons.append(f"unknown nets: {', '.join(unknown_nets)}")
    if unknown_sources:
        reasons.append(f"unknown source ids: {', '.join(sorted(unknown_sources))}")
    if intake.open_questions:
        question_ids = ", ".join(question.id for question in intake.open_questions)
        reasons.append(f"open questions: {question_ids}")
    blocked = bool(reasons)
    return IntakeReport(
        brief_path=brief_path,
        intake_path=intake_path,
        brief_sha256=actual_brief_sha256,
        intake_sha256=actual_intake_sha256,
        sha_matches=sha_matches,
        unmapped_parts=unmapped_parts,
        unmapped_nets=unmapped_nets,
        unknown_parts=unknown_parts,
        unknown_nets=unknown_nets,
        unknown_sources=unknown_sources,
        assumption_only_parts=assumption_only_parts,
        assumption_only_nets=assumption_only_nets,
        open_questions=intake.open_questions,
        verdict="blocked" if blocked else "ready",
        reasons=reasons,
    )
