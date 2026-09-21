"""Minimal fail-closed parser for KiCad's s-expression netlist."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import sexpr
from .brief import DesignBrief, brief_sha256, expected_nets


class NetlistError(ValueError):
    """Raised when a KiCad netlist is malformed or unsupported."""


class Component(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    value: str
    footprint: str


class Netlist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nets: dict[str, frozenset[tuple[str, str]]]
    components: dict[str, Component]


def _section(root: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr]:
    for value in root[1:]:
        if isinstance(value, list) and value and value[0] == name:
            return value
    raise NetlistError(f"missing {name} section")


def _field(value: list[sexpr.SExpr], name: str) -> str:
    for child in value[1:]:
        if isinstance(child, list) and child and child[0] == name:
            if len(child) != 2 or not isinstance(child[1], str):
                raise NetlistError(f"malformed {name} field")
            return child[1]
    return ""


def parse_netlist(path: Path) -> Netlist:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NetlistError(f"could not read netlist {path}: {exc}") from exc
    try:
        root = sexpr.parse_text(text)
    except sexpr.SExprError as exc:
        raise NetlistError(str(exc)) from exc
    except Exception as exc:
        raise NetlistError(f"could not parse netlist {path}: {exc}") from exc
    if not root or root[0] != "export":
        raise NetlistError("missing export section")
    components_section = _section(root, "components")
    nets_section = _section(root, "nets")
    components: dict[str, Component] = {}
    for value in components_section[1:]:
        if not isinstance(value, list) or not value or value[0] != "comp":
            continue
        ref = _field(value, "ref")
        if not ref:
            raise NetlistError("component without ref")
        if ref in components:
            raise NetlistError(f"duplicate component reference: {ref}")
        components[ref] = Component(
            ref=ref,
            value=_field(value, "value"),
            footprint=_field(value, "footprint"),
        )
    nets: dict[str, frozenset[tuple[str, str]]] = {}
    for value in nets_section[1:]:
        if not isinstance(value, list) or not value or value[0] != "net":
            continue
        name = _field(value, "name")
        if name.startswith("/"):
            name = name[1:]
        if not name:
            raise NetlistError("net without name")
        if name in nets:
            raise NetlistError(f"duplicate net name: {name}")
        nodes: set[tuple[str, str]] = set()
        for child in value[1:]:
            if not isinstance(child, list) or not child or child[0] != "node":
                continue
            ref = _field(child, "ref")
            pin = _field(child, "pin")
            if not ref or not pin:
                raise NetlistError("node without ref/pin")
            nodes.add((ref, pin))
        nets[name] = frozenset(nodes)
    return Netlist(nets=nets, components=components)


class ConnectivityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_path: Path
    netlist_path: Path
    brief_sha256: str
    expected: dict[str, list[str]]
    actual: dict[str, list[str]]
    missing_nets: list[str]
    mismatched_nets: dict[str, dict[str, list[str]]]
    unexpected_nets: list[str]
    missing_parts: list[str]
    footprint_mismatches: dict[str, dict[str, str]]
    verdict: Literal["pass", "fail"]


def _nodes(value: frozenset[tuple[str, str]]) -> list[str]:
    return [f"{ref}.{pin}" for ref, pin in sorted(value)]


def check_connectivity(
    brief: DesignBrief,
    netlist: Netlist,
    *,
    brief_path: Path,
    netlist_path: Path,
) -> ConnectivityReport:
    expected = expected_nets(brief)
    expected_display = {name: _nodes(nodes) for name, nodes in sorted(expected.items())}
    actual_display = {
        name: _nodes(nodes) for name, nodes in sorted(netlist.nets.items()) if len(nodes) >= 2
    }
    missing_nets = sorted(set(expected) - set(netlist.nets))
    mismatched_nets: dict[str, dict[str, list[str]]] = {}
    for name, nodes in expected.items():
        actual = netlist.nets.get(name)
        if actual is not None and actual != nodes:
            mismatched_nets[name] = {"expected": _nodes(nodes), "actual": _nodes(actual)}
    unexpected_nets = sorted(set(actual_display) - set(expected))
    missing_parts = sorted(
        part.reference for part in brief.parts if part.reference not in netlist.components
    )
    expected_parts = {part.reference: part.footprint for part in brief.parts}
    footprint_mismatches: dict[str, dict[str, str]] = {}
    for reference, expected_footprint in expected_parts.items():
        component = netlist.components.get(reference)
        if component is not None and component.footprint != expected_footprint:
            footprint_mismatches[reference] = {
                "expected": expected_footprint,
                "actual": component.footprint,
            }
    passed = not (
        missing_nets or mismatched_nets or unexpected_nets or missing_parts or footprint_mismatches
    )
    return ConnectivityReport(
        brief_path=brief_path,
        netlist_path=netlist_path,
        brief_sha256=brief_sha256(brief_path),
        expected=expected_display,
        actual=actual_display,
        missing_nets=missing_nets,
        mismatched_nets=mismatched_nets,
        unexpected_nets=unexpected_nets,
        missing_parts=missing_parts,
        footprint_mismatches=footprint_mismatches,
        verdict="pass" if passed else "fail",
    )
