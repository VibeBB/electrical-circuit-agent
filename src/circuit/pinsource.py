"""Independent, deterministic pin-source parsing and comparison."""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .partspec import PartSpec
from .pinout import names_equal

PinSourceKind = Literal["part_spec", "ibis", "bsdl"]


class PinSourceError(ValueError):
    """Raised when a machine-readable pin source cannot be parsed."""


class PinSourcePin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str = Field(min_length=1)
    name: str = Field(min_length=1)


class PinSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PinSourceKind
    description: str
    path: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pins: list[PinSourcePin]

    @model_validator(mode="after")
    def validate_unique_numbers(self) -> PinSource:
        numbers = [pin.number for pin in self.pins]
        if len(numbers) != len(set(numbers)):
            raise ValueError("pin source contains duplicate pin numbers")
        if not numbers:
            raise ValueError("pin source contains no pins")
        return self


class PinSourceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["pin_source_missing", "pin_source_name_mismatch"]
    number: str
    message: str


class PinSourceComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_pin_source_comparison"] = "circuit_pin_source_comparison"
    class_a: PinSource
    class_b: PinSource
    findings: list[PinSourceFinding]

    @property
    def passed(self) -> bool:
        return not self.findings


def source_from_part_spec(
    spec: PartSpec,
    *,
    spec_sha256: str,
    spec_path: Path | None = None,
) -> PinSource:
    return PinSource(
        kind="part_spec",
        description="PartSpec cell-bound pin table",
        path=str(spec_path.resolve()) if spec_path is not None else None,
        sha256=spec_sha256,
        pins=[PinSourcePin(number=pin.number, name=pin.name) for pin in spec.pins],
    )


def _source_bytes(path: Path) -> tuple[str, str]:
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise PinSourceError(f"cannot read pin source {path}: {exc}") from exc
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PinSourceError(f"pin source is not UTF-8: {path}") from exc
    return text, hashlib.sha256(source).hexdigest()


def _make_source(
    *,
    kind: Literal["ibis", "bsdl"],
    path: Path,
    text: str,
    digest: str,
) -> PinSource:
    try:
        pins = _parse_ibis_pins(text) if kind == "ibis" else _parse_bsdl_pins(text)
        return PinSource(
            kind=kind,
            description=f"{kind.upper()} pin map",
            path=str(path.resolve()),
            sha256=digest,
            pins=pins,
        )
    except ValueError as exc:
        if isinstance(exc, PinSourceError):
            raise
        raise PinSourceError(f"invalid {kind.upper()} pin map: {exc}") from exc


def parse_ibis(path: Path) -> PinSource:
    text, digest = _source_bytes(path)
    return _make_source(kind="ibis", path=path, text=text, digest=digest)


def parse_bsdl(path: Path) -> PinSource:
    text, digest = _source_bytes(path)
    return _make_source(kind="bsdl", path=path, text=text, digest=digest)


def parse_pin_source(path: Path) -> PinSource:
    suffix = path.suffix.casefold()
    if suffix in {".ibs", ".ibis"}:
        return parse_ibis(path)
    if suffix == ".bsdl":
        return parse_bsdl(path)
    raise PinSourceError(f"unsupported pin source extension: {path.suffix or '<none>'}")


def _parse_ibis_pins(text: str) -> list[PinSourcePin]:
    in_pin_section = False
    pins: list[PinSourcePin] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("|", maxsplit=1)[0].strip()
        if not line:
            continue
        if re.fullmatch(r"\[\s*pin\s*\]", line, re.IGNORECASE):
            in_pin_section = True
            continue
        if in_pin_section and line.startswith("["):
            break
        if not in_pin_section:
            continue
        try:
            fields = shlex.split(line, comments=False, posix=True)
        except ValueError as exc:
            raise PinSourceError(f"invalid IBIS [Pin] row at line {line_number}") from exc
        if len(fields) < 2:
            raise PinSourceError(f"invalid IBIS [Pin] row at line {line_number}")
        pins.append(PinSourcePin(number=fields[0], name=fields[1]))
    if not in_pin_section or not pins:
        raise PinSourceError("IBIS source has no usable [Pin] section")
    return pins


def _parse_bsdl_pins(text: str) -> list[PinSourcePin]:
    match = re.search(
        r"\bPIN_MAP_STRING\s*:\s*PIN_MAP_STRING\s*:=\s*(.*?);",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise PinSourceError("BSDL source has no PIN_MAP_STRING declaration")
    fragments = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
    if not fragments:
        raise PinSourceError("BSDL PIN_MAP_STRING contains no string literal")
    mapping = "".join(fragments)
    pins: list[PinSourcePin] = []
    for entry in mapping.split(","):
        if not entry.strip():
            continue
        pair = entry.split(":", maxsplit=1)
        if len(pair) != 2:
            raise PinSourceError("invalid BSDL PIN_MAP_STRING entry")
        number, name = (item.strip() for item in pair)
        if not number or not name:
            raise PinSourceError("invalid BSDL PIN_MAP_STRING entry")
        pins.append(PinSourcePin(number=number, name=name))
    if not pins:
        raise PinSourceError("BSDL PIN_MAP_STRING contains no pin mappings")
    return pins


def _number_key(number: str) -> tuple[int, int | str, str]:
    if number.isdigit():
        return 0, int(number), number
    return 1, number.casefold(), number


def compare_pin_sources(a: PinSource, b: PinSource) -> PinSourceComparison:
    pins_a = {pin.number: pin.name for pin in a.pins}
    pins_b = {pin.number: pin.name for pin in b.pins}
    findings: list[PinSourceFinding] = []
    for number in sorted(pins_a.keys() | pins_b.keys(), key=_number_key):
        name_a = pins_a.get(number)
        name_b = pins_b.get(number)
        if name_a is None or name_b is None:
            present = "Class A" if name_a is not None else "Class B"
            absent = "Class B" if name_a is not None else "Class A"
            findings.append(
                PinSourceFinding(
                    code="pin_source_missing",
                    number=number,
                    message=f"pin {number} appears in {present} but not {absent}",
                )
            )
        elif not names_equal(name_a, name_b):
            findings.append(
                PinSourceFinding(
                    code="pin_source_name_mismatch",
                    number=number,
                    message=f"pin {number} name differs: Class A {name_a!r}, Class B {name_b!r}",
                )
            )
    return PinSourceComparison(class_a=a, class_b=b, findings=findings)
