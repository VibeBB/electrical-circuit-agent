import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from circuit import humanrequest, partspec, revwatch

_FIXTURE = (
    Path(__file__).resolve().parent / "data" / "corpus_parts" / "tps62130-vqfn16" / "part.spec.json"
)


def _write_spec(
    tmp_path: Path,
    *,
    errata: list[dict[str, object]] | None = None,
) -> tuple[partspec.PartSpec, Path]:
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    raw["datasheet"]["source_url"] = "https://manufacturer.example/datasheet.pdf"
    if errata is not None:
        raw["datasheet"]["errata"] = errata
    spec_path = tmp_path / "part.spec.json"
    spec_path.write_text(json.dumps(raw), encoding="utf-8")
    return partspec.load_part_spec(spec_path), spec_path


def _snapshot(revision: str | None, digest: str, url: str) -> revwatch.DatasheetRevisionSnapshot:
    return revwatch.DatasheetRevisionSnapshot(
        revision=revision,
        pdf_sha256=digest,
        source_url=url,
        retrieved_at=datetime.now(UTC),
    )


def test_fetch_current_revision_hashes_manifest_pdf_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = "https://manufacturer.example/revisions.json"
    pdf_url = "https://manufacturer.example/current.pdf"
    pdf_data = b"%PDF-1.4 fixture"
    pdf_sha256 = hashlib.sha256(pdf_data).hexdigest()
    manifest = json.dumps(
        {
            "revision": "B",
            "pdf_url": pdf_url,
            "pdf_sha256": pdf_sha256,
        }
    ).encode("utf-8")
    responses = {
        source_url: (manifest, "application/json", source_url),
        pdf_url: (pdf_data, "application/pdf", pdf_url),
    }

    def fetch_response(url: str) -> tuple[bytes, str, str]:
        return responses[url]

    monkeypatch.setattr(  # pyright: ignore[reportPrivateUsage]
        revwatch,
        "_fetch_response",
        fetch_response,
    )

    snapshot = revwatch.fetch_current_revision(source_url)

    assert snapshot.revision == "B"
    assert snapshot.pdf_sha256 == pdf_sha256
    assert snapshot.source_url == pdf_url
    assert snapshot.retrieved_at.tzinfo is not None


def test_revision_change_writes_request_and_invalidates_bound_approval(tmp_path: Path) -> None:
    spec, spec_path = _write_spec(tmp_path)
    current_sha256 = hashlib.sha256(b"new manufacturer datasheet").hexdigest()
    snapshot = _snapshot("B", current_sha256, "https://manufacturer.example/datasheet.pdf")
    fetched_urls: list[str] = []

    def fetcher(url: str) -> revwatch.DatasheetRevisionSnapshot:
        fetched_urls.append(url)
        return snapshot

    result = revwatch.check_revision(spec, fetcher, spec_path=spec_path)

    assert fetched_urls == ["https://manufacturer.example/datasheet.pdf"]
    assert result.changed is True
    assert result.approval_invalidated is True
    assert result.human_request_id is not None
    assert any(item.code == "datasheet_revision_changed" for item in result.findings)
    request_path = result.human_request_json
    assert request_path is not None
    request = humanrequest.load_request(Path(request_path))
    assert request.kind == "datasheet_acquisition"
    assert isinstance(request.details, humanrequest.DatasheetAcquisitionDetails)
    assert request.details.failure_reason == "revision_mismatch"
    assert request.details.required_sections
    assert request.details.attempted_sources == [snapshot.source_url]
    assert request.details.current_source_sha256 == snapshot.pdf_sha256
    assert request.details.optional_cad_requested is False
    assert revwatch.approval_blocker(tmp_path / "library", spec) == "datasheet_revision_changed"

    updated_spec = spec.model_copy(
        update={
            "datasheet": spec.datasheet.model_copy(
                update={"revision": "B", "sha256": current_sha256}
            )
        }
    )
    assert revwatch.approval_blocker(tmp_path / "library", updated_spec) is None

    repeated = revwatch.check_revision(spec, fetcher, spec_path=spec_path)
    assert repeated.human_request_id == result.human_request_id
    assert len(list((tmp_path / "library" / "requests").glob("*.json"))) == 1


def test_erratum_affect_requires_reading_bound_to_errata_pdf(tmp_path: Path) -> None:
    errata_pdf = tmp_path / "errata.pdf"
    errata_pdf.write_bytes(b"%PDF-1.4 synthetic errata")
    errata_sha256 = hashlib.sha256(errata_pdf.read_bytes()).hexdigest()
    spec, spec_path = _write_spec(
        tmp_path,
        errata=[
            {
                "path": errata_pdf.name,
                "sha256": errata_sha256,
                "revision": "B",
                "title": "Package dimension correction",
                "affects": ["package.body_length"],
            }
        ],
    )
    snapshot = _snapshot(
        spec.datasheet.revision,
        spec.datasheet.sha256,
        "https://manufacturer.example/datasheet.pdf",
    )

    result = revwatch.check_revision(
        spec,
        lambda _url: snapshot,
        spec_path=spec_path,
    )

    assert result.changed is False
    assert [
        (finding.code, finding.field)
        for finding in result.findings
        if finding.code == "errata_unbound"
    ] == [("errata_unbound", "package.body_length")]


def test_granted_alternative_evidence_can_bind_an_errata_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errata_pdf = tmp_path / "errata.pdf"
    errata_pdf.write_bytes(b"%PDF-1.4 synthetic errata")
    errata_sha256 = hashlib.sha256(errata_pdf.read_bytes()).hexdigest()
    spec, spec_path = _write_spec(
        tmp_path,
        errata=[
            {
                "path": errata_pdf.name,
                "sha256": errata_sha256,
                "revision": "B",
                "title": "Package dimension correction",
                "affects": ["package.body_length"],
            }
        ],
    )
    request = humanrequest.build_request(
        kind="alternative_evidence",
        subject={"manufacturer": spec.manufacturer, "mpn": spec.mpn},
        reason="Bind the corrected package length to the errata PDF.",
        evidence=[
            {
                "kind": "document",
                "ref": str(errata_pdf),
                "sha256": errata_sha256,
                "summary": "Errata PDF carrying the corrected package length.",
            }
        ],
        known=["The errata PDF hash matches the local evidence file."],
        unknown=["The corrected dimension must still be checked against the library."],
        agent_assessment=(
            "The errata file is the cited source for the corrected package length. "
            "A grant only binds this field to the supplied document and does not establish "
            "that the resulting library footprint is correct. The approval gate continues "
            "to require independent verification of the authored geometry."
        ),
        recommendation="Grant the package-length evidence binding.",
        recommendation_rationale="The reviewer can inspect the errata PDF directly.",
        alternatives=[
            humanrequest.RequestAlternative(
                option="Grant the package-length evidence binding.",
                risks=["A misread erratum could support an incorrect dimension."],
            ),
            humanrequest.RequestAlternative(
                option="Deny the evidence binding.",
                risks=["The affected PartSpec field remains unbound."],
            ),
        ],
        recommended=0,
        details={
            "kind": "alternative_evidence",
            "evidence_kind": "other_document",
            "files": [{"path": str(errata_pdf), "sha256": errata_sha256}],
            "covers": ["/package/body_length"],
            "unknown_fields": [],
        },
    )
    request_path, _ = humanrequest.write_request(request, tmp_path)
    events_root = tmp_path / "events"
    events_root.mkdir()
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events_root))
    event_bytes = json.dumps(
        {
            "source": "user",
            "message": {
                "content": "\n".join(
                    [
                        f"CIRCUIT-HUMAN-RESPONSE {request.request_id}",
                        "decision: grant",
                        "reviewer: Test Reviewer",
                        "covers: /package/body_length",
                    ]
                )
            },
        }
    ).encode("utf-8")
    event_path = events_root / "grant.json"
    event_path.write_bytes(event_bytes)
    event_sha256 = hashlib.sha256(event_bytes).hexdigest()
    response_dir = request_path.parent / "responses"
    response_dir.mkdir()
    (response_dir / f"{request.request_id}.{event_sha256[:12]}.json").write_text(
        json.dumps(
            {
                "artifact_kind": "circuit_human_response_pointer",
                "request_id": request.request_id,
                "request_sha256": request.request_sha256,
                "event_path": str(event_path),
                "event_sha256": event_sha256,
                "recorded_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    reading = spec.package.body_length.reading
    assert reading is not None
    body_length = spec.package.body_length.model_copy(
        update={"reading": reading.model_copy(update={"alternative_evidence": request.request_id})}
    )
    spec = spec.model_copy(
        update={"package": spec.package.model_copy(update={"body_length": body_length})}
    )
    snapshot = _snapshot(
        spec.datasheet.revision,
        spec.datasheet.sha256,
        "https://manufacturer.example/datasheet.pdf",
    )

    result = revwatch.check_revision(spec, lambda _url: snapshot, spec_path=spec_path)

    assert not any(item.code == "errata_unbound" for item in result.findings)


def test_revision_check_fails_closed_without_source_url(tmp_path: Path) -> None:
    spec, spec_path = _write_spec(tmp_path)
    spec = spec.model_copy(
        update={"datasheet": spec.datasheet.model_copy(update={"source_url": None, "url": None})}
    )

    def unexpected_fetch(_url: str) -> revwatch.DatasheetRevisionSnapshot:
        raise AssertionError("fetcher must not run without a source URL")

    result = revwatch.check_revision(spec, unexpected_fetch, spec_path=spec_path)

    assert result.changed is False
    assert result.findings[0].code == "datasheet_source_url_missing"
