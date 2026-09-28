"""Firmware link: MCU pin connectivity out, firmware pin map check in.

``<design>.firmware.json`` (CircuitFirmwareConnectivity) lists, for every part
marked ``mcu`` in the brief, each connected pin with its net, signal class,
voltage and (when an authoritative KiCad netlist is given) the KiCad pin
function name. firmware-agent reads it for its ``fw.netlist_match`` gate.

``check_firmware_pinmap`` validates firmware-agent's ``<name>.fw-pinmap.json``
(the firmware side is the schema authority) against the same connectivity:
every firmware signal must land on the pad and net the circuit wires, and no
MCU pad the firmware reports as free may drive a net. Unknown evidence fails.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .brief import DesignBrief
from .connectivity import pin_key, signal_class
from .netlist import Netlist

POWER_CLASSES = frozenset({"power", "ground"})


class FirmwareLinkError(ValueError):
    """Raised when firmware connectivity cannot be built."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McuPin(_Strict):
    pin: str = Field(min_length=1)
    function: str | None = None
    net: str | None = None
    signal_class: str | None = None
    voltage_v: float | None = Field(default=None, ge=0)


class Mcu(_Strict):
    ref: str
    lib_id: str
    value: str | None = None
    footprint: str
    pins: list[McuPin]


class CircuitFirmwareConnectivity(_Strict):
    schema_version: Literal[1] = 1
    system: Literal["circuit"] = "circuit"
    artifact_kind: Literal["circuit_firmware_connectivity"] = "circuit_firmware_connectivity"
    design: str
    source: Literal["brief", "netlist"]
    brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    netlist_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mcus: list[Mcu] = Field(min_length=1)

    def mcu(self, ref: str) -> Mcu | None:
        return next((m for m in self.mcus if m.ref == ref), None)


class PinmapEntry(_Strict):
    signal: str
    pad: str
    pad_aliases: list[str]
    package_pin: str | None
    net: str
    function: str
    peripheral: str | None


class FreePad(_Strict):
    pad: str
    pad_aliases: list[str]
    package_pin: str | None


class FirmwarePinmap(_Strict):
    """Mirror of firmware-agent's ``FirmwarePinmap`` (``*.fw-pinmap.json``)."""

    schema_version: Literal[1]
    system: Literal["firmware"]
    artifact_kind: Literal["firmware_pinmap"]
    design: str
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mcu_ref: str
    mcu_profile: str
    mcu_part: str
    io_voltage_max_v: float = Field(gt=0)
    supply_net: str
    pins: list[PinmapEntry] = Field(min_length=1)
    free_pads: list[FreePad]


class FirmwareCheckReport(_Strict):
    schema_version: Literal[1] = 1
    system: Literal["circuit"] = "circuit"
    artifact_kind: Literal["circuit_firmware_check"] = "circuit_firmware_check"
    design: str
    firmware_design: str
    mcu_ref: str
    source: Literal["brief", "netlist"]
    brief_sha256: str
    netlist_sha256: str | None
    pinmap_sha256: str
    firmware_contract_sha256: str
    matched: list[str]
    problems: list[str]
    verdict: Literal["pass", "fail"]


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise FirmwareLinkError(f"could not hash {path}: {exc}") from exc


def pad_of(function: str | None) -> str | None:
    """KiCad pin function -> pad name (``GPIO26_ADC0`` -> ``GPIO26``)."""
    if not function:
        return None
    head = function.replace("/", "_").split("_", 1)[0].upper()
    return head or None


def firmware_connectivity(
    brief: DesignBrief,
    brief_path: Path,
    netlist: Netlist | None = None,
    netlist_path: Path | None = None,
) -> CircuitFirmwareConnectivity:
    if (netlist is None) != (netlist_path is None):
        raise FirmwareLinkError("netlist and netlist_path must be given together")
    parts = [part for part in brief.parts if part.mcu]
    if not parts:
        raise FirmwareLinkError('no part in the brief is marked "mcu": true')
    declared = {net.name: net for net in brief.nets}
    if netlist is None:
        nodes = {
            net.name: frozenset(tuple(token.split(".", 1)) for token in net.pins)
            for net in brief.nets
        }
    else:
        nodes = netlist.nets
    mcus: list[Mcu] = []
    for part in parts:
        if netlist is not None and part.reference not in netlist.components:
            raise FirmwareLinkError(f"MCU {part.reference} is not in the netlist")
        pins: list[McuPin] = []
        for name, members in nodes.items():
            net = declared.get(name)
            for ref, pin in members:
                if ref != part.reference:
                    continue
                voltage = net.voltage_v if net is not None and net.voltage_v > 0 else None
                pins.append(
                    McuPin(
                        pin=pin,
                        function=(
                            netlist.pin_functions.get(f"{ref}.{pin}")
                            if netlist is not None
                            else None
                        ),
                        net=name,
                        signal_class=signal_class(name, net.signal_class if net else None),
                        voltage_v=voltage,
                    )
                )
        if not pins:
            raise FirmwareLinkError(f"MCU {part.reference} has no pins in any net")
        pins.sort(key=lambda item: pin_key(item.pin))
        mcus.append(
            Mcu(
                ref=part.reference,
                lib_id=part.lib_id,
                value=part.value,
                footprint=part.footprint,
                pins=pins,
            )
        )
    return CircuitFirmwareConnectivity(
        design=brief.name,
        source="netlist" if netlist is not None else "brief",
        brief_sha256=sha256_file(brief_path),
        netlist_sha256=sha256_file(netlist_path) if netlist_path is not None else None,
        mcus=mcus,
    )


def write_firmware_connectivity(payload: CircuitFirmwareConnectivity, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path


def load_pinmap(path: Path) -> FirmwarePinmap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FirmwareLinkError(f"could not load firmware pin map {path}: {exc}") from exc
    return FirmwarePinmap.model_validate(value)


def _find(pins: list[McuPin], names: set[str], package_pin: str | None) -> McuPin | None:
    by_function = [p for p in pins if pad_of(p.function) in names]
    if len(by_function) == 1:
        return by_function[0]
    if package_pin is not None:
        return next((p for p in pins if p.pin == package_pin), None)
    return None


def check_firmware_pinmap(
    connectivity: CircuitFirmwareConnectivity, pinmap: FirmwarePinmap, pinmap_sha256: str
) -> FirmwareCheckReport:
    problems: list[str] = []
    matched: list[str] = []
    mcu = connectivity.mcu(pinmap.mcu_ref)
    if mcu is None:
        problems.append(
            f"circuit has no MCU {pinmap.mcu_ref} "
            f"(MCUs: {', '.join(m.ref for m in connectivity.mcus)})"
        )
    else:
        claimed: set[str] = set()
        for entry in pinmap.pins:
            found = _find(mcu.pins, {entry.pad, *entry.pad_aliases}, entry.package_pin)
            if found is None:
                problems.append(
                    f"{entry.signal}: {entry.pad} (package pin {entry.package_pin or '?'}) "
                    f"is not connected on {mcu.ref}"
                )
                continue
            claimed.add(found.pin)
            if found.net != entry.net:
                problems.append(
                    f"{entry.signal}: {mcu.ref}.{found.pin} ({entry.pad}) is on net "
                    f"{found.net}, firmware expects {entry.net}"
                )
                continue
            if found.signal_class in POWER_CLASSES:
                problems.append(f"{entry.signal}: net {entry.net} is a {found.signal_class} net")
                continue
            if found.voltage_v is not None and found.voltage_v > pinmap.io_voltage_max_v:
                problems.append(
                    f"{entry.signal}: net {entry.net} at {found.voltage_v} V exceeds "
                    f"{pinmap.mcu_part} I/O maximum {pinmap.io_voltage_max_v} V"
                )
                continue
            matched.append(f"{entry.signal}={mcu.ref}.{found.pin}/{entry.net}")
        for free in pinmap.free_pads:
            found = _find(mcu.pins, {free.pad, *free.pad_aliases}, free.package_pin)
            if found is None or found.pin in claimed or found.signal_class in POWER_CLASSES:
                continue
            problems.append(
                f"{mcu.ref}.{found.pin} ({free.pad}) drives net {found.net} but the firmware "
                "leaves the pad unassigned"
            )
        if not any(p.net == pinmap.supply_net for p in mcu.pins):
            problems.append(f"supply net {pinmap.supply_net} does not reach {mcu.ref}")
    return FirmwareCheckReport(
        design=connectivity.design,
        firmware_design=pinmap.design,
        mcu_ref=pinmap.mcu_ref,
        source=connectivity.source,
        brief_sha256=connectivity.brief_sha256,
        netlist_sha256=connectivity.netlist_sha256,
        pinmap_sha256=pinmap_sha256,
        firmware_contract_sha256=pinmap.contract_sha256,
        matched=matched,
        problems=problems,
        verdict="fail" if problems else "pass",
    )
