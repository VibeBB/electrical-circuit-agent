from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from circuit import libmetrics, libreview, mutation
from circuit.libmetrics import LibraryMetrics
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
)


def _spec() -> PartSpec:
    reading = Reading(
        page=1,
        bbox=(1, 1, 2, 2),
        vision="dimension value",
        vision_record="fixture.json",
    )

    def dimension(value: float) -> Dimension:
        return Dimension(nom=value, reading=reading)

    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="METRICS-1",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="extract.json",
        ),
        package=PackageSpec(
            family="chip",
            drawing_id="chip",
            pin_count=2,
            pitch=dimension(1.0),
            body_length=dimension(2.0),
            body_width=dimension(1.0),
            height=dimension(0.8),
            drawing_view="top",
            pin1_corner="top_left",
            pin1_reading=reading,
        ),
        pins=[
            PinSpec(
                number=str(number),
                name=f"PIN{number}",
                electrical_type="passive",
                reading=reading,
            )
            for number in (1, 2)
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="METRICS-1",
                package_designator="CHIP",
                pin_count=2,
                row=CellRef(table=0, row=1, col=0),
                reading=reading,
            )
        ],
    )


def _empty_mutation_report() -> mutation.MutationReport:
    return mutation.MutationReport(
        seed=1,
        baseline_findings=[],
        outcomes=[],
        family_detection_rates={},
        single_oracle=[],
        undetected=[],
        passed=True,
    )


def _project_files(project: Path) -> tuple[Path, Path]:
    library = project / "library"
    corpus_path = library / "corpus" / "corpus.json"
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    corpus_path.write_text(
        json.dumps({"schema": "circuit_golden_corpus", "version": 1, "entries": []}),
        encoding="utf-8",
    )
    report_path = library / "mutation-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_empty_mutation_report().model_dump_json(), encoding="utf-8")
    return corpus_path, report_path


def _review_packet(
    library: Path,
    spec_path: Path,
    packet_id: str,
    *,
    passing: bool,
) -> Path:
    packet_dir = library / "reviews" / "METRICS-1" / packet_id
    packet_dir.mkdir(parents=True)
    packet = {
        "artifact_kind": "circuit_library_review_packet",
        "packet_id": packet_id,
        "approvable": True,
        "inputs": {
            "spec_path": str(spec_path.resolve()),
            "symbol_lib": str((spec_path.parent / "symbol.kicad_sym").resolve()),
            "symbol_name": "METRICS-1",
            "footprint_path": str((spec_path.parent / "METRICS-1.kicad_mod").resolve()),
            "library_dir": str(library.resolve()),
            "pdf_sha256": "a" * 64,
            "density": "nominal",
            "tolerance_mm": 0.02,
            "model_required": True,
            "pin_source_path": None,
        },
        "fresh_part_spec_check": {"verdict": "pass" if passing else "fail"},
        "fresh_library_verification": {"verdict": "pass" if passing else "fail"},
    }
    path = packet_dir / "review.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path


def test_clopper_pearson_upper_bound_known_values() -> None:
    assert math.isclose(
        libmetrics.clopper_pearson_upper95(299, 0),
        0.01,
        rel_tol=0,
        abs_tol=0.0001,
    )
    assert libmetrics.clopper_pearson_upper95(0, 0) == 1.0
    with pytest.raises(ValueError, match="0 <= k <= n"):
        libmetrics.clopper_pearson_upper95(2, 3)


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/pins/0/name", "pin_map"),
        ("/package/pin1_corner", "pin1"),
        ("/package/drawing_view", "view"),
        ("/pins/0/view", "view"),
        ("/package/exposed_pad/width/nom", "pad_geometry"),
        ("/package/body_length/nom", "body_pitch"),
        ("/model_orientation", "model_orientation"),
        ("/model/orientation", "model_orientation"),
        ("/package/drawing_id", None),
    ],
)
def test_critical_correction_fields_are_classified(pointer: str, expected: str | None) -> None:
    assert libmetrics._critical_field(pointer) == expected  # pyright: ignore[reportPrivateUsage]


def test_metrics_consume_hash_bound_approvals_and_critical_corrections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    library = project / "library"
    corpus_path, report_path = _project_files(project)
    spec = _spec()
    spec_path = project / "part.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    approved_id = "b" * 16
    rejected_id = "c" * 16
    _review_packet(library, spec_path, approved_id, passing=True)
    _review_packet(library, spec_path, rejected_id, passing=True)
    correction = libreview.ReviewCorrection(
        pointer="/package/pitch/nom",
        old=1.0,
        new=1.1,
        reason="The drawing table gives a different pitch.",
        page=4,
    )
    event_sha256 = "d" * 64
    journal = library / "reviews" / "corrections.jsonl"
    journal.write_text(
        json.dumps(
            {
                "packet_id": rejected_id,
                "mpn": spec.mpn,
                "pdf_sha256": spec.datasheet.sha256,
                **correction.model_dump(),
                "event_sha256": event_sha256,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def current_packet_id_for_fixture(path: Path, **_kwargs: Any) -> str:
        return path.parent.name if path.parent.name in (approved_id, rejected_id) else approved_id

    def approved_review_status(
        _library: Path,
        _spec: PartSpec,
        packet_id: str,
        **_kwargs: Any,
    ) -> libreview.ReviewStatus:
        return libreview.ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="approved",
            reasons=[],
            decisions=[],
        )

    monkeypatch.setattr(libreview, "current_packet_id", current_packet_id_for_fixture)
    monkeypatch.setattr(libreview, "review_status", approved_review_status)
    decision = libreview.ReviewDecision(
        packet_id=rejected_id,
        decision="reject",
        reviewer="Human Reviewer",
        answers={},
        corrections=[correction],
        event_sha256=event_sha256,
        valid=True,
        reasons=[],
        integrity_valid=True,
    )

    def load_decisions_for_fixture(_library: Path, _packet: str) -> list[libreview.ReviewDecision]:
        return [decision]

    monkeypatch.setattr(libreview, "load_decisions", load_decisions_for_fixture)

    metrics = libmetrics.compute_metrics(project)

    assert metrics.accepted_parts == 1
    assert metrics.escapes == 1
    assert metrics.upper95 == 1.0
    assert metrics.corpus_manifest_sha256 == hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    assert metrics.mutation_report_sha256 == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert not metrics.release_relaxation_supported


def test_metrics_ignore_corrections_when_gates_did_not_all_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    library = project / "library"
    _project_files(project)
    spec = _spec()
    spec_path = project / "part.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    packet_id = "e" * 16
    _review_packet(library, spec_path, packet_id, passing=False)
    correction = libreview.ReviewCorrection(
        pointer="/pins/0/name",
        old="PIN1",
        new="SIG1",
        reason="The pin table gives a different name.",
        page=2,
    )
    digest = "f" * 64
    (library / "reviews" / "corrections.jsonl").write_text(
        json.dumps(
            {
                "packet_id": packet_id,
                "mpn": spec.mpn,
                "pdf_sha256": spec.datasheet.sha256,
                **correction.model_dump(),
                "event_sha256": digest,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def current_packet_id_for_fixture(_path: Path, **_kwargs: Any) -> str:
        return packet_id

    def approved_review_status(
        _library: Path,
        _spec: PartSpec,
        _packet: str,
        **_kwargs: Any,
    ) -> libreview.ReviewStatus:
        return libreview.ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="approved",
            reasons=[],
            decisions=[],
        )

    monkeypatch.setattr(libreview, "current_packet_id", current_packet_id_for_fixture)
    monkeypatch.setattr(libreview, "review_status", approved_review_status)
    decision = libreview.ReviewDecision(
        packet_id=packet_id,
        decision="reject",
        reviewer="Human Reviewer",
        answers={},
        corrections=[correction],
        event_sha256=digest,
        valid=True,
        reasons=[],
        integrity_valid=True,
    )

    def load_decisions_for_fixture(_library: Path, _packet: str) -> list[libreview.ReviewDecision]:
        return [decision]

    monkeypatch.setattr(libreview, "load_decisions", load_decisions_for_fixture)

    metrics = libmetrics.compute_metrics(project)

    assert metrics.accepted_parts == 1
    assert metrics.escapes == 0


def test_metrics_report_mutation_families_outcomes_and_critical_single_oracle(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _, report_path = _project_files(project)
    outcome = mutation.MutationOutcome(
        mutation=mutation.Mutation(
            operator="model_offset_0_1mm",
            target="model",
            params={"offset_mm": 0.1},
            critical=True,
            seed=7,
        ),
        finding_codes=["model_height"],
        families=["model_geometry"],
        non_vision_family_count=1,
        status="single_oracle",
    )
    report = mutation.MutationReport(
        seed=7,
        baseline_findings=[],
        outcomes=[outcome],
        family_detection_rates={"model_geometry": 1.0},
        single_oracle=["model_offset_0_1mm"],
        undetected=[],
        passed=False,
    )
    report_path.write_text(report.model_dump_json(), encoding="utf-8")

    metrics = libmetrics.compute_metrics(project)

    assert metrics.family_detection_rates == {"model_geometry": 1.0}
    assert metrics.operator_outcomes == [outcome]
    assert metrics.critical_single_oracle == ["model_offset_0_1mm"]
    assert not metrics.release_relaxation_supported


def test_review_relaxation_requires_fresh_hash_bound_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    corpus_path, report_path = _project_files(project)
    metrics = LibraryMetrics(
        accepted_parts=299,
        escapes=0,
        upper95=libmetrics.clopper_pearson_upper95(299, 0),
        corpus_manifest_sha256=hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        mutation_report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
        family_detection_rates={},
        operator_outcomes=[],
        critical_single_oracle=[],
        release_relaxation_supported=True,
        findings=[],
    )

    def compute_metrics_for_fixture(_project: Path) -> LibraryMetrics:
        return metrics

    monkeypatch.setattr(libmetrics, "compute_metrics", compute_metrics_for_fixture)
    metrics_path = project / "library" / "library-metrics.json"
    metrics_path.write_text(metrics.model_dump_json(), encoding="utf-8")

    assert libmetrics.require_relaxation_supported(project) == metrics

    report_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(
        libmetrics.LibraryMetricsError,
        match="review_relaxation_not_supported_by_metrics",
    ):
        libmetrics.require_relaxation_supported(project)
