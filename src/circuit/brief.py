"""Machine-readable design brief models and helpers."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_mm: float
    y_mm: float
    rotation_deg: float = 0.0


SIGNAL_CLASSES = ("power", "ground", "signal", "analog", "data", "highspeed", "shield")


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(pattern=r"^[A-Z][A-Z0-9]*[0-9]+$")
    lib_id: str
    footprint: str
    value: str | None = None
    connector: bool = False
    mcu: bool = False
    housing: str | None = None
    rated_current_a: float | None = Field(default=None, gt=0)
    rated_voltage_v: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_library_ids(self) -> Part:
        if self.lib_id.count(":") != 1 or any(not part for part in self.lib_id.split(":")):
            raise ValueError("lib_id must contain exactly one colon")
        if self.footprint.count(":") != 1 or any(not part for part in self.footprint.split(":")):
            raise ValueError("footprint must contain exactly one colon")
        return self


class Net(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_+\-./]+$")
    pins: list[str] = Field(min_length=2)
    signal_class: (
        Literal["power", "ground", "signal", "analog", "data", "highspeed", "shield"] | None
    ) = None
    voltage_v: float = Field(default=0.0, ge=0)
    current_a: float = Field(default=0.0, ge=0)


class Board(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    placements: dict[str, Placement] = Field(default_factory=dict)


class DrawingInfo(BaseModel):
    """ISO 7200 title-block data a brief cannot derive: who owns,
    prepared and approved the drawing, and when it was released.

    The document status is derived from the release fields, never declared.
    """

    model_config = ConfigDict(extra="forbid")

    legal_owner: str | None = Field(default=None, min_length=1, max_length=40)
    identification_prefix: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$"
    )
    revision: str = Field(default="A", pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,7}$")
    responsible_dept: str | None = Field(default=None, min_length=1, max_length=20)
    technical_reference: str | None = Field(default=None, min_length=1, max_length=30)
    created_by: str | None = Field(default=None, min_length=1, max_length=30)
    approved_by: str | None = Field(default=None, min_length=1, max_length=30)
    date_of_issue: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    supplementary_title: str | None = Field(default=None, min_length=1, max_length=60)
    classification: str | None = Field(default=None, min_length=1, max_length=25)
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}$")

    @model_validator(mode="after")
    def validate_release(self) -> DrawingInfo:
        if self.date_of_issue is not None:
            try:
                datetime.date.fromisoformat(self.date_of_issue)
            except ValueError as exc:
                raise ValueError(f"date_of_issue is not a calendar date: {exc}") from exc
            if self.approved_by is None:
                raise ValueError("date_of_issue requires approved_by: only approved drawings issue")
        return self

    @property
    def status(self) -> str:
        """ISO 7200 document status derived from the release fields."""
        if self.approved_by is not None and self.date_of_issue is not None:
            return "Released"
        if self.approved_by is not None:
            return "In approval"
        return "In preparation"

    def identification(self, design: str) -> str:
        """Drawing number: the prefix when set, else the design name."""
        return self.identification_prefix or design


class ThermalPart(BaseModel):
    """Datasheet-sourced dissipation and junction limit of one part."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(pattern=r"^[A-Z][A-Z0-9]*[0-9]+$")
    power_w: float = Field(ge=0)
    tj_max_c: float
    derating_margin_c: float = Field(default=0, ge=0)
    theta_ja_c_per_w: float | None = Field(default=None, ge=0)
    theta_jc: float | None = Field(default=None, ge=0)
    theta_cs: float | None = Field(default=None, ge=0)
    theta_sa: float | None = Field(default=None, ge=0)
    source: str = Field(min_length=1)


class ThermalSpec(BaseModel):
    """Thermal facts handed to simulation-agent; simulation owns the verdict."""

    model_config = ConfigDict(extra="forbid")

    ambient_c: float
    parts: list[ThermalPart] = Field(min_length=1)
    response_path: str | None = Field(default=None, min_length=1)


class LifetimeStress(BaseModel):
    """One mission-profile step: hot-spot temperature and time fraction."""

    model_config = ConfigDict(extra="forbid")

    temperature_c: float = Field(gt=-273.15)
    fraction: float = Field(gt=0, le=1)


class LifetimePart(BaseModel):
    """Datasheet-sourced wear-out facts of one part (e.g. an electrolytic)."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(pattern=r"^[A-Z][A-Z0-9]*[0-9]+$")
    rated_life_h: float = Field(gt=0)
    rated_temp_c: float = Field(gt=-273.15)
    activation_energy_ev: float = Field(gt=0)
    profile: list[LifetimeStress] = Field(min_length=1)
    required_life_h: float = Field(gt=0)
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profile(self) -> LifetimePart:
        if abs(sum(step.fraction for step in self.profile) - 1) > 1e-9:
            raise ValueError("lifetime profile fractions must sum to 1")
        return self


class LifetimeSpec(BaseModel):
    """Arrhenius lifetime facts handed to simulation-agent; simulation owns the verdict."""

    model_config = ConfigDict(extra="forbid")

    parts: list[LifetimePart] = Field(min_length=1)
    response_path: str | None = Field(default=None, min_length=1)


class DesignBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin_pattern: ClassVar[re.Pattern[str]] = re.compile(r"^([A-Z][A-Z0-9]*[0-9]+)\.([^.\s]+)$")

    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    description: str = ""
    parts: list[Part] = Field(min_length=1)
    nets: list[Net] = Field(min_length=1)
    board: Board
    drawing: DrawingInfo = Field(default_factory=DrawingInfo)
    thermal: ThermalSpec | None = None
    lifetime: LifetimeSpec | None = None

    @model_validator(mode="after")
    def validate_references_and_connections(self) -> DesignBrief:
        references = [part.reference for part in self.parts]
        if len(set(references)) != len(references):
            raise ValueError("part references must be unique")
        known_references = set(references)
        net_names = [net.name for net in self.nets]
        if len(set(net_names)) != len(net_names):
            raise ValueError("net names must be unique")
        seen_pins: set[str] = set()
        for net in self.nets:
            for token in net.pins:
                match = self.pin_pattern.fullmatch(token)
                if match is None:
                    raise ValueError(f"invalid pin token: {token}")
                if match.group(1) not in known_references:
                    raise ValueError(f"pin references unknown part: {token}")
                if token in seen_pins:
                    raise ValueError(f"pin appears in multiple nets: {token}")
                seen_pins.add(token)
        for reference, placement in self.board.placements.items():
            if reference not in known_references:
                raise ValueError(f"placement references unknown part: {reference}")
            if not 0 <= placement.x_mm <= self.board.width_mm:
                raise ValueError(f"placement x is outside board: {reference}")
            if not 0 <= placement.y_mm <= self.board.height_mm:
                raise ValueError(f"placement y is outside board: {reference}")
        if self.thermal is not None:
            thermal_refs = [part.reference for part in self.thermal.parts]
            if len(set(thermal_refs)) != len(thermal_refs):
                raise ValueError("thermal part references must be unique")
            for reference in thermal_refs:
                if reference not in known_references:
                    raise ValueError(f"thermal references unknown part: {reference}")
        if self.lifetime is not None:
            lifetime_refs = [part.reference for part in self.lifetime.parts]
            if len(set(lifetime_refs)) != len(lifetime_refs):
                raise ValueError("lifetime part references must be unique")
            for reference in lifetime_refs:
                if reference not in known_references:
                    raise ValueError(f"lifetime references unknown part: {reference}")
        return self


def load_brief(path: Path) -> DesignBrief:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not load design brief {path}: {exc}") from exc
    return DesignBrief.model_validate(value)


def brief_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"could not hash design brief {path}: {exc}") from exc
    return digest.hexdigest()


def expected_nets(brief: DesignBrief) -> dict[str, frozenset[tuple[str, str]]]:
    result: dict[str, frozenset[tuple[str, str]]] = {}
    for net in brief.nets:
        result[net.name] = frozenset(
            (reference, pin) for reference, pin in (item.split(".", 1) for item in net.pins)
        )
    return result
