"""Independent, deterministic pin-source parsing and comparison."""

from __future__ import annotations

import csv
import hashlib
import re
import shlex
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .partspec import PartSpec
from .pinout import names_equal

PinSourceKind = Literal[
    "part_spec",
    "ibis",
    "bsdl",
    "stm32_open_pin_data",
    "amd_package_file",
    "microchip_atdf",
]


class PinSourceError(ValueError):
    """Raised when a machine-readable pin source cannot be parsed."""


class PinSourcePin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str = Field(min_length=1)
    name: str = Field(min_length=1)
    bank: str | None = None


class PinSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PinSourceKind
    description: str
    path: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage: str = Field(min_length=1)
    identity: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    pins: list[PinSourcePin]

    @model_validator(mode="after")
    def validate_unique_numbers(self) -> PinSource:
        numbers = [pin.number for pin in self.pins]
        if len(numbers) != len(set(numbers)):
            raise ValueError("pin source contains duplicate pin numbers")
        if not numbers:
            raise ValueError("pin source contains no pins")
        return self


def _empty_pin_sources() -> list[PinSource]:
    return []


class PinSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    kind: PinSourceKind | None = None
    pinout_name: str | None = None
    derived_from: list[str] = Field(default_factory=list)

    @field_validator("derived_from")
    @classmethod
    def validate_derived_from(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("derived_from lineage names must be non-empty")
        return values


class PinSourceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "pin_source_missing",
        "pin_source_name_mismatch",
        "pin_source_identity_mismatch",
    ]
    number: str
    message: str


class PinSourceComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_pin_source_comparison"] = "circuit_pin_source_comparison"
    class_a: PinSource
    class_b: PinSource
    sources: list[PinSource] = Field(default_factory=_empty_pin_sources)
    independent_lineages: list[str] = Field(default_factory=list)
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
        lineage="part_spec",
        description="PartSpec cell-bound pin table",
        path=str(spec_path.resolve()) if spec_path is not None else None,
        sha256=spec_sha256,
        identity=_spec_identity(spec),
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
    kind: PinSourceKind,
    path: Path,
    digest: str,
    pins: list[PinSourcePin],
    identity: list[str],
    derived_from: Sequence[str] = (),
) -> PinSource:
    return PinSource(
        kind=kind,
        lineage=kind,
        description=f"{kind.replace('_', ' ').upper()} pin map",
        path=str(path.resolve()),
        sha256=digest,
        identity=_unique_text(identity),
        derived_from=list(derived_from),
        pins=pins,
    )


def parse_ibis(path: Path, *, derived_from: Sequence[str] = ()) -> PinSource:
    text, digest = _source_bytes(path)
    return _make_source(
        kind="ibis",
        path=path,
        digest=digest,
        pins=_parse_ibis_pins(text),
        identity=_ibis_identity(text),
        derived_from=derived_from,
    )


def parse_bsdl(path: Path, *, derived_from: Sequence[str] = ()) -> PinSource:
    text, digest = _source_bytes(path)
    return _make_source(
        kind="bsdl",
        path=path,
        digest=digest,
        pins=_parse_bsdl_pins(text),
        identity=_bsdl_identity(text),
        derived_from=derived_from,
    )


def parse_pin_source(
    path: Path,
    *,
    kind: PinSourceKind | None = None,
    pinout_name: str | None = None,
    derived_from: Sequence[str] = (),
) -> PinSource:
    suffix = path.suffix.casefold()
    selected_kind = kind
    if selected_kind is None:
        if suffix in {".ibs", ".ibis"}:
            selected_kind = "ibis"
        elif suffix == ".bsdl":
            selected_kind = "bsdl"
        elif suffix in {".atdf"}:
            selected_kind = "microchip_atdf"
        elif suffix in {".csv", ".txt", ".pins", ".pkg"}:
            selected_kind = "amd_package_file"
        elif suffix == ".xml":
            text, _ = _source_bytes(path)
            root = _parse_xml(text, path)
            tags = {_local_name(item.tag) for item in root.iter()}
            selected_kind = (
                "stm32_open_pin_data"
                if "mcu" in tags
                else "microchip_atdf"
                if "pinout" in tags
                else None
            )
    if selected_kind is None or selected_kind == "part_spec":
        raise PinSourceError(f"unsupported pin source extension: {path.suffix or '<none>'}")
    if selected_kind == "ibis":
        return parse_ibis(path, derived_from=derived_from)
    if selected_kind == "bsdl":
        return parse_bsdl(path, derived_from=derived_from)
    if selected_kind == "stm32_open_pin_data":
        return parse_stm32_open_pin_data(path, derived_from=derived_from)
    if selected_kind == "amd_package_file":
        return parse_amd_package_file(path, derived_from=derived_from)
    return parse_microchip_atdf(
        path,
        pinout_name=pinout_name,
        derived_from=derived_from,
    )


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


def _unique_text(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _spec_identity(spec: PartSpec) -> list[str]:
    return _unique_text([spec.mpn, spec.package.drawing_id])


def _ibis_identity(text: str) -> list[str]:
    section: str | None = None
    identities: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("|", maxsplit=1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
            continue
        if section == "component" and line:
            try:
                fields = shlex.split(line, comments=False, posix=True)
            except ValueError:
                continue
            if fields:
                identities.append(fields[0])
            section = None
    return _unique_text(identities)


def _bsdl_identity(text: str) -> list[str]:
    entity = re.search(r"\bentity\s+([A-Za-z0-9_.-]+)\s+is\b", text, re.IGNORECASE)
    packages = re.findall(
        r"\bpackage\s+([A-Za-z0-9_.-]+)\s+is\b",
        text,
        re.IGNORECASE,
    )
    return _unique_text(([entity.group(1)] if entity is not None else []) + packages)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].casefold()


def _xml_attribute(element: ET.Element, name: str) -> str | None:
    target = name.casefold()
    return next(
        (
            value.strip()
            for key, value in element.attrib.items()
            if _local_name(key) == target and value.strip()
        ),
        None,
    )


def _parse_xml(text: str, path: Path) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise PinSourceError(f"invalid pin-source XML {path}: {exc}") from exc


def parse_stm32_open_pin_data(
    path: Path,
    *,
    derived_from: Sequence[str] = (),
) -> PinSource:
    text, digest = _source_bytes(path)
    root = _parse_xml(text, path)
    mcus = [item for item in root.iter() if _local_name(item.tag) == "mcu"]
    if len(mcus) != 1:
        raise PinSourceError(f"STM32 pin data must contain exactly one MCU element: {path}")
    mcu = mcus[0]
    pins: list[PinSourcePin] = []
    for element in mcu:
        if _local_name(element.tag) != "pin":
            continue
        position = _xml_attribute(element, "position")
        name = _xml_attribute(element, "name")
        if position is None or name is None:
            raise PinSourceError("STM32 pin is missing Position or Name")
        pins.append(
            PinSourcePin(
                number=position,
                name=name,
                bank=_xml_attribute(element, "bank"),
            )
        )
    identities = [
        value
        for key in ("RefName", "Package", "Name")
        if (value := _xml_attribute(mcu, key)) is not None
    ]
    return _make_source(
        kind="stm32_open_pin_data",
        path=path,
        digest=digest,
        pins=pins,
        identity=identities,
        derived_from=derived_from,
    )


def _amd_pin_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _is_amd_pin_number(value: str) -> bool:
    return re.fullmatch(r"(?:[A-Z]{1,3}\d{1,4}[A-Z]?|\d{1,6})", value, re.IGNORECASE) is not None


def _amd_identity(text: str) -> list[str]:
    identities: list[str] = []
    pattern = re.compile(
        r"^\s*(?:device|part|package|component)\s*[:=]\s*([A-Za-z0-9_.+-]+)",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if match is not None:
            identities.append(match.group(1))
    return _unique_text(identities)


def parse_amd_package_file(
    path: Path,
    *,
    derived_from: Sequence[str] = (),
) -> PinSource:
    text, digest = _source_bytes(path)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = next((item for item in ("\t", "|", ";") if item in sample), ",")

    header_index: int | None = None
    header: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        try:
            fields = next(csv.reader([line], delimiter=delimiter, skipinitialspace=True))
        except csv.Error:
            continue
        normalized = [_amd_pin_key(field) for field in fields]
        if "pin" in normalized and "pinname" in normalized:
            header_index = index
            header = normalized
            break
    if header_index is None:
        raise PinSourceError("AMD package file has no Pin and Pin Name columns")

    pin_index = header.index("pin")
    name_index = header.index("pinname")
    bank_index = header.index("bank") if "bank" in header else None
    pins: list[PinSourcePin] = []
    for line_number, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
        if not line.strip():
            continue
        try:
            fields = next(csv.reader([line], delimiter=delimiter, skipinitialspace=True))
        except csv.Error as exc:
            raise PinSourceError(f"invalid AMD package row at line {line_number}") from exc
        number = fields[pin_index].strip() if pin_index < len(fields) else ""
        if not _is_amd_pin_number(number):
            continue
        name = fields[name_index].strip() if name_index < len(fields) else ""
        if not name:
            raise PinSourceError(f"AMD package row has no Pin Name at line {line_number}")
        bank = (
            fields[bank_index].strip() or None
            if bank_index is not None and bank_index < len(fields)
            else None
        )
        pins.append(PinSourcePin(number=number, name=name, bank=bank))
    return _make_source(
        kind="amd_package_file",
        path=path,
        digest=digest,
        pins=pins,
        identity=_amd_identity(text),
        derived_from=derived_from,
    )


def parse_microchip_atdf(
    path: Path,
    *,
    pinout_name: str | None,
    derived_from: Sequence[str] = (),
) -> PinSource:
    if pinout_name is None or not pinout_name.strip():
        raise PinSourceError("Microchip ATDF parsing requires a pinout name")
    text, digest = _source_bytes(path)
    root = _parse_xml(text, path)
    pinouts = [
        item
        for item in root.iter()
        if _local_name(item.tag) == "pinout"
        and (_xml_attribute(item, "name") or "").casefold() == pinout_name.strip().casefold()
    ]
    if len(pinouts) != 1:
        raise PinSourceError(
            f"ATDF pinout {pinout_name!r} must resolve to exactly one pinout: {path}"
        )
    pinout = pinouts[0]
    pins: list[PinSourcePin] = []
    for element in pinout:
        if _local_name(element.tag) != "pin":
            continue
        position = _xml_attribute(element, "position")
        pad = _xml_attribute(element, "pad")
        if position is None or pad is None:
            raise PinSourceError("ATDF pin is missing position or pad")
        pins.append(
            PinSourcePin(
                number=position,
                name=pad,
                bank=_xml_attribute(element, "bank"),
            )
        )
    parents = {child: parent for parent in root.iter() for child in parent}
    identity_elements = {pinout}
    parent = parents.get(pinout)
    while parent is not None:
        if _local_name(parent.tag) in {"device", "variant", "component", "pinout"}:
            identity_elements.add(parent)
        parent = parents.get(parent)
    identities: list[str] = []
    for element in root.iter():
        if element not in identity_elements:
            continue
        for key in ("name", "refname", "package", "device", "component", "entity", "id"):
            value = _xml_attribute(element, key)
            if value is not None:
                identities.append(value)
    identities.append(pinout_name)
    return _make_source(
        kind="microchip_atdf",
        path=path,
        digest=digest,
        pins=pins,
        identity=identities,
        derived_from=derived_from,
    )


def _number_key(number: str) -> tuple[int, int | str, str]:
    if number.isdigit():
        return 0, int(number), number
    return 1, number.casefold(), number


def _identity_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _identity_compatible(left: str, right: str) -> bool:
    left_key = _identity_key(left)
    right_key = _identity_key(right)
    if not left_key or not right_key:
        return False
    shorter, longer = sorted((left_key, right_key), key=len)
    return shorter == longer or (len(shorter) >= 6 and longer.startswith(shorter))


def compare_pin_sources(
    a: PinSource,
    b: PinSource | Sequence[PinSource],
) -> PinSourceComparison:
    sources = [b] if isinstance(b, PinSource) else list(b)
    if not sources:
        raise PinSourceError("at least one independent pin source is required")
    pins_a = {pin.number: pin.name for pin in a.pins}
    findings: list[PinSourceFinding] = []

    independent_lineages: list[str] = []
    for source in [a, *sources]:
        if source.derived_from or source.lineage in independent_lineages:
            continue
        independent_lineages.append(source.lineage)

    for source in sources:
        if not any(
            _identity_compatible(source_identity, spec_identity)
            for source_identity in source.identity
            for spec_identity in a.identity
        ):
            findings.append(
                PinSourceFinding(
                    code="pin_source_identity_mismatch",
                    number="",
                    message=(
                        f"pin source identity {source.identity!r} does not match "
                        f"PartSpec identity {a.identity!r}"
                    ),
                )
            )
        pins_b = {pin.number: pin.name for pin in source.pins}
        for number in sorted(pins_a.keys() | pins_b.keys(), key=_number_key):
            name_a = pins_a.get(number)
            name_b = pins_b.get(number)
            if name_a is None or name_b is None:
                present = "PartSpec" if name_a is not None else source.description
                absent = source.description if name_a is not None else "PartSpec"
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
                        message=(
                            f"pin {number} name differs: PartSpec {name_a!r}, "
                            f"{source.description} {name_b!r}"
                        ),
                    )
                )
    return PinSourceComparison(
        class_a=a,
        class_b=sources[0],
        sources=sources,
        independent_lineages=independent_lineages,
        findings=findings,
    )
