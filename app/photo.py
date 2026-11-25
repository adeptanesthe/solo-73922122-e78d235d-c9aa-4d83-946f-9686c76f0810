"""Photo recognition: perspective sampling of 5x5 dot-matrix tags.

The scanner hands us an 8-bit grayscale picture plus one to four candidate
border quadrilaterals.  Every group lists its corners in image order
**top-left, top-right, bottom-right, bottom-left**.  Coordinates are pixel
coordinates ``(x, y)`` where ``x`` is the column and ``y`` is the row; the
valid range is ``0 <= x <= width - 1`` and ``0 <= y <= height - 1``.

For each group we build the projective transform (homography) that maps the
unit square onto the quadrilateral, evaluate it at the centres of the 5x5
cells, round each coordinate to the nearest integer (half up) and read that
pixel:

* ``0..80``    -> black (``1``, the registry's black marker)
* ``176..255`` -> white (``0``)
* anything else -> unreadable (``None``)

Groups whose corners do not describe a strictly convex quadrilateral, whose
grid has more than four unreadable cells, or that leave sample points outside
the picture are reported as invalid; they never produce candidates.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from .codes import GRID_SIZE, MAX_UNREADABLE

MAX_IMAGE_SIDE = 256
MAX_BORDERS = 4
BLACK_MAX = 80
WHITE_MIN = 176

_EPS = 1e-9


class PhotoError(ValueError):
    """Global request error (bad image or border count) -> HTTP 422."""


class GroupError(ValueError):
    """One group cannot be turned into a grid / candidates.

    ``reason`` is a stable machine-readable identifier that is echoed in the
    response, for example ``non_convex_corners``.
    """

    def __init__(self, reason: str, message: str):
        self.reason = reason
        self.message = message
        super().__init__(message)


# ------------------------------------------------------------------ image

def _coerce_dimension(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PhotoError(f"{label} must be an integer")
    if value <= 0:
        raise PhotoError(f"{label} must be positive")
    return value


def _coerce_pixel(value: Any, row: int, col: int) -> int:
    if isinstance(value, bool):
        raise PhotoError(f"pixel ({row},{col}) is a boolean; expected an 8-bit gray value")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise PhotoError(f"pixel ({row},{col}) must be an integer 0..255")
        value = int(value)
    if not isinstance(value, int):
        raise PhotoError(f"pixel ({row},{col}) is not an integer 8-bit gray value")
    if not 0 <= value <= 255:
        raise PhotoError(f"pixel ({row},{col}) is {value}; expected 0..255")
    return value


def parse_image(
    raw: Any,
    width: int | None = None,
    height: int | None = None,
) -> list[list[int]]:
    """Normalize a nested (or flat + width/height) 8-bit grayscale image.

    A nested array is the canonical wire shape:
    ``[[gray, ...], ...]``.  A flat row-major array is also accepted when
    ``width`` and ``height`` are supplied.  Both sides must be at most
    :data:`MAX_IMAGE_SIDE`.
    """
    if isinstance(raw, dict):
        src = raw
        for key in ("pixels", "data", "values", "gray", "grayscale", "image"):
            if key in src:
                raw = src[key]
                break
        else:
            raise PhotoError("image object must contain pixels/data/values")
        width = src.get("width", src.get("cols", width))
        height = src.get("height", src.get("rows", height))

    if not isinstance(raw, (list, tuple)) or not raw:
        raise PhotoError("image must be a non-empty 2D grayscale array")

    nested = all(isinstance(row, (list, tuple)) for row in raw)
    if nested:
        if width is not None or height is not None:
            # Dimensions are inferred from nested rows; if supplied, they
            # must agree so a typo cannot silently reinterpret the picture.
            if height is not None:
                height = _coerce_dimension(height, "height")
                if height != len(raw):
                    raise PhotoError(f"height {height} does not match {len(raw)} image rows")
            if width is not None:
                width = _coerce_dimension(width, "width")
                if width != len(raw[0]):
                    raise PhotoError(f"width {width} does not match {len(raw[0])} image columns")
        rows = [list(row) for row in raw]
    else:
        if width is None or height is None:
            raise PhotoError(
                "flat image arrays require width and height; or send a nested 2D array"
            )
        width = _coerce_dimension(width, "width")
        height = _coerce_dimension(height, "height")
        if len(raw) != width * height:
            raise PhotoError(
                f"flat image has {len(raw)} values; expected {width * height} = width*height"
            )
        rows = [list(raw[r * width:(r + 1) * width]) for r in range(height)]

    height = len(rows)
    if height == 0:
        raise PhotoError("image must contain at least one row")
    width = len(rows[0])
    if width == 0:
        raise PhotoError("image rows must not be empty")
    if height > MAX_IMAGE_SIDE or width > MAX_IMAGE_SIDE:
        raise PhotoError(
            f"image is {width}x{height}; each side must be at most {MAX_IMAGE_SIDE} pixels"
        )
    for r, row in enumerate(rows):
        if len(row) != width:
            raise PhotoError(f"image row {r} has {len(row)} pixels; expected {width}")
        rows[r] = [_coerce_pixel(v, r, c) for c, v in enumerate(row)]
    return rows


# --------------------------------------------------------------- geometry

def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GroupError("invalid_corners", f"{label} is not a number")
    out = float(value)
    if not math.isfinite(out):
        raise GroupError("invalid_corners", f"{label} is not a finite number")
    return out


def _point(value: Any, label: str) -> tuple[float, float]:
    if isinstance(value, dict):
        if "x" in value and "y" in value:
            return _number(value["x"], f"{label}.x"), _number(value["y"], f"{label}.y")
        if "col" in value and "row" in value:
            return _number(value["col"], f"{label}.col"), _number(value["row"], f"{label}.row")
        raise GroupError("invalid_corners", f"{label} must contain x/y")
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _number(value[0], f"{label}[0]"), _number(value[1], f"{label}[1]")
    raise GroupError("invalid_corners", f"{label} must be an [x, y] pair or an x/y object")


def normalize_corners(raw: Any) -> list[tuple[float, float]]:
    """Accept four ``[x, y]`` pairs, four x/y objects, or eight flat numbers."""
    if isinstance(raw, dict):
        for key in ("corners", "corner", "quad", "points", "四角"):
            if key in raw:
                raw = raw[key]
                break
        else:
            raise GroupError("invalid_corners", "border group must contain corners")
    if not isinstance(raw, (list, tuple)):
        raise GroupError("invalid_corners", "corners must be a list")
    if len(raw) == 8 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
        return [
            (_number(raw[0], "corners[0]"), _number(raw[1], "corners[1]")),
            (_number(raw[2], "corners[2]"), _number(raw[3], "corners[3]")),
            (_number(raw[4], "corners[4]"), _number(raw[5], "corners[5]")),
            (_number(raw[6], "corners[6]"), _number(raw[7], "corners[7]")),
        ]
    if len(raw) != 4:
        raise GroupError(
            "invalid_corners",
            "corners must contain four points (TL, TR, BR, BL) or eight flat numbers",
        )
    return [_point(value, f"corners[{i}]") for i, value in enumerate(raw)]


def validate_corners_inside(
    corners: Sequence[tuple[float, float]], width: int, height: int
) -> None:
    for i, (x, y) in enumerate(corners):
        if not (0.0 <= x <= width - 1) or not (0.0 <= y <= height - 1):
            raise GroupError(
                "invalid_corners",
                f"corner {i} ({x:g},{y:g}) is outside the {width}x{height} image",
            )


def is_convex_quad(corners: Sequence[tuple[float, float]]) -> bool:
    """True for a strictly convex quadrilateral in the given cyclic order.

    Collinear neighbouring edges and self-crossing (bow-tie) orders fail.
    """
    if len(corners) != 4:
        return False
    signs: list[bool] = []
    for i in range(4):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % 4]
        cx, cy = corners[(i + 2) % 4]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) <= _EPS:
            return False
        signs.append(cross > 0)
    return all(signs) or not any(signs)


def homography_coefficients(
    corners: Sequence[tuple[float, float]],
) -> tuple[float, float, float, float, float, float, float, float]:
    """Coefficients mapping unit square ``(u,v)`` to the quad.

    ``x = (a*u + b*v + c) / (g*u + h*v + 1)`` and likewise for ``y``.
    The unit square corners ``(0,0),(1,0),(1,1),(0,1)`` map to the four
    corners in the supplied order.
    """
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = corners
    dx1, dx2, dx3 = x1 - x2, x3 - x2, x0 - x1 + x2 - x3
    dy1, dy2, dy3 = y1 - y2, y3 - y2, y0 - y1 + y2 - y3

    if abs(dx3) <= _EPS and abs(dy3) <= _EPS:
        # Affine (parallelogram) case.
        return (x1 - x0, x3 - x0, x0, y1 - y0, y3 - y0, y0, 0.0, 0.0)

    denom = dx1 * dy2 - dx2 * dy1
    if abs(denom) <= _EPS:
        raise GroupError(
            "non_convex_corners",
            "corners are degenerate; a perspective transform cannot be built",
        )
    g = (dx3 * dy2 - dx2 * dy3) / denom
    h = (dx1 * dy3 - dx3 * dy1) / denom
    a = (x1 - x0) + g * x1
    b = (x3 - x0) + h * x3
    d = (y1 - y0) + g * y1
    e = (y3 - y0) + h * y3
    return (a, b, x0, d, e, y0, g, h)


def invert_matrix3(m: Sequence[Sequence[float]]) -> list[list[float]]:
    """Inverse of a 3x3 matrix (used by the demo renderer and tests)."""
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) <= _EPS:
        raise GroupError("non_convex_corners", "singular perspective transform")
    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]


def classify_gray(value: int) -> int | None:
    """Apply the black / white / unreadable thresholds.

    The registry's cell convention is ``1`` = black, ``0`` = white (see
    :mod:`app.codes`), so dark pixels map to ``1``.
    """
    if value <= BLACK_MAX:
        return 1
    if value >= WHITE_MIN:
        return 0
    return None


def sample_grid(
    image: Sequence[Sequence[int]],
    corners: Sequence[tuple[float, float]],
) -> tuple[int | None, ...]:
    """Perspective-sample the 25 cell centres into a row-major grid."""
    height = len(image)
    width = len(image[0])
    validate_corners_inside(corners, width, height)
    if not is_convex_quad(corners):
        raise GroupError(
            "non_convex_corners",
            "the four corners do not form a strictly convex quadrilateral",
        )

    a, b, c, d, e, f, g, h = homography_coefficients(corners)
    cells: list[int | None] = []
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            u = (col + 0.5) / GRID_SIZE
            v = (row + 0.5) / GRID_SIZE
            w = g * u + h * v + 1.0
            if abs(w) <= _EPS:
                raise GroupError("non_convex_corners", "degenerate perspective transform")
            x = (a * u + b * v + c) / w
            y = (d * u + e * v + f) / w
            # Round half up ("四舍五入").
            sx = math.floor(x + 0.5)
            sy = math.floor(y + 0.5)
            if not (0 <= sx < width and 0 <= sy < height):
                raise GroupError(
                    "invalid_corners",
                    f"cell centre ({sx},{sy}) falls outside the {width}x{height} image",
                )
            cells.append(classify_gray(image[sy][sx]))
    return tuple(cells)


def extract_group(
    image: Sequence[Sequence[int]], raw_corners: Any
) -> tuple[list[tuple[float, float]], tuple[int | None, ...]]:
    """Corners -> (normalized corners, 25-cell grid), or raise GroupError."""
    corners = normalize_corners(raw_corners)
    flat = sample_grid(image, corners)
    return corners, flat


def unreadable_count(flat: Sequence[int | None]) -> int:
    return sum(1 for value in flat if value is None)


def validate_group_readability(flat: Sequence[int | None]) -> None:
    unknown = unreadable_count(flat)
    if unknown > MAX_UNREADABLE:
        raise GroupError(
            "too_many_unreadable",
            f"photo group has {unknown} unreadable cells; at most {MAX_UNREADABLE} are allowed",
        )
