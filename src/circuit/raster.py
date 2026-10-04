"""Rasterize PDF/SVG intake files to PNG for the vision lane.

Uses the system binaries `pdftoppm` (poppler-utils) and `rsvg-convert`
(librsvg2-bin) installed in the tools image. Missing binaries fail closed —
advisory lanes degrade, never guess.
"""

from __future__ import annotations

import math
import os
import shlex
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

from PIL import Image, ImageOps

_PDFTOPPM_ENV = "CIRCUIT_PDFTOPPM"
_RSVG_ENV = "CIRCUIT_RSVG_CONVERT"
_GLYPH_GRID = (32, 48)


class RasterizeError(RuntimeError):
    """Raised when a file cannot be rasterized."""


def glyph_signature(image: Image.Image) -> tuple[float, ...] | None:
    """Return a fixed-size, ink-normalized glyph signature."""
    grayscale = ImageOps.grayscale(image)

    def binarize(value: int) -> int:
        return 255 if value < 160 else 0

    binary = grayscale.point(binarize)  # pyright: ignore[reportUnknownMemberType]
    ink_bbox = binary.getbbox()
    if ink_bbox is None:
        return None
    cropped = binary.crop(ink_bbox)
    resized = cropped.resize(  # pyright: ignore[reportUnknownMemberType]
        _GLYPH_GRID, Image.Resampling.LANCZOS
    )
    pixels = cast(Iterable[int], resized.getdata())
    return tuple(float(value) / 255 for value in pixels)


def normalized_cross_correlation(first: Sequence[float], second: Sequence[float]) -> float | None:
    """Compute zero-mean normalized cross-correlation without array dependencies."""
    if len(first) != _GLYPH_GRID[0] * _GLYPH_GRID[1] or len(first) != len(second):
        return None
    first_mean = sum(first) / len(first)
    second_mean = sum(second) / len(second)
    first_centered = [value - first_mean for value in first]
    second_centered = [value - second_mean for value in second]
    first_energy = sum(value * value for value in first_centered)
    second_energy = sum(value * value for value in second_centered)
    denominator = math.sqrt(first_energy * second_energy)
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(first_centered, second_centered, strict=True)) / denominator


def _command(env_name: str, default: str) -> list[str]:
    return shlex.split(os.environ.get(env_name, default))


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        binary = command[0]
        package = "poppler-utils" if "pdftoppm" in binary else "librsvg2-bin"
        raise RasterizeError(
            f"{binary} is not available; install the {package} apt package "
            f"or set {os.path.basename(binary).upper()} path env overrides"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RasterizeError(f"rasterizer failed: {exc}") from exc


def rasterize(
    source: Path,
    out_dir: Path,
    *,
    dpi: int = 150,
    first_page: int | None = None,
    last_page: int | None = None,
) -> list[Path]:
    """Rasterize `source` (.pdf or .svg) to PNG pages in `out_dir`."""
    if not source.is_file() or source.stat().st_size == 0:
        raise RasterizeError(f"rasterize source is missing or empty: {source}")
    if dpi <= 0:
        raise RasterizeError("dpi must be positive")
    effective_first = first_page if first_page is not None else 1
    if (
        (first_page is not None and first_page < 1)
        or (last_page is not None and last_page < 1)
        or (last_page is not None and last_page < effective_first)
    ):
        raise RasterizeError(
            "page numbers must be positive and last_page must not precede first_page"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        prefix = out_dir / source.stem
        page_args: list[str] = []
        if first_page is not None:
            page_args.extend(["-f", str(first_page)])
        if last_page is not None:
            page_args.extend(["-l", str(last_page)])
        result = _run(
            [
                *_command(_PDFTOPPM_ENV, "pdftoppm"),
                "-png",
                "-r",
                str(dpi),
                *page_args,
                str(source),
                str(prefix),
            ]
        )
        if result.returncode:
            raise RasterizeError(result.stderr.strip() or "pdftoppm failed")
        images = sorted(
            (path for path in out_dir.glob(f"{source.stem}-*.png") if path.is_file()),
            key=_page_key,
        )
    elif suffix == ".svg":
        if first_page is not None or last_page is not None:
            raise RasterizeError("page selection is only supported for PDF sources")
        out = out_dir / f"{source.stem}.png"
        result = _run(
            [
                *_command(_RSVG_ENV, "rsvg-convert"),
                "--dpi-x",
                str(dpi),
                "--dpi-y",
                str(dpi),
                "--output",
                str(out),
                str(source),
            ]
        )
        if result.returncode:
            raise RasterizeError(result.stderr.strip() or "rsvg-convert failed")
        images = [out] if out.is_file() else []
    else:
        raise RasterizeError(f"unsupported rasterize source type: {source.suffix or source.name}")
    if not images:
        raise RasterizeError(f"rasterizer produced no PNG output in {out_dir}")
    return images


def _page_key(path: Path) -> tuple[int, str]:
    digits = "".join(ch for ch in path.stem.split("-")[-1] if ch.isdigit())
    return (int(digits) if digits else 0, path.name)
