from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from scripts import run_real_mutation_matrix
from scripts.run_real_mutation_matrix import (
    _FIXTURE_DIR,  # pyright: ignore[reportPrivateUsage]
    _entry,  # pyright: ignore[reportPrivateUsage]
    _prepare_baseline,  # pyright: ignore[reportPrivateUsage]
)

from circuit import corpus
from circuit.libitems import parse_footprint
from circuit.mutation import MutationReport
from circuit.partspec import PartSpec, PartSpecReport, load_part_spec


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


def test_real_mutation_matrix_success_marks_synthetic_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("tps62130-vqfn16")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    pdf_path = cache_dir / f"{entry.id}.pdf"
    pdf_path.write_bytes(b"%PDF-fixture")
    fixture_root = tmp_path / "parts"
    (fixture_root / entry.id).mkdir(parents=True)
    spec = load_part_spec(_FIXTURE_DIR / "part.spec.json")
    report = MutationReport(
        seed=62130,
        baseline_findings=[],
        outcomes=[],
        family_detection_rates={},
        single_oracle=[],
        undetected=[],
        passed=True,
    )
    monkeypatch.setattr(run_real_mutation_matrix, "_FIXTURE_ROOT", fixture_root)

    def fake_sha256(_path: Path) -> str:
        return entry.datasheet.sha256

    monkeypatch.setattr(
        run_real_mutation_matrix.corpus,
        "sha256",
        fake_sha256,
    )

    def prepare_baseline(
        _entry: corpus.CorpusEntry,
        _pdf_path: Path,
        _work_dir: Path,
        _fixture_dir: Path,
    ) -> tuple[PartSpec, Path, Path, Path, Path, PartSpecReport]:
        return (
            spec,
            tmp_path / "spec.json",
            tmp_path / "check.json",
            tmp_path / "footprint.kicad_mod",
            tmp_path / "model.step",
            PartSpecReport(
                artifact_kind="circuit_part_spec_check",
                verdict="pass",
                part_spec_sha256="a" * 64,
                extraction_sha256="b" * 64,
                pdf_sha256="c" * 64,
                checked_readings=0,
                findings=[],
            ),
        )

    monkeypatch.setattr(run_real_mutation_matrix, "_prepare_baseline", prepare_baseline)

    def fake_fixture(**_kwargs: object) -> object:
        return object()

    def fake_run_mutations(_fixture: object) -> MutationReport:
        return report

    monkeypatch.setattr(
        run_real_mutation_matrix,
        "library_mutation_fixture",
        fake_fixture,
    )
    monkeypatch.setattr(
        run_real_mutation_matrix,
        "run_mutations",
        fake_run_mutations,
    )
    output = tmp_path / "matrix.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_real_mutation_matrix.py",
            "--entry",
            entry.id,
            "--cache",
            str(cache_dir),
            "--out",
            str(output),
            "--export-oracle",
        ],
    )

    assert run_real_mutation_matrix.main() == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact == {
        "artifact_kind": "circuit_real_mutation_matrix",
        "entry": entry.id,
        "pdf_sha256": entry.datasheet.sha256,
        "synthetic_evidence": ["vision_reads", "advisory_reviews"],
        "synthetic_evidence_counted": False,
        "matrix": report.model_dump(mode="json"),
    }


def test_real_mutation_matrix_failure_output_keeps_its_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("tps62130-vqfn16")
    output = tmp_path / "failure.json"
    monkeypatch.delenv("CIRCUIT_REQUIRE_CORPUS_PDFS", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_real_mutation_matrix.py",
            "--entry",
            entry.id,
            "--cache",
            str(tmp_path / "missing-cache"),
            "--out",
            str(output),
        ],
    )

    assert run_real_mutation_matrix.main() == 1
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert set(artifact) == {"artifact_kind", "entry", "error"}
    assert artifact["artifact_kind"] == "circuit_real_mutation_matrix_failure"
    assert artifact["entry"] == entry.id
