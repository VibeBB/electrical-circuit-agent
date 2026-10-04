from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from circuit import corpus, libitems, libreview, occt
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

REPO_ROOT = Path(__file__).parents[1]
CORPUS_ROOT = REPO_ROOT / "library" / "corpus"
_DATASHEET_SHA256_SNAPSHOT = "584ef469c312b3ec7cbf67ba94b3f8801caf303d04212e1c41e55b123af60013"


def _reading(text: str) -> Reading:
    return Reading(page=1, bbox=(0, 0, 1, 1), vision=text, vision_record="test")


def _partspec(pin_name: str = "VCC") -> PartSpec:
    package = PackageSpec(
        family="custom",
        drawing_id="test",
        pin_count=1,
        body_length=Dimension(nom=1, reading=_reading("1 mm")),
        body_width=Dimension(nom=1, reading=_reading("1 mm")),
        height=Dimension(nom=1, reading=_reading("1 mm")),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin 1 at top left"),
    )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TEST",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="test.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="test-extraction.json",
        ),
        package=package,
        pins=[
            PinSpec(
                number="1",
                name=pin_name,
                electrical_type="power_in",
                reading=_reading(f"1 {pin_name}"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="TEST",
                package_designator="test",
                pin_count=1,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("TEST test"),
            )
        ],
    )


def test_manifest_and_seeded_truth_files_validate() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    assert len(manifest.entries) == 40
    assert all(entry.truth_status == "unconfirmed" for entry in manifest.entries)
    canaries: set[str] = set()
    datasheet_hashes = [(entry.id, entry.datasheet.sha256) for entry in manifest.entries]
    for entry in manifest.entries:
        assert len(entry.datasheet.sha256) == 64
        assert all(character in "0123456789abcdef" for character in entry.datasheet.sha256)
        truth, digest = corpus.load_truth(CORPUS_ROOT, entry)
        assert truth.id == entry.id
        assert digest == corpus.sha256(CORPUS_ROOT / entry.truth_path)
        canary_uuid = truth.canary.removeprefix("CIRCUIT-CORPUS-CANARY-")
        assert str(UUID(canary_uuid, version=4)) == canary_uuid
        assert truth.canary not in canaries
        canaries.add(truth.canary)
    snapshot = hashlib.sha256(
        json.dumps(datasheet_hashes, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    assert snapshot == _DATASHEET_SHA256_SNAPSHOT


def test_tht_corpus_pads_compare_drill_and_shape() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    entry = next(item for item in manifest.entries if item.id == "lm317-to220")
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)

    def footprint_for_truth(*, mutate: bool = False) -> libitems.FootprintDef:
        pads: list[libitems.PadDef] = []
        for pad in truth.expected_pads:
            drill = pad.drill
            shape = pad.shape or "rect"
            if mutate and pad.number == "1" and drill is not None:
                drill += 0.02
            if mutate and pad.number == "2":
                shape = "rect" if shape != "rect" else "circle"
            pads.append(
                libitems.PadDef(
                    number=pad.number,
                    type="thru_hole",
                    shape=shape,
                    x=pad.center[0],
                    y=pad.center[1],
                    rotation=0,
                    width=pad.size[0],
                    height=pad.size[1],
                    drill=drill,
                    layers=["*.Cu"],
                )
            )
        return libitems.FootprintDef(
            name="",
            attributes=[],
            pads=pads,
            graphics=[],
            models=[],
            properties={},
        )

    symbol = libitems.SymbolDef(name="", pins=[], properties={})
    model = occt.box(0, 0, 0, 0.001, 0.001, 0.001)
    correct = corpus.score_part(truth, _partspec(), footprint_for_truth(), symbol, model)
    assert not {
        finding.code
        for finding in correct.findings
        if finding.code in {"corpus_pad_drill_mismatch", "corpus_pad_shape_mismatch"}
    }

    incorrect = corpus.score_part(
        truth,
        _partspec(),
        footprint_for_truth(mutate=True),
        symbol,
        model,
    )
    mismatch_codes = {finding.code for finding in incorrect.findings}
    assert "corpus_pad_drill_mismatch" in mismatch_codes
    assert "corpus_pad_shape_mismatch" in mismatch_codes


def test_mcp1700_truth_stays_incomplete_without_datasheet_geometry() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    entry = next(item for item in manifest.entries if item.id == "mcp1700-sot23")
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
    missing = corpus._missing_truth_requirements(truth)  # pyright: ignore[reportPrivateUsage]
    required = {
        "body_length",
        "body_width",
        "height",
        "pitch",
        "lead_span",
        "lead_length",
        "lead_width",
    }
    assert required <= set(missing)
    assert "expected_pads" in missing


def test_datasheet_cache_requires_exact_sha256(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = corpus.load_manifest(CORPUS_ROOT / "corpus.json").entries[0]
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CIRCUIT_CORPUS_CACHE", str(cache))
    assert corpus._cached_pdf_status(entry) == "not_available"  # pyright: ignore[reportPrivateUsage]
    pdf = cache / "cached.pdf"
    pdf.write_bytes(b"not the configured datasheet")
    assert corpus._cached_pdf_status(entry) == "hash_mismatch"  # pyright: ignore[reportPrivateUsage]
    pdf.write_bytes(b"verified")
    entry.datasheet.sha256 = hashlib.sha256(b"verified").hexdigest()
    assert corpus._cached_pdf_status(entry) == "verified"  # pyright: ignore[reportPrivateUsage]


def test_human_confirmation_requires_hash_bound_pr_b_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth_digest = "a" * 64
    event_digest = "b" * 64
    entry = corpus.CorpusEntry(
        id="fixture",
        manufacturer="Example",
        mpn="FIXTURE",
        package_family="custom",
        datasheet=corpus.CorpusDatasheet(
            url="https://example.invalid/fixture.pdf",
            sha256="c" * 64,
            revision="A",
        ),
        truth_path="truth/fixture.json",
        truth_status="human_confirmed",
        confirmed_by="Test Reviewer",
        confirmed_at="2026-01-01T00:00:00Z",
        notes="fixture",
    )
    packet_id = corpus.approval_packet_id(entry.id, truth_digest)
    approval_dir = tmp_path / "corpus" / "approvals"
    approval_dir.mkdir(parents=True)
    approval = corpus.CorpusApproval(
        schema="circuit_corpus_truth_approval",
        version=1,
        entry_id=entry.id,
        packet_id=packet_id,
        truth_sha256=truth_digest,
        event_sha256=event_digest,
        reviewer="Test Reviewer",
        confirmed_at="2026-01-01T00:00:00Z",
    )
    (approval_dir / "fixture.json").write_text(
        approval.model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    seen_packet_ids: list[str] = []

    def load_decisions(_library_root: Path, seen_id: str) -> list[libreview.ReviewDecision]:
        seen_packet_ids.append(seen_id)
        return [
            libreview.ReviewDecision(
                packet_id=seen_id,
                decision="approve",
                reviewer="Test Reviewer",
                answers={"truth_sha256": truth_digest},
                corrections=[],
                event_sha256=event_digest,
                valid=True,
                reasons=[],
                integrity_valid=True,
            )
        ]

    monkeypatch.setattr(corpus.libreview, "load_decisions", load_decisions)
    assert corpus._approval_is_valid(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "corpus",
        entry,
        truth_digest,
    ) == (True, "human_confirmed")
    assert seen_packet_ids == [packet_id]
    assert corpus._approval_is_valid(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "corpus",
        entry,
        "d" * 64,
    ) == (False, "corpus_approval_event_invalid")


def test_unconfirmed_truth_never_scores_as_pass() -> None:
    entry = corpus.load_manifest(CORPUS_ROOT / "corpus.json").entries[0]
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
    footprint = libitems.FootprintDef(
        name="",
        attributes=[],
        pads=[],
        graphics=[],
        models=[],
        properties={},
    )
    symbol = libitems.SymbolDef(name="", pins=[], properties={})
    model = occt.box(0, 0, 0, 0.001, 0.001, 0.001)
    arguments = (truth, _partspec(), footprint, symbol, model)
    result = corpus.score_part(*arguments)
    repeated = corpus.score_part(*arguments)
    assert result.truth_status == "unconfirmed"
    assert result.verdict != "pass"
    assert result.truth_sha256
    assert result.manifest_sha256 == ""
    assert repeated == result


@pytest.mark.parametrize("artifact_name", ["partspec", "footprint", "symbol"])
def test_authored_canary_is_an_error(artifact_name: str) -> None:
    entry = corpus.load_manifest(CORPUS_ROOT / "corpus.json").entries[0]
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
    result = corpus.score_part(
        truth,
        _partspec(truth.canary if artifact_name == "partspec" else "VCC"),
        libitems.FootprintDef(
            name=truth.canary if artifact_name == "footprint" else "",
            attributes=[],
            pads=[],
            graphics=[],
            models=[],
            properties={},
        ),
        libitems.SymbolDef(
            name=truth.canary if artifact_name == "symbol" else "",
            pins=[],
            properties={},
        ),
        occt.box(0, 0, 0, 0.001, 0.001, 0.001),
    )
    finding = next(item for item in result.findings if item.code == "corpus_canary_leak")
    assert finding.severity == "error"


def test_model_file_canary_is_an_error(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    shutil.copytree(CORPUS_ROOT, corpus_root)
    entry = corpus.load_manifest(corpus_root / "corpus.json").entries[0]
    truth, _ = corpus.load_truth(corpus_root, entry)
    partspec_path = tmp_path / "part.spec.json"
    partspec_path.write_text(_partspec().model_dump_json(), encoding="utf-8")
    footprint_path = tmp_path / "footprint.kicad_mod"
    symbol_path = tmp_path / "symbols.kicad_sym"
    model_path = tmp_path / "model.step"
    model_path.write_text(truth.canary, encoding="utf-8")
    result = corpus.score_entry(
        corpus_root,
        entry.id,
        partspec_path,
        footprint_path,
        symbol_path,
        "TEST",
        model_path,
    )
    assert "corpus_canary_leak" in {finding.code for finding in result.findings}


def test_author_lane_cannot_load_or_score_corpus_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "b")
    with pytest.raises(corpus.CorpusError, match="author lanes cannot read"):
        corpus.load_truth(CORPUS_ROOT, manifest.entries[0])
