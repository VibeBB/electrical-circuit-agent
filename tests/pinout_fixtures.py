from collections.abc import Mapping, Sequence
from typing import Literal

from circuit.datasheet import PdfWord
from circuit.partspec import PinoutDrawing, Reading
from circuit.pinout import PinoutGeometry, derive_pinout

Point = tuple[float, float]

QUAD16_NAMES = {
    "1": "SW",
    "2": "PGND",
    "3": "PGND",
    "4": "PVIN",
    "5": "PVIN",
    "6": "AGND",
    "7": "EN",
    "8": "FB",
    "9": "FSW",
    "10": "DEF",
    "11": "VOS",
    "12": "SW",
    "13": "SW",
    "14": "BOOT",
    "15": "NC",
    "16": "COMP",
}

DUAL8_NAMES = {
    "1": "IN",
    "2": "GND",
    "3": "EN",
    "4": "FB",
    "5": "OUT",
    "6": "SW",
    "7": "BOOT",
    "8": "VIN",
}


def fixture_word(text: str, center: Point, *, width: float = 4) -> PdfWord:
    x, y = center
    return PdfWord(text=text, x0=x - width / 2, top=y - 1, x1=x + width / 2, bottom=y + 1)


def quad16_fixture(
    names: Mapping[str, str] = QUAD16_NAMES,
) -> tuple[list[PdfWord], dict[str, Point], dict[str, str]]:
    points = [
        *((-10.0, y) for y in (-6.0, -2.0, 2.0, 6.0)),
        *((x, 10.0) for x in (-6.0, -2.0, 2.0, 6.0)),
        *((10.0, y) for y in (6.0, 2.0, -2.0, -6.0)),
        *((x, -10.0) for x in (6.0, 2.0, -2.0, -6.0)),
    ]
    if set(names) != {str(number) for number in range(1, 17)}:
        raise ValueError("quad fixture must provide names for pins 1 through 16")
    positions = {str(number): point for number, point in enumerate(points, start=1)}
    selected_names = dict(names)
    words: list[PdfWord] = []
    for number, point in positions.items():
        x, y = point
        words.append(fixture_word(number, point, width=1))
        if abs(x) >= abs(y):
            name_point = (x + (-6 if x < 0 else 6), y)
        elif y < 0:
            name_point = (x, y - 6)
        else:
            name_point = (x, y + 6)
        words.append(fixture_word(selected_names[number], name_point, width=1.5))
    return words, positions, selected_names


def dual_fixture(
    names: Mapping[str, str] | Sequence[str],
) -> tuple[list[PdfWord], dict[str, Point], dict[str, str]]:
    selected_names = (
        dict(names)
        if isinstance(names, Mapping)
        else {str(number): name for number, name in enumerate(names, start=1)}
    )
    pin_count = len(selected_names)
    if not pin_count or set(selected_names) != {str(number) for number in range(1, pin_count + 1)}:
        raise ValueError("dual fixture must provide consecutive pin names starting at 1")
    left_count = (pin_count + 1) // 2
    right_count = pin_count - left_count

    def row_positions(count: int) -> list[float]:
        return [2 * (index - (count - 1) / 2) for index in range(count)]

    positions: dict[str, Point] = {}
    for number, y in enumerate(row_positions(left_count), start=1):
        positions[str(number)] = (-8.0, y)
    for offset, y in enumerate(reversed(row_positions(right_count)), start=1):
        positions[str(left_count + offset)] = (8.0, y)
    words: list[PdfWord] = []
    for number, point in positions.items():
        x, y = point
        words.append(fixture_word(number, point, width=1))
        words.append(fixture_word(selected_names[number], (x - 5 if x < 0 else x + 5, y)))
    return words, positions, selected_names


def dual8_fixture() -> tuple[list[PdfWord], dict[str, Point], dict[str, str]]:
    return dual_fixture(DUAL8_NAMES)


def geometry_for_names(
    names: Mapping[str, str],
    *,
    pin_count: int,
    topology: Literal["quad", "dual"] | None = None,
    view: Literal["top", "bottom"] = "top",
    page: int = 1,
) -> PinoutGeometry:
    selected_topology = topology or ("quad" if pin_count == 16 else "dual")
    words, _, _ = quad16_fixture(names) if selected_topology == "quad" else dual_fixture(names)
    geometry, issues = derive_pinout(words, view=view, pin_count=pin_count, page=page)
    if geometry is None or issues:
        raise ValueError(f"fixture pinout could not be derived: {[issue.code for issue in issues]}")
    return geometry


def pinout_drawing(
    names: Mapping[str, str],
    *,
    page: int = 1,
    view: Literal["top", "bottom"] = "top",
    bbox: tuple[float, float, float, float] = (10, 10, 90, 90),
    vision_record: str = "vision.json",
) -> PinoutDrawing:
    view_text = f"{view.title()} View"
    return PinoutDrawing(
        page=page,
        bbox=bbox,
        view=view,
        view_reading=Reading(
            page=page,
            bbox=(10, 10, 30, 20),
            mechanical=view_text,
            vision=view_text,
            vision_record=vision_record,
        ),
        labels_vision=dict(names),
        vision_record=vision_record,
    )
