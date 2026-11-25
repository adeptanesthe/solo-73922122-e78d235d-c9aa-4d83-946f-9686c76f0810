"""5x5 dot-matrix tag domain logic.

Rules (from the exhibit prop-box spec):

* A registered tag is a complete 5x5 black/white grid (25 cells, 0/1).
* A scanner grid is the straightened same-size grid.  At most
  ``MAX_UNREADABLE`` cells may be unreadable (``None``).
* A box number is a *hit* when, for at least one of the four quarter-turn
  rotations of the registered tag, the scanner grid differs in at most
  ``MAX_MISMATCH`` of the *visible* cells.

Indexed conflict checks
-----------------------
Registration must not scan every confirmed reading.  We maintain a chunk
signature index (see :func:`chunk_signatures`).  The 25 positions are
partitioned into :data:`CHUNK_COUNT` disjoint chunks (six).  For a matching
reading/tag pair there are at most ``MAX_UNREADABLE + MAX_MISMATCH = 5``
"bad" positions (unreadable or wrong).  Six chunks and five bad positions
imply, by the pigeonhole principle, that at least one chunk is completely
clean: all its cells are visible in the reading and exactly equal the tag.
Storing the exact bits of every fully visible chunk of all four rotations of
every confirmed reading therefore lets us retrieve every possible conflict
through a handful of indexed lookups, followed by exact verification.  The
index is complete (it can never miss a conflict); lookups may return false
candidates, which the full matcher filters out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

GRID_SIZE = 5
CELL_COUNT = GRID_SIZE * GRID_SIZE
MAX_UNREADABLE = 4
MAX_MISMATCH = 1

# Six disjoint contiguous chunks over row-major positions 0..24:
# sizes 4, 4, 4, 4, 4, 5.
CHUNK_RANGES: tuple[tuple[int, int], ...] = (
    (0, 4),
    (4, 8),
    (8, 12),
    (12, 16),
    (16, 20),
    (20, 25),
)
CHUNK_COUNT = len(CHUNK_RANGES)

_UNKNOWN_STRINGS = {
    "?", "？", "x", "Ｘ", "ｘ", "✕", "×", "*", "-", "—", "_", ".",
    "", "null", "none", "n/a", "na",
}
_ZERO_STRINGS = {"0", "０", "w", "W", "white", "白", "白格"}
_ONE_STRINGS = {"1", "１", "b", "B", "black", "黑", "黑格"}
# Numeric markers scanners sometimes use for "could not read".
_UNKNOWN_NUMBERS = {-1, 2, 9}


class GridError(ValueError):
    """Raised when a submitted value cannot be interpreted as a 5x5 grid."""


def normalize_cell(value: Any) -> int | None:
    """Coerce one wire value to ``0``, ``1`` or ``None`` (unreadable)."""
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None:
        return None
    if isinstance(value, int):
        if value == 0 or value == 1:
            return value
        if value in _UNKNOWN_NUMBERS:
            return None
        raise GridError(f"invalid cell value {value!r}; expected 0, 1 or an unreadable marker")
    if isinstance(value, float) and value.is_integer():
        return normalize_cell(int(value))
    if isinstance(value, str):
        token = value.strip()
        if token in _ZERO_STRINGS:
            return 0
        if token in _ONE_STRINGS:
            return 1
        if token in {"-1", "2", "9"}:  # string-encoded unclear markers
            return None
        if token.lower() in _UNKNOWN_STRINGS or token in _UNKNOWN_STRINGS:
            return None
        raise GridError(
            f"invalid cell value {value!r}; expected 0/1 or an unreadable marker such as null or '?'"
        )
    raise GridError(f"unsupported cell value {value!r}")


def parse_grid(raw: Any) -> tuple[int | None, ...]:
    """Parse many convenient wire shapes into a 25-tuple.

    Accepted shapes:

    * ``[[0, 1, ...5], ...5]`` -- canonical 5x5 nested arrays;
    * ``[0, 1, ...25]`` -- flat row-major array;
    * ``["01010", ...5]`` -- five row strings;
    * a single 25-character string.

    Unreadable cells: ``null``, ``"?"``, ``"x"``, ``"-"``, ``2`` or ``-1``.
    Shape errors raise :class:`GridError`.
    """
    if isinstance(raw, str):
        raw = [ch for ch in raw if ch not in (" ", "\n", "\r", "\t")]

    if not isinstance(raw, (list, tuple)):
        raise GridError("grid must be a 5x5 array (or a flat array of 25 cells)")

    # Five row strings.
    if len(raw) == GRID_SIZE and all(isinstance(row, str) for row in raw):
        cells: list[Any] = []
        for row in raw:
            cells.extend(list(row))
        raw = cells

    # Nested 5x5 arrays.
    if len(raw) == GRID_SIZE and all(isinstance(row, (list, tuple)) for row in raw):
        flat: list[Any] = []
        for r, row in enumerate(raw):
            if len(row) != GRID_SIZE:
                raise GridError(f"row {r} has {len(row)} cells; expected {GRID_SIZE}")
            flat.extend(row)
        raw = flat

    if len(raw) != CELL_COUNT:
        raise GridError(
            f"grid has {len(raw)} cells; expected {CELL_COUNT} in a {GRID_SIZE}x{GRID_SIZE} grid"
        )

    return tuple(normalize_cell(v) for v in raw)


def to_matrix(flat: Sequence[int | None]) -> list[list[int | None]]:
    return [list(flat[r * GRID_SIZE:(r + 1) * GRID_SIZE]) for r in range(GRID_SIZE)]


def grid_key(flat: Sequence[int | None]) -> str:
    """Canonical exact-grid identity used for idempotent rescans.

    ``0``/``1`` are literal; unreadable cells become ``?``.  Two submissions
    with the same key are the *same* grid array (same unclear positions).
    """
    return "".join("?" if v is None else str(v) for v in flat)


def unreadable_positions(flat: Sequence[int | None]) -> tuple[int, ...]:
    return tuple(i for i, v in enumerate(flat) if v is None)


def rotate(flat: Sequence[int | None], turns: int) -> tuple[int | None, ...]:
    """Rotate a row-major grid clockwise by ``turns`` quarter turns."""
    turns %= 4
    grid = [list(flat[r * GRID_SIZE:(r + 1) * GRID_SIZE]) for r in range(GRID_SIZE)]
    for _ in range(turns):
        grid = [list(row) for row in zip(*grid[::-1])]
    return tuple(v for row in grid for v in row)


@dataclass(frozen=True)
class Match:
    orientation: int  # clockwise quarter turns of the registered tag
    mismatches: int  # wrong visible cells at the best orientation
    visible: int

    def as_dict(self) -> dict[str, int]:
        return {
            "orientation": self.orientation,
            "mismatches": self.mismatches,
            "visible_cells": self.visible,
        }


def match_grid(scan: Sequence[int | None], tag: Sequence[int | None]) -> Match | None:
    """Return the best hit of ``scan`` against complete ``tag``, else ``None``.

    Each of the four rotations of ``tag`` is tried.  Only visible
    (non-``None``) scan cells count.  A hit needs at most
    ``MAX_MISMATCH`` mismatching visible cells.
    """
    visible = sum(1 for v in scan if v is not None)
    best: Match | None = None
    for turns in range(4):
        rotated = rotate(tag, turns)
        mismatches = 0
        for sv, tv in zip(scan, rotated):
            if sv is not None and sv != tv:
                mismatches += 1
                if mismatches > MAX_MISMATCH:
                    break
        if mismatches <= MAX_MISMATCH and (best is None or mismatches < best.mismatches):
            best = Match(orientation=turns, mismatches=mismatches, visible=visible)
    return best


def is_hit(scan: Sequence[int | None], tag: Sequence[int | None]) -> bool:
    return match_grid(scan, tag) is not None


def chunk_bits(flat: Sequence[int | None], chunk_index: int) -> str | None:
    """Exact bit signature of one chunk, or ``None`` if it has an unclear cell."""
    lo, hi = CHUNK_RANGES[chunk_index]
    segment = flat[lo:hi]
    if any(v is None for v in segment):
        return None
    return "".join(str(v) for v in segment)  # type: ignore[arg-type]


def chunk_signatures(flat: Sequence[int | None]) -> set[tuple[int, str]]:
    """All ``(chunk_index, bits)`` rows to index for one stored grid.

    For confirmed readings we index every fully visible chunk of all four
    rotations (at most ``4 * CHUNK_COUNT`` rows; duplicates collapsed).
    """
    rows: set[tuple[int, str]] = set()
    for turns in range(4):
        rotated = rotate(flat, turns)
        for ci in range(CHUNK_COUNT):
            bits = chunk_bits(rotated, ci)
            if bits is not None:
                rows.add((ci, bits))
    return rows


def query_chunk_signatures(tag: Sequence[int | None]) -> list[tuple[int, str]]:
    """Chunk lookups to run for a complete candidate tag.

    The tag is complete, so every chunk yields exact bits.  Stored readings
    carry all four rotations, hence no rotation of the query is needed.
    """
    out: list[tuple[int, str]] = []
    for ci in range(CHUNK_COUNT):
        bits = chunk_bits(tag, ci)
        if bits is not None:
            out.append((ci, bits))
    return out


def validate_for_registration(flat: Sequence[int | None]) -> None:
    if any(v is None for v in flat):
        raise GridError("registered tag must be a complete 5x5 code (no unreadable cells)")


def validate_for_scan(flat: Sequence[int | None]) -> None:
    unknown = sum(1 for v in flat if v is None)
    if unknown > MAX_UNREADABLE:
        raise GridError(
            f"scan has {unknown} unreadable cells; at most {MAX_UNREADABLE} are allowed"
        )
