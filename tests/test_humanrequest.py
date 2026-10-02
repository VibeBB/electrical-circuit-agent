from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from circuit.humanrequest import (
    HumanRequestError,
    build_request,
    load_request,
    write_request,
)

_ASSESSMENT = (
    "The request collects the available source material and deterministic findings for "
    "human review. Compare each claim with the cited evidence and identify any unresolved "
    "risk before making a decision. Hash agreement and agent confidence are not proof that "
    "the underlying library content is correct."
)


def _request_fields() -> dict[str, Any]:
    return {
        "kind": "library_review",
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "subject": {"manufacturer": "Example", "mpn": "TEST-1"},
        "reason": "Review the package library artifacts against the source evidence.",
        "evidence": [
            {
                "kind": "note",
                "ref": "verification",
                "summary": "Fresh deterministic verification report.",
            }
        ],
        "known": ["The report is bound to the current artifact hashes."],
        "unknown": [],
        "agent_assessment": _ASSESSMENT,
        "recommendation": "Review before approval.",
        "recommendation_rationale": "The evidence requires an independent human decision.",
        "alternatives": [
            {
                "option": "Review before approval.",
                "risks": ["A source discrepancy could be missed."],
            },
            {
                "option": "Request changes.",
                "risks": ["The library release will be delayed."],
            },
        ],
        "recommended": 0,
        "details": {"kind": "library_review", "packet_id": "a" * 16},
    }


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "subject",
        "reason",
        "evidence",
        "known",
        "agent_assessment",
        "recommendation",
        "recommendation_rationale",
        "alternatives",
        "recommended",
        "details",
    ],
)
def test_missing_required_request_fields_are_rejected(field: str) -> None:
    fields = _request_fields()
    fields.pop(field)
    with pytest.raises(ValidationError):
        build_request(**fields)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("subject", "manufacturer"), " "),
        (("subject", "mpn"), ""),
        (("reason",), " "),
        (("evidence", 0, "ref"), ""),
        (("evidence", 0, "summary"), " "),
        (("known", 0), ""),
        (("alternatives", 0, "option"), " "),
        (("alternatives", 0, "risks", 0), ""),
    ],
)
def test_empty_request_strings_are_rejected(path: tuple[object, ...], value: str) -> None:
    fields = _request_fields()
    target: Any = fields
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        build_request(**fields)


def test_non_review_request_requires_unknowns() -> None:
    fields = _request_fields()
    fields.update(
        kind="datasheet_acquisition",
        unknown=[],
        details={
            "kind": "datasheet_acquisition",
            "failure_reason": "not_found",
            "required_sections": ["package_drawing"],
            "attempted_sources": ["manufacturer website: not found"],
            "optional_cad_requested": False,
        },
    )
    with pytest.raises(ValidationError, match="unknown must contain at least one item"):
        build_request(**fields)


def test_request_requires_two_risk_bearing_alternatives() -> None:
    fields = _request_fields()
    fields["alternatives"] = fields["alternatives"][:1]
    with pytest.raises(ValidationError):
        build_request(**fields)
    fields = _request_fields()
    fields["alternatives"][0]["risks"] = []
    with pytest.raises(ValidationError):
        build_request(**fields)


def test_recommended_alternative_must_match_recommendation() -> None:
    fields = _request_fields()
    fields["recommended"] = 1
    with pytest.raises(ValidationError, match="recommended alternative must match"):
        build_request(**fields)


def test_agent_assessment_must_be_multi_sentence_prose() -> None:
    fields = _request_fields()
    fields["agent_assessment"] = "This is too short."
    with pytest.raises(ValidationError, match="impression must contain"):
        build_request(**fields)


def test_request_details_kind_must_match_request_kind() -> None:
    fields = _request_fields()
    fields["details"] = {
        "kind": "alternative_evidence",
        "evidence_kind": "measurement",
        "files": [{"path": "measurement.csv", "sha256": "b" * 64}],
        "covers": ["/package/body_length"],
    }
    with pytest.raises(ValidationError):
        build_request(**fields)


def test_request_rejects_extra_fields_and_invalid_sha256() -> None:
    fields = _request_fields()
    fields["unexpected"] = "not permitted"
    with pytest.raises(ValidationError):
        build_request(**fields)

    fields = _request_fields()
    fields["evidence"][0]["sha256"] = "A" * 64
    with pytest.raises(ValidationError):
        build_request(**fields)


def test_request_hash_tamper_is_rejected(tmp_path: Path) -> None:
    request = build_request(**_request_fields())
    json_path, _ = write_request(request, tmp_path)
    stored = json.loads(json_path.read_text(encoding="utf-8"))
    stored["reason"] = "changed after the request was hashed"
    json_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(HumanRequestError, match="human_request_tampered"):
        load_request(json_path)


def test_request_hash_uses_canonical_json() -> None:
    fields = _request_fields()
    fields["subject"]["manufacturer"] = "Éxample"
    request = build_request(**fields)
    payload = request.model_dump(mode="json", exclude={"request_id", "request_sha256"})
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    assert request.request_sha256 == digest
    assert request.request_id == digest[:16]


def test_request_creation_time_must_be_utc() -> None:
    fields = _request_fields()
    fields["created_at"] = datetime.fromisoformat("2025-01-01T00:00:00+01:00")
    with pytest.raises(ValidationError, match="created_at must be timezone-aware UTC"):
        build_request(**fields)


def test_request_creation_time_defaults_to_utc() -> None:
    fields = _request_fields()
    fields.pop("created_at")
    request = build_request(**fields)
    assert request.created_at.tzinfo is UTC


def test_markdown_preserves_section_order_and_embeds_page_images(tmp_path: Path) -> None:
    fields = _request_fields()
    fields["evidence"] = [
        {
            "kind": "page_image",
            "ref": "evidence/page-001.png",
            "sha256": "c" * 64,
            "summary": "Source datasheet page.",
        }
    ]
    _, markdown_path = write_request(build_request(**fields), tmp_path)
    markdown = markdown_path.read_text(encoding="utf-8")
    headings = [
        "## Reason",
        "## Evidence",
        "## Known",
        "## Unknown",
        "## Agent assessment",
        "## Recommendation + rationale",
        "## Alternatives and risks",
        "## How to respond",
    ]
    assert [markdown.index(heading) for heading in headings] == sorted(
        markdown.index(heading) for heading in headings
    )
    assert "![Source datasheet page.](evidence/page-001.png)" in markdown


def test_request_write_is_idempotent_and_rejects_changed_artifacts(tmp_path: Path) -> None:
    request = build_request(**_request_fields())
    first = write_request(request, tmp_path)
    second = write_request(request, tmp_path)
    assert first == second
    assert all(path.is_file() for path in first)

    first[1].write_text("changed", encoding="utf-8")
    with pytest.raises(HumanRequestError, match="human_request_write_conflict"):
        write_request(request, tmp_path)
