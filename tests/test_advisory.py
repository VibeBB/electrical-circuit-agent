import pytest
from pydantic import ValidationError

from circuit.advisory import (
    AdvisoryResult,
    VisualReviewDetail,
    parse_visual_review,
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
