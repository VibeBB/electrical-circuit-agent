"""Create hash-bound datasheet crops for vision-capable authoring agents."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .advisory import impression_is_prose
from .datasheet import DatasheetExtraction, load_extraction

VisionKind = Literal["transcribe", "view", "pin1_corner", "pin_labels", "table"]
Rasterizer = Literal["pdftoppm", "pdfium"]
BBox = tuple[float, float, float, float]

_PROMPTS: dict[VisionKind, str] = {
    "transcribe": (
        "Transcribe all text and numbers visible in this image exactly, including symbols such as "
        "±, □, ⌀ and units. Answer with the text only."
    ),
    "view": (
        "Is the drawing in this image labeled as a top view or a bottom view? "
        "Answer exactly 'top' or 'bottom'."
    ),
    "pin1_corner": (
        "Which corner of the package holds pin 1 in this drawing? Answer one of: top_left, "
        "top_right, bottom_left, bottom_right."
    ),
    "pin_labels": (
        "List every pin number visible in this pinout drawing with the signal "
        "name printed next to it, "
        'as a JSON object {"<number>": "<name>"}.'
    ),
    "table": (
        "Transcribe the table in this image as JSON: a list of rows, each row "
        "a list of cell strings in left-to-right order, including header rows. "
        "Use an empty string for an empty cell."
    ),
}


def prompt_for_kind(kind: VisionKind) -> str:
    return _PROMPTS[kind]


_CONTROL_ALPHABET = "ACDEFHJKLMNPRTUVWXY34679"
_PDFTOPPM_ENV = "CIRCUIT_PDFTOPPM"


class VisionReadError(ValueError):
    """Raised when a vision read cannot be created or loaded safely."""


class VisionReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    page: int = Field(ge=1)
    bbox: BBox
    kind: VisionKind

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: BBox) -> BBox:
        x0, y0, x1, y1 = value
        if not all(math.isfinite(coord) for coord in value) or x0 >= x1 or y0 >= y1:
            raise ValueError("bbox must be a finite, non-empty rectangle")
        return value


class VisionReadItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read_id: str
    field: str
    kind: VisionKind
    page: int
    bbox: BBox
    crop_bbox: BBox
    dpi: int
    rasterizer: Rasterizer
    image_path: str
    image_sha256: str
    prompt: str
    prompt_sha256: str
    control: bool = False


class VisionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_vision_read_batch"]
    batch_id: str
    created_at: str
    lane: str
    profile: str
    model: str
    pdf_path: str
    pdf_sha256: str
    items: list[VisionReadItem]
    control_salt: str
    control_answer_sha256: str
    control_read_sha256: str


class VisionAnswerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_vision_read_answers"]
    batch_id: str
    answered_at: str
    answers: dict[str, str]
    impressions: dict[str, str] = Field(default_factory=dict)
    normalized: dict[str, str | dict[str, str] | list[list[str]]]
    status: dict[str, Literal["ok", "unparseable"]]
    control_passed: bool


class VisionAnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    impression: str


@dataclass(frozen=True)
class LoadedVisionRead:
    batch: VisionBatch
    item: VisionReadItem
    answers: VisionAnswerRecord
    answer: str
    normalized: str | dict[str, str] | list[list[str]]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        if exclusive:
            os.link(temp_path, path)
            temp_path.unlink()
        else:
            os.replace(temp_path, path)
    except FileExistsError as exc:
        temp_path.unlink(missing_ok=True)
        raise VisionReadError(f"answers already exist: {path}") from exc
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise


def _request(value: VisionReadRequest | dict[str, object]) -> VisionReadRequest:
    if isinstance(value, VisionReadRequest):
        return value
    return VisionReadRequest.model_validate(value)


def _extraction_pdf(extraction: DatasheetExtraction, extraction_path: Path) -> Path:
    path = Path(extraction.pdf_path)
    return path if path.is_absolute() else extraction_path.resolve().parent / path


def _padded_bbox(bbox: BBox, width: float, height: float) -> BBox:
    x0, y0, x1, y1 = bbox
    return (
        max(0.0, x0 - 4.0),
        max(0.0, y0 - 4.0),
        min(width, x1 + 4.0),
        min(height, y1 + 4.0),
    )


def _dpi(bbox: BBox) -> int:
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    return min(1200, max(300, math.ceil(600 * 72 / min(width, height))))


def _render_pdftoppm(
    pdf_path: Path,
    output: Path,
    page: int,
    bbox: BBox,
    dpi: int,
) -> None:
    scale = dpi / 72
    x0, y0, x1, y1 = bbox
    x = math.floor(x0 * scale)
    y = math.floor(y0 * scale)
    width = math.ceil(x1 * scale) - x
    height = math.ceil(y1 * scale) - y
    prefix = output.with_suffix("")
    command = [
        *shlex.split(os.environ.get(_PDFTOPPM_ENV, "pdftoppm")),
        "-f",
        str(page),
        "-l",
        str(page),
        "-r",
        str(dpi),
        "-x",
        str(x),
        "-y",
        str(y),
        "-W",
        str(width),
        "-H",
        str(height),
        "-png",
        "-singlefile",
        str(pdf_path),
        str(prefix),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VisionReadError(f"{command[0]} is not available; install poppler-utils") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VisionReadError(f"pdftoppm failed: {exc}") from exc
    if result.returncode:
        raise VisionReadError(result.stderr.strip() or "pdftoppm failed")
    produced = prefix.with_suffix(".png")
    if produced != output:
        produced.replace(output)
    if not output.is_file():
        raise VisionReadError("pdftoppm produced no crop image")


def _render_pdfium(
    pdf_path: Path,
    output: Path,
    page_number: int,
    bbox: BBox,
    page_width: float,
    page_height: float,
    dpi: int,
) -> None:
    x0, y0, x1, y1 = bbox
    crop = (x0, page_height - y1, page_width - x1, y0)
    try:
        with pdfium.PdfDocument(str(pdf_path)) as document:
            page = cast(Any, document[page_number - 1])
            bitmap = page.render(scale=dpi / 72, crop=crop)
            bitmap.to_pil().save(output, format="PNG")
    except (OSError, ValueError, IndexError, RuntimeError) as exc:
        raise VisionReadError(f"pdfium crop failed: {exc}") from exc


def _control_image(path: Path, size: tuple[int, int]) -> str:
    answer = "".join(secrets.choice(_CONTROL_ALPHABET) for _ in range(6))
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    box = draw.textbbox((0, 0), answer, font=font)
    x = max(0, (size[0] - (box[2] - box[0])) // 2)
    y = max(0, (size[1] - (box[3] - box[1])) // 2)
    draw.text((x, y), answer, fill="black", font=font)
    for _ in range(4):
        points = (
            secrets.randbelow(max(size[0], 1)),
            secrets.randbelow(max(size[1], 1)),
            secrets.randbelow(max(size[0], 1)),
            secrets.randbelow(max(size[1], 1)),
        )
        draw.line(points, fill=(160, 160, 160), width=1)
    image.save(path, format="PNG")
    return answer


def _batch_payload(batch: VisionBatch) -> dict[str, object]:
    return cast(
        dict[str, object],
        batch.model_dump(mode="json", exclude={"items": {"__all__": {"control"}}}),
    )


def create_read_batch(
    extraction_path: Path,
    requests: Sequence[VisionReadRequest | dict[str, object]],
    out_dir: Path | None = None,
    lane: str = "main",
    profile: str = "",
    model: str = "unknown",
) -> VisionBatch:
    if not 1 <= len(requests) <= 7:
        raise VisionReadError("requests must contain 1..7 items")
    normalized_requests = [_request(item) for item in requests]
    extraction = load_extraction(extraction_path)
    pdf_path = _extraction_pdf(extraction, extraction_path)
    if not pdf_path.is_file():
        raise VisionReadError(f"datasheet PDF is missing: {pdf_path}")
    pdf_sha256 = _sha256(pdf_path.read_bytes())
    if pdf_sha256 != extraction.pdf_sha256:
        raise VisionReadError("datasheet PDF SHA-256 differs from extraction")

    pages = {page.page: page for page in extraction.pages}
    batch_id = secrets.token_hex(6)
    batch_dir = (
        out_dir
        if out_dir is not None
        else extraction_path.resolve().parent / "vision-reads" / batch_id
    )
    batch_dir.mkdir(parents=True, exist_ok=False)
    items: list[VisionReadItem] = []
    first_crop_size: tuple[int, int] | None = None
    rasterizer: Rasterizer = "pdfium" if lane == "b" else "pdftoppm"
    for request in normalized_requests:
        page = pages.get(request.page)
        if page is None:
            raise VisionReadError(f"page {request.page} is not present in the extraction")
        crop_bbox = _padded_bbox(request.bbox, page.width_pt, page.height_pt)
        crop_width = crop_bbox[2] - crop_bbox[0]
        crop_height = crop_bbox[3] - crop_bbox[1]
        dpi = _dpi(crop_bbox)
        if max(crop_width, crop_height) * dpi / 72 > 2400:
            raise VisionReadError("bbox too large; split the region")
        read_id = secrets.token_hex(6)
        image_relative = f"images/{read_id}.png"
        image_path = batch_dir / image_relative
        image_path.parent.mkdir(parents=True, exist_ok=True)
        if rasterizer == "pdfium":
            _render_pdfium(
                pdf_path,
                image_path,
                request.page,
                crop_bbox,
                page.width_pt,
                page.height_pt,
                dpi,
            )
        else:
            _render_pdftoppm(pdf_path, image_path, request.page, crop_bbox, dpi)
        with Image.open(image_path) as image:
            if first_crop_size is None:
                first_crop_size = image.size
        prompt = _PROMPTS[request.kind]
        items.append(
            VisionReadItem(
                read_id=read_id,
                field=request.field,
                kind=request.kind,
                page=request.page,
                bbox=request.bbox,
                crop_bbox=crop_bbox,
                dpi=dpi,
                rasterizer=rasterizer,
                image_path=image_relative,
                image_sha256=_sha256(image_path.read_bytes()),
                prompt=prompt,
                prompt_sha256=_sha256(prompt.encode("utf-8")),
            )
        )
    assert first_crop_size is not None
    control_read_id = secrets.token_hex(6)
    control_path = batch_dir / "images" / f"{control_read_id}.png"
    answer = _control_image(control_path, first_crop_size)
    control_prompt = _PROMPTS["transcribe"]
    items.append(
        VisionReadItem(
            read_id=control_read_id,
            field="control",
            kind="transcribe",
            page=normalized_requests[0].page,
            bbox=(0.0, 0.0, 1.0, 1.0),
            crop_bbox=(0.0, 0.0, 1.0, 1.0),
            dpi=300,
            rasterizer=rasterizer,
            image_path=control_path.relative_to(batch_dir).as_posix(),
            image_sha256=_sha256(control_path.read_bytes()),
            prompt=control_prompt,
            prompt_sha256=_sha256(control_prompt.encode("utf-8")),
            control=True,
        )
    )
    secrets.SystemRandom().shuffle(items)
    control_salt = secrets.token_hex(16)
    normalized_control_answer = re.sub(r"\s+", "", answer).casefold()
    batch = VisionBatch(
        artifact_kind="circuit_vision_read_batch",
        batch_id=batch_id,
        created_at=datetime.now(UTC).isoformat(),
        lane=lane,
        profile=profile,
        model=model,
        pdf_path=str(pdf_path.resolve()),
        pdf_sha256=pdf_sha256,
        items=items,
        control_salt=control_salt,
        control_answer_sha256=_sha256(f"{control_salt}{normalized_control_answer}".encode()),
        control_read_sha256=_sha256(f"{control_salt}{control_read_id}".encode()),
    )
    _atomic_json(batch_dir / "batch.json", _batch_payload(batch))
    return batch


def _load_batch(batch_path: Path) -> VisionBatch:
    try:
        raw = json.loads(batch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VisionReadError(f"vision batch is unreadable: {exc}") from exc
    try:
        batch = VisionBatch.model_validate(raw)
    except ValueError as exc:
        raise VisionReadError(f"vision batch is invalid: {exc}") from exc
    items = [
        item.model_copy(
            update={
                "control": _sha256(f"{batch.control_salt}{item.read_id}".encode())
                == batch.control_read_sha256
            }
        )
        for item in batch.items
    ]
    if sum(item.control for item in items) != 1:
        raise VisionReadError("vision batch must identify exactly one control read")
    return batch.model_copy(update={"items": items})


def _normalize_answer(
    item: VisionReadItem, answer: str
) -> tuple[str | dict[str, str] | list[list[str]], Literal["ok", "unparseable"]]:
    if item.kind == "transcribe":
        normalized = unicodedata.normalize("NFKC", answer)
        return re.sub(r"\s+", " ", normalized).strip(), "ok"
    if item.kind in ("view", "pin1_corner"):
        return re.sub(r"[\s-]+", "_", answer.casefold().strip()), "ok"
    try:
        parsed = json.loads(answer)
    except json.JSONDecodeError:
        return answer, "unparseable"
    if item.kind == "table":
        if not isinstance(parsed, list):
            return answer, "unparseable"
        normalized_rows: list[list[str]] = []
        for value in cast(list[object], parsed):
            if not isinstance(value, list):
                return answer, "unparseable"
            row = cast(list[object], value)
            normalized_row: list[str] = []
            for cell in row:
                if not isinstance(cell, str):
                    return answer, "unparseable"
                normalized_row.append(
                    re.sub(r"\s+", " ", unicodedata.normalize("NFKC", cell)).strip()
                )
            normalized_rows.append(normalized_row)
        return normalized_rows, "ok"
    if not isinstance(parsed, dict):
        return answer, "unparseable"
    normalized_labels: dict[str, str] = {}
    for key, value in cast(dict[object, object], parsed).items():
        if not isinstance(key, str) or not isinstance(value, str):
            return answer, "unparseable"
        normalized_labels[key] = value
    return normalized_labels, "ok"


def record_answers(
    batch_path: Path,
    answers: Mapping[str, str | VisionAnswerInput | dict[str, object]],
) -> VisionAnswerRecord:
    batch = _load_batch(batch_path)
    read_ids = {item.read_id for item in batch.items}
    if set(answers) != read_ids:
        raise VisionReadError("answers must cover exactly all read IDs")
    parsed_answers: dict[str, VisionAnswerInput] = {}
    invalid_impressions: list[str] = []
    for read_id, value in answers.items():
        try:
            if isinstance(value, str):
                raise ValueError("answer and impression are required")
            parsed = (
                value
                if isinstance(value, VisionAnswerInput)
                else VisionAnswerInput.model_validate(value)
            )
            impression_is_prose(parsed.impression)
        except (ValueError, TypeError):
            invalid_impressions.append(read_id)
        else:
            parsed_answers[read_id] = parsed
    if invalid_impressions:
        raise VisionReadError(
            "missing or invalid impression for read_ids: " + ", ".join(sorted(invalid_impressions))
        )
    answer_texts = {read_id: value.answer for read_id, value in parsed_answers.items()}
    impressions = {read_id: value.impression for read_id, value in parsed_answers.items()}
    answers_path = batch_path.parent / "answers.json"
    if answers_path.exists():
        raise VisionReadError(f"answers already exist: {answers_path}")
    normalized: dict[str, str | dict[str, str] | list[list[str]]] = {}
    status: dict[str, Literal["ok", "unparseable"]] = {}
    control_passed = False
    for item in batch.items:
        value, state = _normalize_answer(item, answer_texts[item.read_id])
        normalized[item.read_id] = value
        status[item.read_id] = state
        if item.control:
            control_answer = re.sub(r"\s+", "", answer_texts[item.read_id]).casefold()
            control_passed = (
                state == "ok"
                and _sha256(f"{batch.control_salt}{control_answer}".encode())
                == batch.control_answer_sha256
            )
    record = VisionAnswerRecord(
        artifact_kind="circuit_vision_read_answers",
        batch_id=batch.batch_id,
        answered_at=datetime.now(UTC).isoformat(),
        answers=answer_texts,
        impressions=impressions,
        normalized=normalized,
        status=status,
        control_passed=control_passed,
    )
    _atomic_json(answers_path, record.model_dump(mode="json"), exclusive=True)
    return record


def load_vision_read(
    spec_dir: Path, ref: str
) -> tuple[VisionBatch, VisionReadItem, VisionAnswerRecord]:
    batch_ref, separator, read_id = ref.rpartition("#")
    if not separator or not batch_ref or not read_id:
        raise VisionReadError("vision_read must be '<relative batch.json path>#<read_id>'")
    relative = Path(batch_ref)
    if relative.is_absolute():
        raise VisionReadError("vision_read batch path must be relative to the spec directory")
    spec_root = spec_dir.resolve()
    batch_path = (spec_root / relative).resolve()
    if not batch_path.is_relative_to(spec_root):
        raise VisionReadError("vision_read batch path escapes the spec directory")
    batch = _load_batch(batch_path)
    item = next((entry for entry in batch.items if entry.read_id == read_id), None)
    if item is None:
        raise VisionReadError(f"unknown vision read ID: {read_id}")
    image_relative = Path(item.image_path)
    image_path = (batch_path.parent / image_relative).resolve()
    if image_relative.is_absolute() or not image_path.is_relative_to(batch_path.parent.resolve()):
        raise VisionReadError("vision-read image path escapes its batch directory")
    try:
        image_sha = _sha256(image_path.read_bytes())
    except OSError as exc:
        raise VisionReadError(f"vision-read image is unavailable: {exc}") from exc
    if image_sha != item.image_sha256:
        raise VisionReadError("vision-read image SHA-256 mismatch")
    try:
        raw_answers = json.loads((batch_path.parent / "answers.json").read_text(encoding="utf-8"))
        answer_record = VisionAnswerRecord.model_validate(raw_answers)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise VisionReadError(f"vision answers are unavailable or invalid: {exc}") from exc
    if answer_record.batch_id != batch.batch_id:
        raise VisionReadError("vision answers refer to a different batch")
    if read_id not in answer_record.answers or read_id not in answer_record.normalized:
        raise VisionReadError(f"vision answer is missing for read ID: {read_id}")
    return (
        batch,
        item,
        answer_record,
    )
