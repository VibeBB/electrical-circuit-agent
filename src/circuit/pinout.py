"""Deterministic geometry and name checks for datasheet pinout drawings."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .datasheet import PdfWord

Point = tuple[float, float]
Side = Literal["left", "right", "top", "bottom"]
Winding = Literal["ccw", "cw"]


class PinoutLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    name: str | None
    x: float
    y: float
    page_x: float
    page_y: float


class PinoutGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    view: Literal["top", "bottom"]
    winding: Winding | None
    labels: list[PinoutLabel]


class PinoutIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning", "info"]
    message: str


def _center(word: PdfWord) -> Point:
    return ((word.x0 + word.x1) / 2, (word.top + word.bottom) / 2)


def _height(word: PdfWord) -> float:
    return max(0.0, word.bottom - word.top)


def _phrase_merge(tokens: Sequence[PdfWord]) -> list[PdfWord]:
    horizontal = sorted(
        (word for word in tokens if word.x1 - word.x0 >= _height(word)),
        key=lambda word: (_center(word)[1], word.x0),
    )
    other = [word for word in tokens if word.x1 - word.x0 < _height(word)]
    lines: list[list[PdfWord]] = []
    for word in horizontal:
        center_y = _center(word)[1]
        matching = [
            line
            for line in lines
            if abs(median(_center(item)[1] for item in line) - center_y)
            <= 0.3 * max(_height(word), median(_height(item) for item in line))
        ]
        if matching:
            min(
                matching,
                key=lambda line: abs(median(_center(item)[1] for item in line) - center_y),
            ).append(word)
        else:
            lines.append([word])

    merged = list(other)
    for line in lines:
        line.sort(key=lambda word: word.x0)
        current: PdfWord | None = None
        for word in line:
            if current is None:
                current = word
                continue
            gap = word.x0 - current.x1
            center_delta = abs(_center(word)[1] - _center(current)[1])
            tolerance = 0.3 * max(_height(word), _height(current))
            if gap <= 2.0 and center_delta <= tolerance:
                current = PdfWord(
                    text=f"{current.text} {word.text}",
                    x0=min(current.x0, word.x0),
                    top=min(current.top, word.top),
                    x1=max(current.x1, word.x1),
                    bottom=max(current.bottom, word.bottom),
                )
            else:
                merged.append(current)
                current = word
        if current is not None:
            merged.append(current)
    return sorted(merged, key=lambda word: (word.top, word.x0))


def normalized(points: dict[str, Point]) -> dict[str, Point]:
    if not points:
        return {}
    center_x = sum(point[0] for point in points.values()) / len(points)
    center_y = sum(point[1] for point in points.values()) / len(points)
    half_x = max((abs(point[0] - center_x) for point in points.values()), default=0.0)
    half_y = max((abs(point[1] - center_y) for point in points.values()), default=0.0)
    return {
        number: (
            (point[0] - center_x) / half_x if half_x else 0.0,
            (point[1] - center_y) / half_y if half_y else 0.0,
        )
        for number, point in points.items()
    }


def winding(points: Sequence[Point]) -> Winding | None:
    if len(points) < 3:
        return None
    area = 0.5 * sum(
        x * next_y - next_x * y
        for (x, y), (next_x, next_y) in zip(points, (*points[1:], points[0]), strict=True)
    )
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    bbox_area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    if bbox_area <= 0 or abs(area) < 0.01 * bbox_area:
        return None
    return "ccw" if area < 0 else "cw"


def _number_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value.casefold())


def _dot(left: Point, right: Point) -> float:
    return left[0] * right[0] + left[1] * right[1]


def _cross(left: Point, right: Point) -> float:
    return left[0] * right[1] - left[1] * right[0]


def _outward(side: Side) -> Point:
    return {
        "left": (-1.0, 0.0),
        "right": (1.0, 0.0),
        "top": (0.0, -1.0),
        "bottom": (0.0, 1.0),
    }[side]


def _find_name(
    label: Point,
    outward: Point,
    pitch: float,
    tokens: Sequence[PdfWord],
    label_centers: dict[str, Point],
    own_number: str,
    *,
    inward: bool = False,
) -> tuple[PdfWord | None, bool]:
    candidates: list[tuple[float, PdfWord]] = []
    for token in tokens:
        text = token.text.strip()
        if not text or re.fullmatch(r"\d+", text):
            continue
        token_center = _center(token)
        vector = (token_center[0] - label[0], token_center[1] - label[1])
        along = _dot(vector, outward)
        if along <= 0 or abs(_cross(vector, outward)) > 0.6 * pitch:
            continue
        if inward and any(
            number != own_number
            and math.dist(token_center, center) < math.dist(token_center, label)
            for number, center in label_centers.items()
        ):
            continue
        candidates.append((along, token))
    candidates.sort(key=lambda item: (item[0], item[1].x0, item[1].top))
    if not candidates:
        return None, False
    ambiguous = len(candidates) > 1 and candidates[1][0] - candidates[0][0] <= 1.0
    return candidates[0][1], ambiguous


def derive_pinout(
    tokens: Sequence[PdfWord],
    *,
    view: Literal["top", "bottom"],
    pin_count: int,
    page: int,
) -> tuple[PinoutGeometry | None, list[PinoutIssue]]:
    words = _phrase_merge(tokens)
    labels_by_number: dict[str, PdfWord] = {}
    number_counts: Counter[str] = Counter()
    for word in words:
        text = word.text.strip()
        if not re.fullmatch(r"\d+", text):
            continue
        number = int(text)
        if 1 <= number <= pin_count:
            key = str(number)
            number_counts[key] += 1
            labels_by_number.setdefault(key, word)

    issues: list[PinoutIssue] = []
    missing = [str(number) for number in range(1, pin_count + 1) if not number_counts[str(number)]]
    duplicates = sorted(
        (number for number, count in number_counts.items() if count > 1),
        key=_number_key,
    )
    for number in missing:
        issues.append(
            PinoutIssue(
                code="pinout_number_missing",
                severity="error",
                message=f"pinout number {number} is missing",
            )
        )
    for number in duplicates:
        issues.append(
            PinoutIssue(
                code="pinout_number_duplicate",
                severity="error",
                message=f"pinout number {number} occurs {number_counts[number]} times",
            )
        )
    if missing or duplicates:
        return None, issues

    page_centers = {number: _center(word) for number, word in labels_by_number.items()}
    center = (
        sum(point[0] for point in page_centers.values()) / pin_count,
        sum(point[1] for point in page_centers.values()) / pin_count,
    )
    half_x = max(abs(point[0] - center[0]) for point in page_centers.values())
    half_y = max(abs(point[1] - center[1]) for point in page_centers.values())
    sides: dict[str, Side] = {}
    for number, point in page_centers.items():
        dx, dy = point[0] - center[0], point[1] - center[1]
        horizontal_score = abs(dx) / half_x if half_x else 0.0
        vertical_score = abs(dy) / half_y if half_y else 0.0
        if horizontal_score >= vertical_score:
            sides[number] = "left" if dx < 0 else "right"
        else:
            sides[number] = "top" if dy < 0 else "bottom"

    pitches = [
        math.dist(page_centers[str(number)], page_centers[str(number + 1)])
        for number in range(1, pin_count)
        if sides[str(number)] == sides[str(number + 1)]
    ]
    pitch = (
        median(pitches)
        if pitches
        else 1.5 * median(_height(word) for word in labels_by_number.values())
    )
    pitch = max(pitch, 1e-6)
    non_number_tokens = [word for word in words if not re.fullmatch(r"\d+", word.text.strip())]
    names: dict[str, str | None] = {}
    label_centers = page_centers
    for number in sorted(page_centers, key=_number_key):
        outward = _outward(sides[number])
        candidate, ambiguous = _find_name(
            page_centers[number],
            outward,
            pitch,
            non_number_tokens,
            label_centers,
            number,
        )
        if ambiguous:
            issues.append(
                PinoutIssue(
                    code="pinout_name_ambiguous",
                    severity="error",
                    message=f"pinout name for pin {number} has two equally near candidates",
                )
            )
        if candidate is None:
            inward = (-outward[0], -outward[1])
            candidate, ambiguous = _find_name(
                page_centers[number],
                inward,
                pitch,
                non_number_tokens,
                label_centers,
                number,
                inward=True,
            )
            if ambiguous:
                issues.append(
                    PinoutIssue(
                        code="pinout_name_ambiguous",
                        severity="error",
                        message=f"pinout name for pin {number} has two equally near candidates",
                    )
                )
        if candidate is None:
            issues.append(
                PinoutIssue(
                    code="pinout_name_unresolved",
                    severity="error",
                    message=f"pinout name for pin {number} could not be resolved",
                )
            )
            names[number] = None
        else:
            names[number] = candidate.text.strip()

    top_view_centers = {
        number: (
            2 * center[0] - point[0] if view == "bottom" else point[0],
            point[1],
        )
        for number, point in page_centers.items()
    }
    normalized_centers = normalized(top_view_centers)
    labels = [
        PinoutLabel(
            number=number,
            name=names[number],
            x=normalized_centers[number][0],
            y=normalized_centers[number][1],
            page_x=page_centers[number][0],
            page_y=page_centers[number][1],
        )
        for number in sorted(page_centers, key=_number_key)
    ]
    ordered_centers = [top_view_centers[str(number)] for number in range(1, pin_count + 1)]
    return (
        PinoutGeometry(
            page=page,
            view=view,
            winding=winding(ordered_centers),
            labels=labels,
        ),
        issues,
    )


def _ordered_points(points: dict[str, Point]) -> tuple[list[str], list[Point]]:
    numbers = sorted(points, key=_number_key)
    return numbers, [points[number] for number in numbers]


def _angle(point: Point) -> float:
    return math.atan2(point[1], point[0])


def _angular_delta(left: float, right: float) -> float:
    return abs((left - right + math.pi) % (2 * math.pi) - math.pi)


def compare_orientation(
    reference: dict[str, Point],
    candidate: dict[str, Point],
) -> list[PinoutIssue]:
    common = {number: reference[number] for number in reference.keys() & candidate.keys()}
    candidate_common = {number: candidate[number] for number in common}
    if "1" not in common or len(common) < 3:
        return []
    reference_normalized = normalized(common)
    candidate_normalized = normalized(candidate_common)
    _, reference_points = _ordered_points(reference_normalized)
    _, candidate_points = _ordered_points(candidate_normalized)
    reference_winding = winding(reference_points)
    candidate_winding = winding(candidate_points)
    if (
        reference_winding is not None
        and candidate_winding is not None
        and reference_winding != candidate_winding
    ):
        return [
            PinoutIssue(
                code="chirality_mismatch",
                severity="error",
                message="candidate pin order has opposite winding from the pinout drawing",
            )
        ]
    rotation_mismatch = _angular_delta(
        _angle(reference_normalized["1"]),
        _angle(candidate_normalized["1"]),
    ) > math.radians(45)
    if rotation_mismatch:
        return [
            PinoutIssue(
                code="rotation_mismatch",
                severity="error",
                message="candidate pin 1 is rotated more than 45 degrees from the pinout drawing",
            )
        ]

    def cyclic_order(points: dict[str, Point]) -> list[str]:
        ordered = sorted(points, key=lambda number: (_angle(points[number]), _number_key(number)))
        one_index = ordered.index("1")
        return ordered[one_index:] + ordered[:one_index]

    reference_order = cyclic_order(reference_normalized)
    candidate_order = cyclic_order(candidate_normalized)
    if reference_order != candidate_order:
        first_mismatch = next(
            index
            for index, (expected, actual) in enumerate(
                zip(reference_order, candidate_order, strict=True)
            )
            if expected != actual
        )
        return [
            PinoutIssue(
                code="order_mismatch",
                severity="error",
                message=(
                    f"pin order differs at position {first_mismatch + 1}: "
                    f"drawing {reference_order[first_mismatch]}, candidate "
                    f"{candidate_order[first_mismatch]}"
                ),
            )
        ]
    return []


def names_equal(a: str, b: str) -> bool:
    return _normalized_name(a) == _normalized_name(b)


def _normalized_name(value: str) -> str:
    value = re.sub(r"~\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"[\s_]", "", value)
    return value.strip("#*").casefold()


def diagnose_permutation(
    positions: dict[str, Point],
    expected: dict[str, str],
    actual: dict[str, str],
) -> list[str]:
    if not positions:
        return []
    numbers = sorted(positions, key=_number_key)
    normalized_positions = normalized(positions)
    name_counts = Counter(_normalized_name(value) for value in expected.values())
    unique_numbers = [
        number
        for number in numbers
        if number in expected
        and expected[number]
        and name_counts[_normalized_name(expected[number])] == 1
    ]
    if not unique_numbers:
        return []

    def score(permutation: dict[str, str]) -> float:
        matches = sum(
            number in actual
            and permutation.get(number) in expected
            and names_equal(actual[number], expected[permutation[number]])
            for number in unique_numbers
        )
        return matches / len(unique_numbers)

    identity = {number: number for number in numbers}
    identity_score = score(identity)
    candidates: dict[str, float] = {}
    count = len(numbers)
    for shift in range(1, 4):
        for direction, sign in (("+", 1), ("-", -1)):
            candidates[f"shift{direction}{shift}"] = score(
                {
                    number: str((int(number) - 1 + sign * shift) % count + 1)
                    for number in numbers
                    if number.isdigit()
                }
            )

    transforms: dict[str, Callable[[float, float], Point]] = {
        "mirror_x": lambda x, y: (-x, y),
        "mirror_y": lambda x, y: (x, -y),
        "rotate_90": lambda x, y: (-y, x),
        "rotate_180": lambda x, y: (-x, -y),
        "rotate_270": lambda x, y: (y, -x),
        "transpose": lambda x, y: (y, x),
        "antitranspose": lambda x, y: (-y, -x),
    }
    for name, transform in transforms.items():
        permutation: dict[str, str] = {}
        for number, (x, y) in normalized_positions.items():
            target = transform(x, y)
            nearest = min(
                numbers,
                key=lambda candidate: (
                    math.dist(target, normalized_positions[candidate]),
                    _number_key(candidate),
                ),
            )
            permutation[number] = nearest
        if len(set(permutation.values())) == count:
            candidates[name] = score(permutation)
    return [
        name
        for name, value in sorted(candidates.items(), key=lambda item: (-item[1], item[0]))
        if value >= 0.9 and value > identity_score
    ]
