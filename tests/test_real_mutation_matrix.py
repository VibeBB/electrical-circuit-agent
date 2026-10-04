from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from scripts.run_real_mutation_matrix import (
    _FIXTURE_DIR,  # pyright: ignore[reportPrivateUsage]
    _entry,  # pyright: ignore[reportPrivateUsage]
    _prepare_baseline,  # pyright: ignore[reportPrivateUsage]
)

from circuit import corpus
from circuit.libitems import parse_footprint


def test_tps62130_fixture_matches_documented_land_pattern() -> None:
    footprint = parse_footprint(_FIXTURE_DIR / "footprint.kicad_mod")
    perimeter = [pad for pad in footprint.pads if pad.number != "17"]
    exposed = next(pad for pad in footprint.pads if pad.number == "17")
    lineage = json.loads((_FIXTURE_DIR / "lineage.json").read_text(encoding="utf-8"))

    assert sum(round(abs(pad.x), 1) == 1.4 for pad in perimeter) == 8
    assert sum(round(abs(pad.y), 1) == 1.4 for pad in perimeter) == 8
    assert exposed.width == pytest.approx(1.68)
    assert exposed.height == pytest.approx(1.68)
    assert exposed.paste_margin == pytest.approx(-0.065)
    assert "one 1.55 mm square stencil aperture" in lineage["exposed_pad_paste_basis"]
    assert lineage["pages"] == [40, 41, 42]


def test_real_part_baseline_requires_pdf_when_configured(
    tmp_path: Path,
) -> None:
    if os.environ.get("CIRCUIT_REQUIRE_CORPUS_PDFS") != "1":
        pytest.skip("set CIRCUIT_REQUIRE_CORPUS_PDFS=1 to run the real-PDF baseline check")

    cache_value = os.environ.get("CIRCUIT_CORPUS_CACHE")
    cache_dir = Path(cache_value) if cache_value else tmp_path / "corpus-cache"
    pdf_path = cache_dir / "tps62130-vqfn16.pdf"
    assert pdf_path.is_file(), f"required corpus PDF is missing: {pdf_path}"
    entry = _entry("tps62130-vqfn16")
    assert corpus.sha256(pdf_path) == entry.datasheet.sha256

    work_dir = tmp_path / "baseline"
    work_dir.mkdir()
    _spec, _spec_path, _check_path, _footprint, _model, report = _prepare_baseline(
        entry,
        pdf_path,
        work_dir,
    )

    assert report.verdict == "pass"
    assert not [finding for finding in report.findings if finding.severity == "error"]
