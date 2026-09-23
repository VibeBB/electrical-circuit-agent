"""Emit the wire-agent ConnectivitySource contract (*.connectivity.json).

The wire importer is the schema authority (wire ADR-0003); this module
projects a design brief (and optionally an authoritative KiCad netlist) into
that schema. Emission is fail-closed: unknown signal classes default to
``signal`` and unrated quantities keep the wire-side defaults by omission.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .brief import DesignBrief, load_brief
from .netlist import Netlist, parse_netlist

_GROUND_NAME = re.compile(r"^(AGND|DGND|PGND|GND|GNDA|GNDD|VSS|VSSA|0V|RTN|RETURN|PE|FG)([_.].*)?$")
_POWER_NAME = re.compile(r"^(VCC|VDD|VBAT|VBUS|VIN|VAUX|PWR|\+?\d+V\d*(A|B)?)([_.].*)?$")
_PIN_SUFFIX = re.compile(r"(\d+)(.*)$")


class ConnectivityError(ValueError):
    """Raised when a ConnectivitySource payload cannot be built."""


def _signal_class(name: str, declared: str | None) -> str:
    if declared is not None:
        return declared
    upper = name.upper().lstrip("+-/")
    if _GROUND_NAME.match(upper):
        return "ground"
    if _POWER_NAME.match(upper):
        return "power"
    return "signal"


def _pin_key(pin: str) -> tuple[int, int, str]:
    match = _PIN_SUFFIX.match(pin)
    if match is None or match.group(2):
        return (1, 0, pin)
    return (0, int(match.group(1)), pin)


def _brief_cavities(brief: DesignBrief, reference: str) -> list[str]:
    pins: set[str] = set()
    for net in brief.nets:
        for token in net.pins:
            ref, _, pin = token.partition(".")
            if ref == reference:
                pins.add(pin)
    return sorted(pins, key=_pin_key)


def _netlist_cavities(netlist: Netlist, reference: str) -> list[str]:
    pins: set[str] = set()
    for nodes in netlist.nets.values():
        for ref, pin in nodes:
            if ref == reference:
                pins.add(pin)
    return sorted(pins, key=_pin_key)


def connectivity_source(brief: DesignBrief, netlist: Netlist | None = None) -> dict[str, Any]:
    connectors: list[dict[str, Any]] = []
    for part in brief.parts:
        if not part.connector:
            continue
        cavities = (
            _netlist_cavities(netlist, part.reference)
            if netlist is not None
            else _brief_cavities(brief, part.reference)
        )
        if not cavities:
            raise ConnectivityError(f"connector {part.reference} has no pins in any net")
        entry: dict[str, Any] = {
            "ref": part.reference,
            "family_hint": part.lib_id,
            "cavities": cavities,
        }
        if part.housing is not None:
            entry["housing"] = part.housing
        if part.rated_current_a is not None:
            entry["rated_current_a"] = part.rated_current_a
        if part.rated_voltage_v is not None:
            entry["rated_voltage_v"] = part.rated_voltage_v
        connectors.append(entry)
    nets = [
        {
            "ref": net.name,
            "signal_class": _signal_class(net.name, net.signal_class),
            "voltage_v": net.voltage_v,
            "current_a": net.current_a,
        }
        for net in brief.nets
    ]
    return {
        "schema_version": 1,
        "system": "circuit",
        "connectors": connectors,
        "nets": nets,
    }


def write_connectivity(brief: DesignBrief, out_path: Path, netlist: Netlist | None = None) -> Path:
    payload = connectivity_source(brief, netlist)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m circuit.connectivity",
        description="emit the wire-agent ConnectivitySource contract",
    )
    parser.add_argument("--brief", required=True)
    parser.add_argument("--netlist")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        design = load_brief(Path(args.brief))
        parsed = parse_netlist(Path(args.netlist)) if args.netlist else None
        payload = connectivity_source(design, parsed)
        write_connectivity(design, Path(args.out), parsed)
    except (ValueError, OSError) as exc:
        print(
            json.dumps(
                {"verdict": "fail", "stage": "connectivity-export", "detail": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "verdict": "pass",
                "design": design.name,
                "connectors": [item["ref"] for item in payload["connectors"]],
                "nets": [item["ref"] for item in payload["nets"]],
                "out": args.out,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
