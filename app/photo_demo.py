"""Deterministic photo demo: one picture, two candidate card borders.

The generated grayscale picture shows the same 5x5 tag under two detected
border quadrilaterals (an inner and a slightly larger outer edge).  The two
perspective samples are deliberately made different by two small dust specks
that only land on the outer border's cell centres, so the demo proves the
headline rule: different grids, same box -> both readings confirmed, each
with its own number (and existing grids keep their original numbers).
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from . import services
from .codes import GRID_SIZE, grid_key, match_grid, to_matrix
from .photo import (
    homography_coefficients,
    invert_matrix3,
    sample_grid,
    unreadable_count,
)

PHOTO_BOX_NUMBER = "BOX-PHOTO-01"
PHOTO_SCANNER_ID = "photo-gate"
IMAGE_SIZE = 64

BLACK_GRAY = 20
WHITE_GRAY = 235
BACKGROUND_GRAY = 170
DUST_GRAY = 128  # 81..175 -> unreadable

#: The tag carried by the card.  Distinct from every seeded demo tag.
PHOTO_TAG: list[list[int]] = [
    [1, 0, 0, 1, 0],
    [0, 1, 0, 0, 0],
    [0, 0, 1, 0, 1],
    [1, 0, 0, 0, 0],
    [0, 0, 1, 0, 1],
]

#: Outer border and inner border, ordered TL, TR, BR, BL (x, y).
OUTER_CORNERS: list[list[float]] = [
    [11.0, 9.0],
    [47.0, 12.0],
    [44.0, 48.0],
    [9.0, 45.0],
]
INNER_CORNERS: list[list[float]] = [
    [12.0, 10.0],
    [48.0, 13.0],
    [45.0, 49.0],
    [10.0, 46.0],
]

#: Cells whose centres only the outer border sees covered by dust.
DUST_CELLS = ((2, 1), (1, 3))


def _coeff_matrix(corners: Sequence[Sequence[float]]) -> list[list[float]]:
    a, b, c, d, e, f, g, h = homography_coefficients(
        [(float(x), float(y)) for x, y in corners]
    )
    return [[a, b, c], [d, e, f], [g, h, 1.0]]


def _cell_index(corners: Sequence[Sequence[float]], row: int, col: int) -> tuple[int, int]:
    """Rounded pixel coordinate of one cell centre for the given border."""
    a, b, c, d, e, f, g, h = homography_coefficients(
        [(float(x), float(y)) for x, y in corners]
    )
    u = (col + 0.5) / GRID_SIZE
    v = (row + 0.5) / GRID_SIZE
    w = g * u + h * v + 1.0
    x = (a * u + b * v + c) / w
    y = (d * u + e * v + f) / w
    return math.floor(x + 0.5), math.floor(y + 0.5)


def render_quad(
    size: int,
    corners: Sequence[Sequence[float]],
    tag: Sequence[Sequence[int]],
    *,
    background: int = BACKGROUND_GRAY,
) -> list[list[int]]:
    """Render a 5x5 tag through the inverse perspective transform.

    Every pixel whose coordinate lies inside the quadrilateral gets the
    gray value of the tag cell it falls in; everything else is background.
    Sampling the cell centres of the same quad therefore recovers ``tag``.
    """
    m = invert_matrix3(_coeff_matrix(corners))
    image: list[list[int]] = []
    for y in range(size):
        row: list[int] = []
        for x in range(size):
            w = m[2][0] * x + m[2][1] * y + m[2][2]
            if abs(w) < 1e-12:
                row.append(background)
                continue
            u = (m[0][0] * x + m[0][1] * y + m[0][2]) / w
            v = (m[1][0] * x + m[1][1] * y + m[1][2]) / w
            if 0.0 <= u < 1.0 and 0.0 <= v < 1.0:
                cell = tag[min(GRID_SIZE - 1, int(v * GRID_SIZE))][
                    min(GRID_SIZE - 1, int(u * GRID_SIZE))
                ]
                row.append(BLACK_GRAY if cell else WHITE_GRAY)
            else:
                row.append(background)
        image.append(row)
    return image


def build_demo() -> tuple[list[list[int]], list[dict[str, Any]], list[int | None], list[int | None]]:
    image = render_quad(IMAGE_SIZE, OUTER_CORNERS, PHOTO_TAG)

    # Dust specks: paint a 2x2 block anchored at the *outer* border's cell
    # centre.  The inner border samples one pixel up-left of that, so only
    # the outer grid turns these cells into "unreadable".
    for row, col in DUST_CELLS:
        sx, sy = _cell_index(OUTER_CORNERS, row, col)
        ix, iy = _cell_index(INNER_CORNERS, row, col)
        # The 2x2 block sits up-left of the outer sample point, so the inner
        # border (which samples one pixel down-right of it) stays clean.
        ex, ey = sx - 1, sy - 1
        if (ix, iy) == (sx, sy) or (abs(ix - ex) <= 1 and abs(iy - ey) <= 1):
            raise RuntimeError("demo dust would also cover the inner border")
        for dx in (0, 1):
            for dy in (0, 1):
                if 0 <= ex + dx < IMAGE_SIZE and 0 <= ey + dy < IMAGE_SIZE:
                    image[ey + dy][ex + dx] = DUST_GRAY

    inner_grid = sample_grid(image, [tuple(p) for p in INNER_CORNERS])
    outer_grid = sample_grid(image, [tuple(p) for p in OUTER_CORNERS])
    borders = [
        {"id": "card-outer", "corners": [list(p) for p in OUTER_CORNERS]},
        {"id": "card-inner", "corners": [list(p) for p in INNER_CORNERS]},
    ]
    return image, borders, inner_grid, outer_grid


IMAGE, BORDERS, INNER_GRID, OUTER_GRID = build_demo()

_EXPECTED_FLAT = tuple(cell for row in PHOTO_TAG for cell in row)


def verify_demo() -> None:
    """Fail fast at import time if the picture no longer samples as intended."""
    if tuple(INNER_GRID) != _EXPECTED_FLAT:
        raise RuntimeError("photo demo inner border does not sample the tag exactly")
    if unreadable_count(OUTER_GRID) != len(DUST_CELLS):
        raise RuntimeError("photo demo outer border should have exactly the dust cells unclear")
    tag = _EXPECTED_FLAT
    if match_grid(INNER_GRID, tag) is None or match_grid(OUTER_GRID, tag) is None:
        raise RuntimeError("photo demo grids must both match the tag")
    if grid_key(INNER_GRID) == grid_key(OUTER_GRID):
        raise RuntimeError("photo demo grids must differ")
    if unreadable_count(INNER_GRID) > 0:
        raise RuntimeError("photo demo inner grid should be fully readable")


verify_demo()


def _copy_pixels(rows: Sequence[Sequence[int]]) -> list[list[int]]:
    return [list(row) for row in rows]


def _copy_borders() -> list[dict[str, Any]]:
    return [
        {"id": border["id"], "corners": [list(point) for point in border["corners"]]}
        for border in BORDERS
    ]


def request_body() -> dict[str, Any]:
    """A fresh copy each call: callers may mutate it without side effects."""
    return {
        "scanner_id": PHOTO_SCANNER_ID,
        "image": {
            "width": IMAGE_SIZE,
            "height": IMAGE_SIZE,
            "pixels": _copy_pixels(IMAGE),
        },
        "borders": _copy_borders(),
    }


def photo_scenario_payload() -> dict[str, Any]:
    box = services.get_box(PHOTO_BOX_NUMBER)
    readings = services.list_readings(limit=500)
    by_key = {r["grid_key"]: r["reading_no"] for r in readings}
    return {
        "title": "一张照片 / 两组边框：不同格阵，同一箱",
        "seeded_box": box,
        "request_body": request_body(),
        "expected": {
            "inner_grid_key": grid_key(INNER_GRID),
            "outer_grid_key": grid_key(OUTER_GRID),
            "inner_unreadable": unreadable_count(INNER_GRID),
            "outer_unreadable": unreadable_count(OUTER_GRID),
            "box_number": PHOTO_BOX_NUMBER,
            "inner_reading_id": by_key.get(grid_key(INNER_GRID)),
            "outer_reading_id": by_key.get(grid_key(OUTER_GRID)),
        },
        "steps": [
            "1) POST /api/demo/seed (Compose runs it at startup) registers "
            f"{PHOTO_BOX_NUMBER} and the original occluded demo reading.",
            "2) In /docs open POST /api/photo-scans, choose the example "
            "'two_borders_two_grids_same_box', Execute.",
            "3) Expect confirmed=true: both grids match "
            f"{PHOTO_BOX_NUMBER}, each with its own reading id.",
            "4) Execute again: the same grids reuse their original ids.",
        ],
    }


def seed_photo_demo() -> dict[str, Any]:
    """Idempotently register the photo demo box (does not scan the photo)."""
    if services.get_box(PHOTO_BOX_NUMBER) is None:
        services.register_box(PHOTO_TAG, box_number=PHOTO_BOX_NUMBER)
    return photo_scenario_payload()


def grid_matrix(flat: Sequence[int | None]) -> list[list[int | None]]:
    return to_matrix(flat)
