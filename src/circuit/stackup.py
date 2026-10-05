"""Deterministic stackup diagram: stackup JSON -> drawing-style SVG section.

Pure stdlib so it runs anywhere kicad-cli output is available; rasterize the
SVG with `circuit_rasterize` when `rsvg-convert` is present.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any, cast

from circuit.kicad_cli import KicadCliError

_TYPE_COLORS = {
    "BSLT_SILKSCREEN": "#f2f2f2",
    "BSLT_COPPER": "#c87533",
    "BSLT_DIELECTRIC": "#3f7a3f",
    "BSLT_MASK": "#175c2e",
    "BSLT_SOLDERMASK": "#175c2e",
    "BSLT_PASTE": "#c0c0c0",
    "BSLT_FINISH": "#d4af37",
    "BSLT_EDGECONNECTOR": "#d4af37",
    "BSLT_BEVELEDGE": "#9e9e9e",
}
_FALLBACK_COLOR = "#9e9e9e"
_BAND_PX_PER_MM = 110.0
_MIN_BAND_PX = 7.0
_LABEL_X = 340.0
_LABEL_MIN_GAP = 14.0
_LABEL_FONT_SIZE = 12
_WIDTH = 860.0
_MARGIN = 14.0
_DIELECTRIC_KIND = {"BSDT_CORE": "core", "BSDT_PREPREG": "prepreg"}


def _thickness_mm(layer: dict[str, Any]) -> float:
    thickness = layer.get("thickness")
    if not isinstance(thickness, dict):
        return 0.0
    raw = cast(dict[str, Any], thickness).get("valueNm")
    if not isinstance(raw, str) or not raw:
        return 0.0
    try:
        return int(raw) / 1_000_000.0
    except ValueError:
        return 0.0


def _dielectric_detail(layer: dict[str, Any]) -> str:
    dielectric = layer.get("dielectric")
    if not isinstance(dielectric, dict):
        return ""
    sublayers = cast(dict[str, Any], dielectric).get("layer")
    if not isinstance(sublayers, list):
        return ""
    parts: list[str] = []
    for raw in cast(list[Any], sublayers):
        if not isinstance(raw, dict):
            continue
        sub = cast(dict[str, Any], raw)
        material = sub.get("materialName")
        epsilon = sub.get("epsilonR")
        loss = sub.get("lossTangent")
        detail = ""
        if isinstance(material, str) and material:
            detail = material
        if isinstance(epsilon, (int, float)):
            detail += f" er={epsilon}"
        if isinstance(loss, (int, float)):
            detail += f" tan={loss}"
        if detail:
            parts.append(detail.strip())
    return "; ".join(parts)


def _layer_label(layer: dict[str, Any], dielectric_index: int = 0) -> str:
    name = layer.get("userName")
    if (
        isinstance(layer.get("dielectric"), dict)
        and dielectric_index
        and (not name or str(layer.get("layer") or "") == "BL_UNDEFINED")
    ):
        dtype = cast(dict[str, Any], layer["dielectric"]).get("type")
        kind = _DIELECTRIC_KIND.get(str(dtype), str(dtype or "dielectric").lower())
        name = f"Dielectric {dielectric_index} ({kind})"
    if not name:
        name = layer.get("layer") or "layer"
    material = layer.get("materialName")
    mm = _thickness_mm(layer)
    parts = [str(name)]
    if isinstance(material, str) and material:
        parts.append(material)
    if mm > 0:
        parts.append(f"{mm:.3f} mm")
    detail = _dielectric_detail(layer)
    if detail:
        parts.append(detail)
    return " - ".join(parts)


def stackup_svg(data: dict[str, object]) -> str:
    """Render a `pcb export stackup --format json` payload as a section SVG."""
    layers = data.get("layers")
    if not isinstance(layers, list):
        raise KicadCliError("stackup report has no layers list")
    bands: list[tuple[dict[str, Any], float]] = []
    for raw in cast(list[Any], layers):
        if not isinstance(raw, dict):
            continue
        layer = cast(dict[str, Any], raw)
        if layer.get("enabled") is False:
            continue
        mm = _thickness_mm(layer)
        height = max(mm * _BAND_PX_PER_MM, _MIN_BAND_PX) if mm > 0 else _MIN_BAND_PX
        bands.append((layer, height))
    if not bands:
        raise KicadCliError("stackup report has no enabled layers")
    total = sum(height for _, height in bands)
    height = total + 2 * _MARGIN
    labels: list[tuple[float, float, str]] = []
    dielectrics = 0
    y = _MARGIN + 6.0
    for layer, band in bands:
        if isinstance(layer.get("dielectric"), dict):
            dielectrics += 1
        centre = y + band / 2
        text = _layer_label(layer, dielectrics)
        desired = centre + 4
        label_y = desired if not labels else max(desired, labels[-1][0] + _LABEL_MIN_GAP)
        labels.append((label_y, centre, text))
        y += band
    height = max(total + 2 * _MARGIN, labels[-1][0] + 6.0 + _MARGIN)
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_WIDTH:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {_WIDTH:.0f} {height:.0f}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{_MARGIN:.0f}" y="{_MARGIN:.0f}" '
        'font-family="monospace" font-size="10" fill="#666666">stackup (section)</text>',
    ]
    y = _MARGIN + 6.0
    for layer, band in bands:
        color = _TYPE_COLORS.get(str(layer.get("type")), _FALLBACK_COLOR)
        out.append(
            f'<rect x="{_MARGIN:.0f}" y="{y:.2f}" width="{_LABEL_X - 2 * _MARGIN:.0f}" '
            f'height="{band:.2f}" fill="{color}" stroke="#222222" stroke-width="0.5"/>'
        )
        y += band
    band_edge = _LABEL_X - 2 * _MARGIN
    for label_y, centre, text in labels:
        if label_y - 4 != centre:
            out.append(
                f'<line x1="{band_edge:.0f}" y1="{centre:.2f}" '
                f'x2="{_LABEL_X - 8:.0f}" y2="{label_y - 5:.2f}" '
                'stroke="#888888" stroke-width="0.5"/>'
            )
        out.append(
            f'<text x="{_LABEL_X:.0f}" y="{label_y:.2f}" font-family="monospace" '
            f'font-size="{_LABEL_FONT_SIZE}" fill="#111111">{escape(text)}</text>'
        )
    out.append("</svg>")
    return "".join(out) + "\n"


def write_stackup_diagram(data: dict[str, object], out: Path) -> Path:
    """Write `stackup_svg(data)` to `out`, returning the path."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(stackup_svg(data), encoding="utf-8")
    return out
