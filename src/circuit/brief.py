"""Machine-readable design brief models and helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_mm: float
    y_mm: float
    rotation_deg: float = 0.0


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(pattern=r"^[A-Z][A-Z0-9]*[0-9]+$")
    lib_id: str
    footprint: str
    value: str | None = None

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


class Board(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    placements: dict[str, Placement] = Field(default_factory=dict)


class DesignBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin_pattern: ClassVar[re.Pattern[str]] = re.compile(r"^([A-Z][A-Z0-9]*[0-9]+)\.([^.\s]+)$")

    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    description: str = ""
    parts: list[Part] = Field(min_length=1)
    nets: list[Net] = Field(min_length=1)
    board: Board

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
