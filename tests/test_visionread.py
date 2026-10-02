from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from circuit.datasheet import DatasheetExtraction, PageExtraction
from circuit.visionread import (
    VisionAnswerInput,
    VisionAnswerRecord,
    VisionBatch,
    VisionKind,
    VisionReadError,
    VisionReadItem,
    VisionReadRequest,
    _render_pdfium,  # pyright: ignore[reportPrivateUsage]
    _render_pdftoppm,  # pyright: ignore[reportPrivateUsage]
    _write_batch,  # pyright: ignore[reportPrivateUsage]
    create_comparison_batch,
    create_read_batch,
    find_comparison_evidence,
    load_vision_read,
    record_answers,
)
from vision_fixtures import FIXTURE_CONTROL, FIXTURE_IMPRESSION


def _synthetic_pdf(
    path: Path,
    *,
    page_size: tuple[int, int] = (100, 100),
    rectangle: tuple[int, int, int, int] = (20, 30, 10, 10),
) -> bytes:
    x, y, width, height = rectangle
    stream = f"0 0 0 rg\n{x} {y} {width} {height} re f\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_size[0]} {page_size[1]}] "
            "/Resources << >> /Contents 4 0 R >>"
        ).encode(),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, content in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode())
        document.extend(content)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    document.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:]))
    document.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(document)
    return stream


def _extraction(tmp_path: Path) -> Path:
    pdf_path = tmp_path / "part.pdf"
    _synthetic_pdf(pdf_path)
    page_png = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(page_png)
    extraction = DatasheetExtraction(
        artifact_kind="circuit_datasheet_extraction",
        pdf_path=pdf_path.name,
        pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        page_count=1,
        pages=[
            PageExtraction(
                page=1,
                width_pt=100,
                height_pt=100,
                png_path=page_png.name,
                png_sha256=hashlib.sha256(page_png.read_bytes()).hexdigest(),
                dpi=72,
                text_layer=True,
                lanes=[],
                tables_path=None,
                table_count=0,
                vector_objects=0,
                drawing_page=False,
                order_similarity=1,
            )
        ],
        tools={},
    )
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(extraction.model_dump_json(), encoding="utf-8")
    return extraction_path


def test_both_rasterizers_use_pdf_points_for_y_down_crop_geometry(tmp_path: Path) -> None:
    pdf_path = tmp_path / "square.pdf"
    _synthetic_pdf(pdf_path)
    if shutil.which("pdftoppm") is None:
        pytest.skip("pdftoppm is unavailable")
    bbox = (15.0, 55.0, 35.0, 75.0)
    outputs = (tmp_path / "pdftoppm.png", tmp_path / "pdfium.png")

    _render_pdftoppm(pdf_path, outputs[0], 1, bbox, 72)
    _render_pdfium(pdf_path, outputs[1], 1, bbox, 100, 100, 72)

    for output in outputs:
        with Image.open(output) as image:
            rgb = image.convert("RGB")
            assert rgb.size == (20, 20)
            assert rgb.getpixel((9, 9)) == (0, 0, 0)
            assert rgb.getpixel((1, 1)) == (255, 255, 255)


def test_read_batch_limits_request_count_before_loading_extraction(tmp_path: Path) -> None:
    request = VisionReadRequest(field="x", page=1, bbox=(1, 1, 2, 2), kind="view")

    for requests in ([], [request] * 8):
        with pytest.raises(VisionReadError, match=r"1\.\.7"):
            create_read_batch(
                tmp_path / "missing.json",
                requests,
                out_dir=tmp_path / f"count-{len(requests)}",
            )


def test_read_batch_selects_lane_rasterizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_rasterizers(monkeypatch)
    extraction_path = _extraction(tmp_path)
    request = [VisionReadRequest(field="x", page=1, bbox=(20, 55, 30, 65), kind="view")]

    for lane, expected in (("a", "pdftoppm"), ("b", "pdfium"), ("main", "pdftoppm")):
        batch = create_read_batch(
            extraction_path,
            request,
            out_dir=tmp_path / f"lane-{lane}",
            lane=lane,
        )
        assert {item.rasterizer for item in batch.items} == {expected}


def test_pdftoppm_uses_configured_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from circuit import visionread

    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        Image.new("RGB", (2, 2), "white").save(Path(command[-1]).with_suffix(".png"))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setenv("CIRCUIT_PDFTOPPM", "custom-pdftoppm --example-option")
    monkeypatch.setattr(visionread.subprocess, "run", run)
    output = tmp_path / "crop.png"

    _render_pdftoppm(tmp_path / "part.pdf", output, 1, (0, 0, 2, 2), 72)

    assert commands[0][:2] == ["custom-pdftoppm", "--example-option"]
    assert output.is_file()


def test_oversized_crop_is_rejected_without_tiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction_path = _extraction(tmp_path)
    extraction = DatasheetExtraction.model_validate_json(
        extraction_path.read_text(encoding="utf-8")
    )
    extraction.pages[0].width_pt = 10000
    extraction_path.write_text(extraction.model_dump_json(), encoding="utf-8")
    batch_dir = tmp_path / "oversized"

    with pytest.raises(VisionReadError, match="bbox too large"):
        create_read_batch(
            extraction_path,
            [
                VisionReadRequest(
                    field="drawing",
                    page=1,
                    bbox=(0, 10, 10000, 90),
                    kind="view",
                )
            ],
            out_dir=batch_dir,
        )

    assert not (batch_dir / "batch.json").exists()
    assert not list(batch_dir.rglob("*.png"))


def _stub_rasterizers(monkeypatch: pytest.MonkeyPatch) -> None:
    from circuit import visionread

    def render(
        _pdf: Path,
        output: Path,
        _page: int,
        bbox: tuple[float, float, float, float],
        *_args: object,
    ) -> None:
        Image.new(
            "RGB",
            (
                max(1, round(bbox[2] - bbox[0])),
                max(1, round(bbox[3] - bbox[1])),
            ),
            "white",
        ).save(output)

    monkeypatch.setattr(visionread, "_render_pdfium", render)
    monkeypatch.setattr(visionread, "_render_pdftoppm", render)

    def control_image(path: Path, size: tuple[int, int]) -> str:
        Image.new("RGB", size, "white").save(path)
        return FIXTURE_CONTROL

    monkeypatch.setattr(
        visionread,
        "_control_image",
        control_image,
    )


def _batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: VisionKind = "table",
) -> tuple[Path, VisionBatch]:
    _stub_rasterizers(monkeypatch)
    extraction_path = _extraction(tmp_path)
    batch_dir = tmp_path / "vision-batch"
    batch = create_read_batch(
        extraction_path,
        [
            VisionReadRequest(
                field="pin_table",
                page=1,
                bbox=(20, 55, 30, 65),
                kind=kind,
            )
        ],
        out_dir=batch_dir,
    )
    return batch_dir / "batch.json", batch


def _assert_control_hidden(batch_path: Path) -> None:
    payload = json.loads(batch_path.read_text(encoding="utf-8"))
    items = payload["items"]
    assert len({frozenset(item) for item in items}) == 1
    allowed_control_values = {
        "read_id",
        "image_path",
        "image_sha256",
        "bbox",
        "crop_bbox",
        "page",
    }
    assert all(
        value != "control" or key in allowed_control_values
        for item in items
        for key, value in item.items()
    )
    assert "control_salt" not in payload
    assert "control_read_sha256" not in payload
    assert "control_answer_sha256" not in payload
    assert isinstance(payload["control_state_sha256"], str)
    assert len(payload["field_bindings"]) == len(items)
    sidecar_path = (batch_path.parent / payload["control_state_path"]).resolve()
    assert hashlib.sha256(sidecar_path.read_bytes()).hexdigest() == payload["control_state_sha256"]
    assert sidecar_path.parent.name == ".vision-control"
    assert not sidecar_path.is_relative_to(batch_path.parent.resolve())
    assert set(json.loads(sidecar_path.read_text(encoding="utf-8"))) == {
        "batch_id",
        "control_salt",
        "control_read_sha256",
        "control_answer_sha256",
    }


def test_read_batch_blinds_persisted_items_and_restores_field_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    _assert_control_hidden(batch_path)
    item = next(entry for entry in batch.items if not entry.control)
    answers: dict[str, dict[str, object]] = {
        entry.read_id: {
            "answer": FIXTURE_CONTROL if entry.control else '[["1", "SW"]]',
            "impression": FIXTURE_IMPRESSION,
        }
        for entry in batch.items
    }
    record_answers(batch_path, answers)

    loaded_batch, loaded_item, answer_record = load_vision_read(
        tmp_path, f"vision-batch/batch.json#{item.read_id}"
    )

    assert loaded_batch.field_bindings == batch.field_bindings
    assert loaded_item.field == "pin_table"
    assert not loaded_item.control
    assert answer_record.batch_id == batch.batch_id


def test_answer_write_and_load_work_after_a_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    item = next(entry for entry in batch.items if not entry.control)
    answers = {
        entry.read_id: {
            "answer": FIXTURE_CONTROL if entry.control else '[["1", "SW"]]',
            "impression": FIXTURE_IMPRESSION,
        }
        for entry in batch.items
    }
    code = (
        "import json,sys; from pathlib import Path; "
        "from circuit.visionread import record_answers,load_vision_read; "
        "p=json.load(sys.stdin); record_answers(Path(p['batch_path']),p['answers']); "
        "_,item,record=load_vision_read(Path(p['spec_dir']),p['reference']); "
        "assert item.field=='pin_table' and record.control_passed"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(
            {
                "batch_path": str(batch_path),
                "spec_dir": str(tmp_path),
                "reference": f"vision-batch/batch.json#{item.read_id}",
                "answers": answers,
            }
        ),
        text=True,
        capture_output=True,
        cwd=Path(__file__).parents[1],
        check=False,
    )

    assert result.returncode == 0, result.stderr
    loaded_batch, loaded_item, answer_record = load_vision_read(
        tmp_path, f"vision-batch/batch.json#{item.read_id}"
    )
    assert loaded_batch.batch_id == batch.batch_id
    assert loaded_item.field == "pin_table"
    assert answer_record.control_passed


@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_control_state_sidecar_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    payload = json.loads(batch_path.read_text(encoding="utf-8"))
    sidecar_path = (batch_path.parent / payload["control_state_path"]).resolve()
    if failure == "missing":
        sidecar_path.unlink()
        message = "control state sidecar is missing or unreadable"
    else:
        sidecar_path.write_text(sidecar_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        message = "control state sidecar SHA-256 mismatch"

    with pytest.raises(VisionReadError, match=message):
        record_answers(
            batch_path,
            {
                entry.read_id: {
                    "answer": FIXTURE_CONTROL if entry.control else '[["1", "SW"]]',
                    "impression": FIXTURE_IMPRESSION,
                }
                for entry in batch.items
            },
        )


def test_control_state_sidecar_is_created_exclusively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    original = batch_path.read_bytes()

    with pytest.raises(VisionReadError, match="vision control state already exists"):
        _write_batch(batch_path.parent, batch)

    assert batch_path.read_bytes() == original


def test_legacy_batch_without_field_bindings_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, _batch_value = _batch(tmp_path, monkeypatch)
    payload = json.loads(batch_path.read_text(encoding="utf-8"))
    del payload["field_bindings"]
    batch_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VisionReadError, match="legacy vision batch"):
        record_answers(batch_path, {})


def test_table_prompt_and_normalization_are_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    item = next(item for item in batch.items if not item.control)
    assert item.prompt == (
        "Transcribe the table in this image as JSON: a list of rows, each row a list of cell "
        "strings in left-to-right order, including header rows. Use an empty "
        "string for an empty cell."
    )
    answers: dict[str, dict[str, object]] = {
        entry.read_id: {
            "answer": (
                '[["Pin\\u00a0 No.", " Name "], ["1", "SW\\nOUT"]]'
                if not entry.control
                else FIXTURE_CONTROL
            ),
            "impression": FIXTURE_IMPRESSION,
        }
        for entry in batch.items
    }

    record = record_answers(batch_path, answers)

    assert record.status[item.read_id] == "ok"
    assert record.normalized[item.read_id] == [["Pin No.", "Name"], ["1", "SW OUT"]]
    assert record.impressions[item.read_id] == FIXTURE_IMPRESSION


def test_pin_labels_prompt_has_no_trailing_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, batch = _batch(tmp_path, monkeypatch, kind="pin_labels")
    item = next(item for item in batch.items if not item.control)
    assert item.prompt == (
        "List every pin number visible in this pinout drawing with the signal name printed next "
        'to it, as a JSON object {"<number>": "<name>"}.'
    )


@pytest.mark.parametrize(
    "impression",
    [
        "",
        "This image is clear. " * 3,
        "The marks are clear, the image is legible, the border does not obscure text, the spacing "
        "appears regular, and no unusual glyphs or unexpected line breaks can be seen, while the "
        "surrounding table layout offers enough context for a reader to distinguish labels and "
        "values without guessing, despite the close crop around the edges.",
    ],
)
def test_answer_rejects_missing_short_or_single_sentence_impressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    impression: str,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    answers: dict[str, dict[str, object]] = {
        item.read_id: {
            "answer": FIXTURE_CONTROL if item.control else '[["1", "SW"]]',
            "impression": impression,
        }
        for item in batch.items
    }

    with pytest.raises(VisionReadError, match="read_ids"):
        record_answers(batch_path, answers)

    assert not (batch_path.parent / "answers.json").exists()


def test_missing_control_impression_rejects_whole_batch_then_allows_resubmission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    answers: dict[str, dict[str, object]] = {
        item.read_id: {
            "answer": FIXTURE_CONTROL if item.control else '[["1", "SW"]]',
            "impression": FIXTURE_IMPRESSION,
        }
        for item in batch.items
    }
    control_id = next(item.read_id for item in batch.items if item.control)
    answers[control_id] = {"answer": FIXTURE_CONTROL}

    with pytest.raises(VisionReadError, match=control_id):
        record_answers(batch_path, answers)
    assert not (batch_path.parent / "answers.json").exists()

    answers[control_id] = {
        "answer": FIXTURE_CONTROL,
        "impression": FIXTURE_IMPRESSION,
    }
    record = record_answers(batch_path, answers)

    assert record.control_passed
    assert (batch_path.parent / "answers.json").is_file()


def test_table_answer_with_wrong_shape_is_unparseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    item = next(item for item in batch.items if not item.control)
    answers: dict[str, dict[str, object]] = {
        entry.read_id: {
            "answer": FIXTURE_CONTROL if entry.control else '{"not":"rows"}',
            "impression": FIXTURE_IMPRESSION,
        }
        for entry in batch.items
    }

    record = record_answers(batch_path, answers)

    assert record.status[item.read_id] == "unparseable"


def test_comparison_batch_binds_hashes_and_requires_mirrored_control(
    tmp_path: Path,
) -> None:
    extraction_path = _extraction(tmp_path)
    composite_path = tmp_path / "comparison.png"
    Image.new("RGB", (120, 60), "white").save(composite_path, format="PNG")
    artifact_hash = "b" * 64
    spec_hash = "a" * 64
    batch = create_comparison_batch(
        extraction_path,
        composite_path,
        kind="compare_footprint",
        page=1,
        bbox=(10.0, 10.0, 90.0, 90.0),
        crop_bbox=(8.0, 8.0, 92.0, 92.0),
        dpi=300,
        rasterizer="pdftoppm",
        split_x=60,
        spec_sha256=spec_hash,
        artifact_sha256=artifact_hash,
        artifact_kind="footprint",
    )
    batch_path = tmp_path / "vision-reads" / batch.batch_id / "batch.json"
    _assert_control_hidden(batch_path)
    answers: dict[str, dict[str, object]] = {
        item.read_id: {
            "answer": (
                '{"pin1_matches":true,"arrangement_matches":false,'
                '"numbering_direction_matches":true,"differences":[]}'
                if item.control
                else '{"pin1_matches":true,"arrangement_matches":true,'
                '"numbering_direction_matches":true,"differences":[]}'
            ),
            "impression": FIXTURE_IMPRESSION,
        }
        for item in batch.items
    }
    record = record_answers(batch_path, answers)

    current, stale = find_comparison_evidence(
        tmp_path,
        kind="compare_footprint",
        spec_sha256=spec_hash,
        artifact_sha256=artifact_hash,
        artifact_kind="footprint",
    )
    wrong_hash, has_stale = find_comparison_evidence(
        tmp_path,
        kind="compare_footprint",
        spec_sha256=spec_hash,
        artifact_sha256="c" * 64,
        artifact_kind="footprint",
    )

    assert record.control_passed
    assert not stale
    assert len(current) == 1
    assert current[0].normalized is not None
    assert current[0].impression_valid
    assert current[0].item.field == "library.footprint"
    assert not current[0].item.control
    assert wrong_hash == []
    assert has_stale


def test_comparison_control_fails_when_mirror_is_not_detected(
    tmp_path: Path,
) -> None:
    extraction_path = _extraction(tmp_path)
    composite_path = tmp_path / "comparison.png"
    Image.new("RGB", (120, 60), "white").save(composite_path, format="PNG")
    batch = create_comparison_batch(
        extraction_path,
        composite_path,
        kind="compare_symbol",
        page=1,
        bbox=(10.0, 10.0, 90.0, 90.0),
        crop_bbox=(8.0, 8.0, 92.0, 92.0),
        dpi=300,
        rasterizer="pdftoppm",
        split_x=60,
        spec_sha256="a" * 64,
        artifact_sha256="b" * 64,
        artifact_kind="symbol",
    )
    batch_path = tmp_path / "vision-reads" / batch.batch_id / "batch.json"
    answers: dict[str, dict[str, object]] = {
        item.read_id: {
            "answer": (
                '{"pin1_matches":true,"arrangement_matches":true,'
                '"numbering_direction_matches":true,"differences":[]}'
            ),
            "impression": FIXTURE_IMPRESSION,
        }
        for item in batch.items
    }

    record = record_answers(batch_path, answers)

    assert not record.control_passed


def test_model_comparison_binds_render_and_uses_model_answer_shape(
    tmp_path: Path,
) -> None:
    extraction_path = _extraction(tmp_path)
    composite_path = tmp_path / "model-comparison.png"
    Image.new("RGB", (120, 60), "white").save(composite_path, format="PNG")
    spec_hash = "a" * 64
    model_hash = "b" * 64
    bindings = {
        "footprint_sha256": "c" * 64,
        "model_sha256": model_hash,
        "render_sha256": "d" * 64,
        "datasheet_view": "top",
        "left_mirrored": "false",
    }
    batch = create_comparison_batch(
        extraction_path,
        composite_path,
        kind="compare_model",
        page=1,
        bbox=(10.0, 10.0, 90.0, 90.0),
        crop_bbox=(8.0, 8.0, 92.0, 92.0),
        dpi=300,
        rasterizer="pdftoppm",
        split_x=60,
        spec_sha256=spec_hash,
        artifact_sha256=model_hash,
        artifact_kind="model3d",
        additional_bindings=bindings,
    )
    batch_path = tmp_path / "vision-reads" / batch.batch_id / "batch.json"
    _assert_control_hidden(batch_path)
    answers: dict[str, dict[str, object]] = {
        item.read_id: {
            "answer": (
                '{"pin1_marker_matches":false,"outline_matches":true,'
                '"lead_arrangement_matches":true,"differences":[]}'
                if item.control
                else '{"pin1_marker_matches":true,"outline_matches":true,'
                '"lead_arrangement_matches":true,"differences":[]}'
            ),
            "impression": FIXTURE_IMPRESSION,
        }
        for item in batch.items
    }

    record = record_answers(batch_path, answers)
    evidence, stale = find_comparison_evidence(
        tmp_path,
        kind="compare_model",
        spec_sha256=spec_hash,
        artifact_sha256=model_hash,
        artifact_kind="model3d",
        additional_bindings={
            "footprint_sha256": bindings["footprint_sha256"],
            "model_sha256": model_hash,
            "render_sha256": bindings["render_sha256"],
        },
    )

    assert record.control_passed
    assert not stale
    assert len(evidence) == 1
    assert evidence[0].normalized == {
        "pin1_marker_matches": True,
        "outline_matches": True,
        "lead_arrangement_matches": True,
        "differences": [],
    }
    assert evidence[0].item.bindings["render_sha256"] == bindings["render_sha256"]
    assert evidence[0].impression_valid


def test_successful_answer_write_is_answer_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_path, batch = _batch(tmp_path, monkeypatch)
    answers: dict[str, dict[str, object]] = {
        item.read_id: {
            "answer": FIXTURE_CONTROL if item.control else '[["1", "SW"]]',
            "impression": FIXTURE_IMPRESSION,
        }
        for item in batch.items
    }
    record_answers(batch_path, answers)
    stored = (batch_path.parent / "answers.json").read_bytes()

    with pytest.raises(VisionReadError, match="answers already exist"):
        record_answers(batch_path, answers)

    assert (batch_path.parent / "answers.json").read_bytes() == stored


def test_tool_models_forbid_extra_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        VisionReadRequest.model_validate(
            {
                "field": "x",
                "page": 1,
                "bbox": [1, 1, 2, 2],
                "kind": "table",
                "candidate_value": "not allowed",
            }
        )
    with pytest.raises(ValueError):
        VisionAnswerInput.model_validate(
            {"answer": "text", "impression": FIXTURE_IMPRESSION, "candidate_value": "no"}
        )

    batch_path, batch = _batch(tmp_path, monkeypatch)
    invalid_item = batch.items[0].model_dump(mode="json")
    invalid_item["candidate_value"] = "no"
    with pytest.raises(ValueError):
        VisionReadItem.model_validate(invalid_item)
    invalid_batch = batch.model_dump(mode="json")
    invalid_batch["candidate_value"] = "no"
    with pytest.raises(ValueError):
        VisionBatch.model_validate(invalid_batch)

    answers: dict[str, dict[str, object]] = {
        item.read_id: {
            "answer": FIXTURE_CONTROL if item.control else '[["1", "SW"]]',
            "impression": FIXTURE_IMPRESSION,
        }
        for item in batch.items
    }
    record = record_answers(batch_path, answers)
    invalid_record = record.model_dump(mode="json")
    invalid_record["candidate_value"] = "no"
    with pytest.raises(ValueError):
        VisionAnswerRecord.model_validate(invalid_record)
