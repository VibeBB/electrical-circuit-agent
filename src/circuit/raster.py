"""Rasterize PDF/SVG intake files to PNG for the vision lane.

Uses the system binaries `pdftoppm` (poppler-utils) and `rsvg-convert`
(librsvg2-bin) installed in the tools image. Missing binaries fail closed —
advisory lanes degrade, never guess.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

_PDFTOPPM_ENV = "CIRCUIT_PDFTOPPM"
_RSVG_ENV = "CIRCUIT_RSVG_CONVERT"


class RasterizeError(RuntimeError):
    """Raised when a file cannot be rasterized."""


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


def rasterize(source: Path, out_dir: Path, *, dpi: int = 150) -> list[Path]:
    """Rasterize `source` (.pdf or .svg) to PNG pages in `out_dir`."""
    if not source.is_file() or source.stat().st_size == 0:
        raise RasterizeError(f"rasterize source is missing or empty: {source}")
    if dpi <= 0:
        raise RasterizeError("dpi must be positive")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        prefix = out_dir / source.stem
        result = _run(
            [*_command(_PDFTOPPM_ENV, "pdftoppm"), "-png", "-r", str(dpi), str(source), str(prefix)]
        )
        if result.returncode:
            raise RasterizeError(result.stderr.strip() or "pdftoppm failed")
        images = sorted(
            (path for path in out_dir.glob(f"{source.stem}-*.png") if path.is_file()),
            key=_page_key,
        )
    elif suffix == ".svg":
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
