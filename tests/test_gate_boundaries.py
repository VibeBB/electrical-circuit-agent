"""Boundary and property tests for the library-verification geometry kernel.

`circuit.libverify` decides courtyard enclosure, keep-out and clearance
findings with a small set of planar predicates. These tests pin their
tolerance boundaries (below / on / above) and check algebraic properties
(symmetry, translation invariance, agreement with brute-force sampling)
on deterministic grids, following docs/test-coverage.md.
"""

from __future__ import annotations

import itertools
import math

import pytest

from circuit import libverify

# pyright: reportPrivateUsage=false
contains = libverify._contains
cross = libverify._cross
on_segment = libverify._on_segment
segments_intersect = libverify._segments_intersect
point_segment_distance = libverify._point_segment_distance
point_in_polygon = libverify._point_in_polygon
polygon_distance = libverify._polygon_distance

Point = tuple[float, float]
SQUARE: list[Point] = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
GRID = [x / 2 for x in range(-2, 7)]
GRID_POINTS: list[Point] = [(x, y) for x in GRID for y in GRID]


def _shift(points: list[Point], dx: float, dy: float) -> list[Point]:
    return [(x + dx, y + dy) for x, y in points]


# ---------------------------------------------------------------- _contains


@pytest.mark.parametrize("side", range(4))
@pytest.mark.parametrize(("delta", "inside"), [(-1e-3, True), (0.0, True), (1e-3, False)])
def test_contains_three_value_boundary_per_side(side: int, delta: float, inside: bool) -> None:
    outer = (0.0, 0.0, 10.0, 10.0)
    inner = [1.0, 1.0, 9.0, 9.0]
    edge = (0.0, 0.0, 10.0, 10.0)[side]
    # Push one side of the inner box onto, just inside or just past the outer edge.
    inner[side] = edge - delta if side < 2 else edge + delta
    assert contains(outer, (inner[0], inner[1], inner[2], inner[3])) is inside


@pytest.mark.parametrize(
    ("overhang", "inside"), [(1e-6 - 1e-9, True), (1e-6, True), (1e-6 + 1e-9, False)]
)
def test_contains_tolerance_boundary(overhang: float, inside: bool) -> None:
    outer = (0.0, 0.0, 10.0, 10.0)
    assert contains(outer, (1.0, 1.0, 10.0 + overhang, 9.0), 1e-6) is inside


def test_contains_is_reflexive_and_transitive() -> None:
    boxes = [(0.0, 0.0, 10.0, 10.0), (1.0, 1.0, 9.0, 9.0), (2.0, 2.0, 8.0, 8.0)]
    assert all(contains(box, box) for box in boxes)
    assert contains(boxes[0], boxes[1]) and contains(boxes[1], boxes[2])
    assert contains(boxes[0], boxes[2])
    assert not contains(boxes[2], boxes[0])


# -------------------------------------------------------------- _on_segment


@pytest.mark.parametrize(
    ("point", "on"),
    [
        ((0.0, 0.0), True),
        ((1.0, 0.0), True),
        ((2.0, 0.0), True),
        ((2.0 + 2e-9, 0.0), False),
        ((-2e-9, 0.0), False),
        ((1.0, 5e-10), True),
        ((1.0, 2e-9), False),
    ],
)
def test_on_segment_tolerance_boundaries(point: Point, on: bool) -> None:
    assert on_segment((0.0, 0.0), (2.0, 0.0), point) is on


# ------------------------------------------------------ _segments_intersect

SEGMENTS: list[tuple[Point, Point]] = [
    ((x0, y0), (x1, y1))
    for (x0, y0), (x1, y1) in itertools.combinations(
        [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (1.0, 1.0), (1.0, 3.0)], 2
    )
]


@pytest.mark.parametrize(
    ("c", "d", "hit"),
    [
        ((1.0, -1.0), (1.0, 1.0), True),
        ((2.0, -1.0), (2.0, 1.0), True),
        ((2.0 + 1e-6, -1.0), (2.0 + 1e-6, 1.0), False),
        ((1.0, 1e-6), (1.0, 1.0), False),
        ((1.0, 0.0), (1.0, 1.0), True),
        ((3.0, 0.0), (4.0, 0.0), False),
        ((1.0, 0.0), (3.0, 0.0), True),
    ],
)
def test_segment_intersection_cases(c: Point, d: Point, hit: bool) -> None:
    assert segments_intersect((0.0, 0.0), (2.0, 0.0), c, d) is hit


def test_segment_intersection_is_symmetric() -> None:
    for (a, b), (c, d) in itertools.product(SEGMENTS, repeat=2):
        expected = segments_intersect(a, b, c, d)
        assert segments_intersect(c, d, a, b) is expected
        assert segments_intersect(b, a, d, c) is expected


# --------------------------------------------------- _point_segment_distance


@pytest.mark.parametrize(
    ("point", "distance"),
    [
        ((-1.0, 0.0), 1.0),
        ((0.0, 1.0), 1.0),
        ((1.0, 1.0), 1.0),
        ((2.0, 1.0), 1.0),
        ((3.0, 0.0), 1.0),
        ((3.0, 4.0), math.hypot(1.0, 4.0)),
    ],
)
def test_point_segment_distance_clamps_to_endpoints(point: Point, distance: float) -> None:
    assert point_segment_distance(point, (0.0, 0.0), (2.0, 0.0)) == pytest.approx(distance)


def test_degenerate_segment_is_point_distance() -> None:
    assert point_segment_distance((3.0, 4.0), (0.0, 0.0), (0.0, 0.0)) == 5.0


def test_point_segment_distance_is_a_lower_bound_of_sampled_points() -> None:
    start, end = (0.0, 0.0), (2.0, 1.0)
    samples = [(start[0] + t / 50 * 2.0, start[1] + t / 50) for t in range(51)]
    for point in GRID_POINTS:
        exact = point_segment_distance(point, start, end)
        sampled = min(math.dist(point, sample) for sample in samples)
        assert exact <= sampled + 1e-12
        assert sampled - exact <= math.dist(samples[0], samples[1]) / 2 + 1e-12


# -------------------------------------------------------- _point_in_polygon


def test_point_in_square_matches_bounds_on_a_grid() -> None:
    for point in GRID_POINTS:
        expected = 0.0 <= point[0] <= 2.0 and 0.0 <= point[1] <= 2.0
        assert point_in_polygon(point, SQUARE) is expected, point


def test_point_in_concave_polygon() -> None:
    ell: list[Point] = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    assert point_in_polygon((0.5, 1.5), ell)
    assert point_in_polygon((1.5, 0.5), ell)
    assert not point_in_polygon((1.5, 1.5), ell)
    assert point_in_polygon((1.0, 1.5), ell)


# --------------------------------------------------------- _polygon_distance


@pytest.mark.parametrize(("gap", "expected"), [(-0.5, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
def test_polygon_distance_three_value_gap(gap: float, expected: float) -> None:
    other = _shift(SQUARE, 2.0 + gap, 0.0)
    assert polygon_distance(SQUARE, other) == pytest.approx(expected)


def test_nested_polygon_distance_is_zero() -> None:
    inner = [(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)]
    assert polygon_distance(SQUARE, inner) == 0.0
    assert polygon_distance(inner, SQUARE) == 0.0


def test_polygon_distance_symmetry_and_translation_invariance() -> None:
    triangle: list[Point] = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    for dx, dy in itertools.product((-3.0, -1.5, 0.0, 2.5, 4.0), repeat=2):
        moved = _shift(triangle, dx, dy)
        forward = polygon_distance(SQUARE, moved)
        assert forward == pytest.approx(polygon_distance(moved, SQUARE))
        assert forward == pytest.approx(
            polygon_distance(_shift(SQUARE, 7.0, -3.0), _shift(moved, 7.0, -3.0))
        )
        assert forward >= 0.0


def test_cross_sign_encodes_orientation() -> None:
    assert cross((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)) > 0
    assert cross((0.0, 0.0), (1.0, 0.0), (0.0, -1.0)) < 0
    assert cross((0.0, 0.0), (1.0, 0.0), (5.0, 0.0)) == 0
