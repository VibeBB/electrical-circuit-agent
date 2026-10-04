import stat
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from circuit.raster import (
    RasterizeError,
    glyph_signature,
    normalized_cross_correlation,
    rasterize,
)


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_rasterize_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub(
        tmp_path,
        "pdftoppm_stub",
        '#!/bin/bash\nprefix="${@: -1}"\nfor n in 1 2; do printf x > "$prefix-$n.png"; done\n',
    )
    monkeypatch.setenv("CIRCUIT_PDFTOPPM", str(stub))
    source = tmp_path / "datasheet.pdf"
    source.write_bytes(b"%PDF fake")
    out = tmp_path / "pages"
    images = rasterize(source, out, dpi=150)
    assert [p.name for p in images] == ["datasheet-1.png", "datasheet-2.png"]
    result = subprocess.run([str(stub)], capture_output=True, text=True)
    assert result.returncode == 0


def test_rasterize_pdf_page_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = tmp_path / "arguments.txt"
    stub = _stub(
        tmp_path,
        "pdftoppm_pages",
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$CIRCUIT_TEST_ARGUMENTS"\n'
        'prefix="${@: -1}"\n'
        'printf x > "$prefix-3.png"\n',
    )
    monkeypatch.setenv("CIRCUIT_PDFTOPPM", str(stub))
    monkeypatch.setenv("CIRCUIT_TEST_ARGUMENTS", str(arguments))
    source = tmp_path / "datasheet.pdf"
    source.write_bytes(b"%PDF fake")
    images = rasterize(source, tmp_path / "pages", first_page=3, last_page=3)
    assert [path.name for path in images] == ["datasheet-3.png"]
    assert "-f" in arguments.read_text(encoding="utf-8").splitlines()
    assert "-l" in arguments.read_text(encoding="utf-8").splitlines()


def test_rasterize_rejects_invalid_page_selection(tmp_path: Path) -> None:
    source = tmp_path / "datasheet.pdf"
    source.write_bytes(b"%PDF fake")
    for kwargs in (
        {"first_page": 0},
        {"last_page": 0},
        {"first_page": 3, "last_page": 2},
    ):
        with pytest.raises(RasterizeError, match="page numbers"):
            rasterize(source, tmp_path / "pages", **kwargs)


def test_rasterize_rejects_page_selection_for_svg(tmp_path: Path) -> None:
    source = tmp_path / "stackup.svg"
    source.write_text("<svg/>", encoding="utf-8")
    with pytest.raises(RasterizeError, match="only supported for PDF"):
        rasterize(source, tmp_path / "pages", first_page=1)


def test_rasterize_svg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub(
        tmp_path,
        "rsvg_stub",
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do case "$1" in -o|--output) shift; out="$1";; esac; shift; done\n'
        'printf x > "$out"\n',
    )
    monkeypatch.setenv("CIRCUIT_RSVG_CONVERT", str(stub))
    source = tmp_path / "stackup.svg"
    source.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    images = rasterize(source, tmp_path / "out")
    assert [p.name for p in images] == ["stackup.png"]


def test_rasterize_missing_binary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIRCUIT_PDFTOPPM", "/nonexistent/pdftoppm")
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF fake")
    with pytest.raises(RasterizeError, match="poppler-utils"):
        rasterize(source, tmp_path / "out")


def test_rasterize_fails_closed_on_bad_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIRCUIT_PDFTOPPM", "/nonexistent/x")
    with pytest.raises(RasterizeError, match="missing or empty"):
        rasterize(tmp_path / "gone.pdf", tmp_path / "out")
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    with pytest.raises(RasterizeError, match="unsupported"):
        rasterize(source, tmp_path / "out")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF fake")
    fail_stub = _stub(tmp_path, "pdf_fail", "#!/bin/sh\nexit 3\n")
    monkeypatch.setenv("CIRCUIT_PDFTOPPM", str(fail_stub))
    with pytest.raises(RasterizeError):
        rasterize(pdf, tmp_path / "out")


def test_glyph_signature_binarizes_crops_and_resamples_to_fixed_grid() -> None:
    image = Image.new("L", (48, 64), 255)
    ImageDraw.Draw(image).rectangle((14, 12, 30, 52), outline=0, width=3)

    signature = glyph_signature(image)

    assert signature is not None
    assert len(signature) == 32 * 48
    assert normalized_cross_correlation(signature, signature) == pytest.approx(1.0)


def test_zero_mean_ncc_ranks_matching_glyphs_and_fails_on_degenerate_inputs() -> None:
    size = 32 * 48
    first = tuple(float(index == 0) for index in range(size))
    matching = first
    different = tuple(float(index == 1) for index in range(size))

    assert normalized_cross_correlation(first, matching) == pytest.approx(1.0)
    different_score = normalized_cross_correlation(first, different)
    assert different_score is not None
    assert different_score < 0.01
    assert normalized_cross_correlation(first[:-1], first) is None
    assert normalized_cross_correlation((1.0,) * size, (1.0,) * size) is None
