import math
from collections import Counter
from typing import Literal

from circuit.datasheet import PdfWord
from circuit.pinout import (
    compare_orientation,
    derive_pinout,
    diagnose_permutation,
    names_equal,
    normalized,
)
from pinout_fixtures import (
    DUAL8_NAMES,
    QUAD16_NAMES,
    dual8_fixture,
    fixture_word,
    quad16_fixture,
)


def _derived(
    words: list[PdfWord],
    *,
    view: Literal["top", "bottom"] = "top",
    pin_count: int = 16,
):
    geometry, issues = derive_pinout(
        words,
        view=view,
        pin_count=pin_count,
        page=3,
    )
    return geometry, [issue.code for issue in issues]


def test_reusable_quad_and_dual_fixtures_match_the_reference_names() -> None:
    _, _, quad_names = quad16_fixture()
    _, _, dual_names = dual8_fixture()

    assert quad_names == QUAD16_NAMES
    assert Counter(quad_names.values())["SW"] == 3
    assert Counter(quad_names.values())["PVIN"] == 2
    assert Counter(quad_names.values())["PGND"] == 2
    assert dual_names == DUAL8_NAMES


def test_quad_top_view_resolves_names_and_ccw_winding() -> None:
    words, _, names = quad16_fixture()
    geometry, issue_codes = _derived(words)

    assert geometry is not None
    assert issue_codes == []
    assert geometry.winding == "ccw"
    assert geometry.labels[0].number == "1"
    assert geometry.labels[0].name == "SW"
    assert geometry.labels[0].x < 0
    assert geometry.labels[0].y < 0
    assert {label.number: label.name for label in geometry.labels} == names


def test_bottom_view_conversion_matches_top_view_geometry() -> None:
    words, _, _ = quad16_fixture()
    top, top_issues = _derived(words)
    bottom_words = [
        PdfWord(
            text=word.text,
            x0=-word.x1,
            top=word.top,
            x1=-word.x0,
            bottom=word.bottom,
        )
        for word in words
    ]
    bottom, bottom_issues = _derived(bottom_words, view="bottom")

    assert top is not None and bottom is not None
    assert top_issues == bottom_issues == []
    assert bottom.winding == top.winding == "ccw"
    assert {label.number: (label.x, label.y, label.name) for label in bottom.labels} == {
        label.number: (label.x, label.y, label.name) for label in top.labels
    }


def test_dual_view_resolves_eight_pins() -> None:
    words, _, names = dual8_fixture()
    geometry, issue_codes = _derived(words, pin_count=8)

    assert geometry is not None
    assert issue_codes == []
    assert geometry.winding == "ccw"
    assert {label.number: label.name for label in geometry.labels} == names


def test_dual_orientation_comparison_detects_mirror_and_rotation() -> None:
    _, positions, _ = dual8_fixture()
    mirrored = {number: (-point[0], point[1]) for number, point in positions.items()}
    rotated = {number: (-point[1], point[0]) for number, point in positions.items()}

    assert [issue.code for issue in compare_orientation(positions, mirrored)] == [
        "chirality_mismatch"
    ]
    assert [issue.code for issue in compare_orientation(positions, rotated)] == [
        "rotation_mismatch"
    ]


def test_missing_and_duplicate_number_labels_fail_closed() -> None:
    words, _, _ = quad16_fixture()
    missing, missing_issues = _derived([word for word in words if word.text != "7"])
    duplicate, duplicate_issues = _derived([*words, fixture_word("7", (0, 0))])

    assert missing is None
    assert "pinout_number_missing" in missing_issues
    assert duplicate is None
    assert "pinout_number_duplicate" in duplicate_issues


def test_pinout_name_unresolved_and_ambiguous() -> None:
    words, _, _ = quad16_fixture()
    without_pin1_name = [word for word in words if not (word.text == "SW" and word.x0 < -15)]
    unresolved, unresolved_issues = _derived(without_pin1_name)
    ambiguous_words = [
        *words,
        fixture_word("ALT", (-15.5, -5.2)),
    ]
    ambiguous, ambiguous_issues = _derived(ambiguous_words)

    assert unresolved is not None
    assert "pinout_name_unresolved" in unresolved_issues
    assert ambiguous is not None
    assert "pinout_name_ambiguous" in ambiguous_issues


def test_orientation_comparison_reports_chirality_rotation_and_order() -> None:
    _, positions, _ = quad16_fixture()
    mirrored = {number: (-x, y) for number, (x, y) in positions.items()}
    rotated = {number: (-x, -y) for number, (x, y) in positions.items()}
    swapped = dict(positions)
    swapped["2"], swapped["3"] = swapped["3"], swapped["2"]

    assert [issue.code for issue in compare_orientation(positions, mirrored)] == [
        "chirality_mismatch"
    ]
    assert [issue.code for issue in compare_orientation(positions, rotated)] == [
        "rotation_mismatch"
    ]
    assert [issue.code for issue in compare_orientation(positions, swapped)] == ["order_mismatch"]


def test_permutation_diagnoses_shift_and_horizontal_mirror() -> None:
    _, positions, expected = quad16_fixture()
    numbers = sorted(positions, key=int)
    shifted = {number: expected[str(int(number) % len(numbers) + 1)] for number in numbers}
    mirrored = {
        number: expected[
            min(
                positions,
                key=lambda candidate: math.dist(
                    (-normalized(positions)[number][0], normalized(positions)[number][1]),
                    normalized(positions)[candidate],
                ),
            )
        ]
        for number in numbers
    }

    assert diagnose_permutation(positions, expected, shifted) == ["shift+1"]
    assert "mirror_x" in diagnose_permutation(positions, expected, mirrored)


def test_name_normalization_matches_kicad_overbars_and_negation() -> None:
    assert names_equal("~{RESET}#", "#reset")
    assert names_equal(" V_DD ", "vdd")
    assert not names_equal("RESET", "SET")


def test_normalized_points_have_centered_unit_extents() -> None:
    result = normalized({"1": (10.0, 10.0), "2": (10.0, 30.0), "3": (30.0, 10.0)})

    assert math.isclose(sum(point[0] for point in result.values()) / 3, 0.0, abs_tol=1e-9)
    assert math.isclose(sum(point[1] for point in result.values()) / 3, 0.0, abs_tol=1e-9)
    assert max(abs(point[0]) for point in result.values()) == 1
    assert max(abs(point[1]) for point in result.values()) == 1
