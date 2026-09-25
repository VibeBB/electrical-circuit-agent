import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from circuit.advisory import (
    AdvisoryResult,
    VisualReviewDetail,
    parse_visual_review,
    review_record_path,
    write_review_record,
)


def _vision_result() -> AdvisoryResult:
    return AdvisoryResult(
        tool="vision_review",
        stage="review",
        status="ok",
        summary="top view clean",
        artifacts=["circuit-reports/render-top.png"],
        detail={
            "image_path": "circuit-reports/render-top.png",
            "image_sha256": "a" * 64,
            "model": "kimi-k3",
            "checklist": "board_top",
            "impression": "silkscreen reads like an assembly guide; return path is legible",
            "findings": [
                {
                    "category": "silkscreen_overlap",
                    "severity": "warning",
                    "note": "C3 designator overlaps U1 outline",
                    "bbox": [0.1, 0.2, 0.05, 0.03],
                },
                {
                    "category": "design_intent",
                    "severity": "info",
                    "note": "decoupling drawn far from the IC it serves",
                },
                {"category": "other", "severity": "info", "note": "sparse right edge"},
            ],
        },
    )


def test_parse_visual_review_round_trip() -> None:
    detail = parse_visual_review(_vision_result())
    assert detail is not None
    assert detail.checklist == "board_top"
    assert detail.model == "kimi-k3"
    assert detail.impression.startswith("silkscreen reads like")
    assert len(detail.findings) == 3
    assert detail.findings[0].bbox == [0.1, 0.2, 0.05, 0.03]
    assert detail.findings[1].category == "design_intent"


def test_parse_visual_review_rejects_non_vision_tool() -> None:
    result = _vision_result()
    result.tool = "run_design_review"
    assert parse_visual_review(result) is None


def test_parse_visual_review_rejects_malformed_detail() -> None:
    result = _vision_result()
    result.detail = {"image_path": "x.png", "image_sha256": "not-a-hash"}
    assert parse_visual_review(result) is None


def test_parse_visual_review_requires_impression() -> None:
    result = _vision_result()
    assert result.detail is not None
    del result.detail["impression"]
    assert parse_visual_review(result) is None

    result = _vision_result()
    assert result.detail is not None
    result.detail["impression"] = ""
    assert parse_visual_review(result) is None


def test_visual_review_detail_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        VisualReviewDetail.model_validate(
            {
                "image_path": "x.png",
                "image_sha256": "a" * 64,
                "model": "m",
                "checklist": "board_top",
                "impression": "i",
                "verdict": "fail",
            }
        )


def test_visual_review_detail_accepts_drawing_quality_categories() -> None:
    for category in (
        "ambiguous_notation",
        "missing_dimension",
        "missing_manufacturing_info",
        "design_intent",
    ):
        detail = VisualReviewDetail.model_validate(
            {
                "image_path": "x.png",
                "image_sha256": "a" * 64,
                "model": "m",
                "checklist": "board_top",
                "impression": "reads clearly",
                "findings": [{"category": category, "severity": "info", "note": "x"}],
            }
        )
        assert detail.findings[0].category == category


def test_review_record_path_slugifies(tmp_path: Path) -> None:
    image = tmp_path / "Schematic Page (v2).PNG"
    image.write_bytes(b"x")
    path = review_record_path(image)
    assert path.name == "review-visual-schematic-page-v2.advisory.json"


def test_write_review_record_binds_sha256(tmp_path: Path) -> None:
    image = tmp_path / "schematic.png"
    image.write_bytes(b"PNGDATA")
    path = write_review_record(
        image,
        model="kimi-k3",
        checklist="schematic",
        impression="clean sheet",
        findings=[{"category": "label_readability", "severity": "info", "note": "ok"}],
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["tool"] == "vision_review"
    assert record["stage"] == "review"
    assert record["detail"]["image_sha256"] == hashlib.sha256(b"PNGDATA").hexdigest()
    assert parse_visual_review(AdvisoryResult.model_validate(record)) is not None


def test_write_review_record_fails_closed_on_bad_finding(tmp_path: Path) -> None:
    image = tmp_path / "schematic.png"
    image.write_bytes(b"PNGDATA")
    with pytest.raises(ValidationError):
        write_review_record(
            image,
            model="kimi-k3",
            checklist="schematic",
            impression="x",
            findings=[{"category": "not_a_category", "severity": "info", "note": "x"}],
        )
    assert not (tmp_path / "review-visual-schematic.advisory.json").exists()
