"""Unit + property checks for matching and the chunk-index completeness."""
from __future__ import annotations

import random

from app.codes import (
    CHUNK_COUNT,
    chunk_signatures,
    grid_key,
    is_hit,
    match_grid,
    parse_grid,
    query_chunk_signatures,
    rotate,
)


def test_parse_shapes_and_unknown_tokens():
    nested = [[0, 1, None, "?", "x"]] * 5
    flat = parse_grid(nested)
    assert len(flat) == 25
    assert flat[2] is None and flat[3] is None and flat[4] is None

    rows = ["01010", "10101", "01010", "10101", "01010"]
    assert parse_grid(rows)[:5] == (0, 1, 0, 1, 0)

    flat_wire = [0, 1] * 12 + [0]
    assert len(parse_grid(flat_wire)) == 25


def test_rotation_is_four_cycles():
    g = tuple(range(25))
    r1 = rotate(g, 1)
    assert r1[0] == 20  # bottom-left moves to top-left on clockwise rotation
    assert rotate(g, 4) == g
    assert rotate(g, 2) == rotate(rotate(g, 1), 1)


def test_exact_and_single_mismatch_hits():
    tag = tuple([0] * 25)
    assert match_grid(tag, tag).mismatches == 0

    one_wrong = list(tag)
    one_wrong[7] = 1
    m = match_grid(one_wrong, tag)
    assert m is not None and m.mismatches == 1

    two_wrong = list(tag)
    two_wrong[7] = 1
    two_wrong[8] = 1
    assert match_grid(two_wrong, tag) is None


def test_unreadable_cells_are_not_counted():
    tag = tuple([0] * 25)
    scan = [None] * 4 + [0] * 21
    assert is_hit(scan, tag)
    # Four unclear plus one genuinely wrong visible cell still hits.
    scan2 = list(scan)
    scan2[10] = 1
    m = match_grid(scan2, tag)
    assert m is not None and m.mismatches == 1
    # Four unclear plus two wrong visible cells misses.
    scan3 = list(scan2)
    scan3[11] = 1
    assert match_grid(scan3, tag) is None


def test_rotation_orientation_detected():
    # Deliberately asymmetric code.
    tag = [
        1, 1, 0, 0, 0,
        1, 0, 0, 0, 0,
        1, 1, 1, 0, 0,
        0, 0, 0, 0, 0,
        0, 0, 0, 0, 1,
    ]
    scan = rotate(tag, 1)
    m = match_grid(scan, tag)
    assert m is not None and m.orientation == 1 and m.mismatches == 0

    # A 180-degree submitted scan is detected at orientation 2.
    m2 = match_grid(rotate(tag, 2), tag)
    assert m2 is not None and m2.orientation == 2 and m2.mismatches == 0


def test_grid_key_identity():
    g = [[None, 1, 0, 1, 0]] * 5
    k = grid_key(parse_grid(g))
    assert len(k) == 25 and k.count("?") == 5
    assert grid_key(parse_grid(g)) == k


def test_chunk_index_is_complete_for_every_possible_hit():
    """Pigeonhole proof, exercised over many random cases:

    whenever scan hits tag, at least one stored chunk signature must be
    among the tag's query signatures.
    """
    rng = random.Random(20261124)
    for trial in range(4000):
        tag = tuple(rng.randrange(2) for _ in range(25))
        scan = list(rotate(tag, rng.randrange(4)))
        # Introduce up to 4 unreadable cells and up to 1 visible error.
        positions = list(range(25))
        rng.shuffle(positions)
        unknown_count = rng.randrange(5)
        for p in positions[:unknown_count]:
            scan[p] = None
        if rng.random() < 0.7:
            error_pos = positions[unknown_count]
            if scan[error_pos] is not None:
                scan[error_pos] ^= 1

        if not is_hit(scan, tag):
            continue

        stored = chunk_signatures(scan)
        queried = set(query_chunk_signatures(tag))
        assert stored & queried, f"index missed hit on trial {trial}"
        # Every chunk family is represented in the design.
        assert all(0 <= ci < CHUNK_COUNT for ci, _ in stored)


def test_chunk_index_has_no_false_negatives_under_adversarial_layout():
    """Place 4 unknowns + 1 error one per distinct chunk as far as possible;
    a clean sixth chunk must still be indexed and found."""
    tag = tuple([0] * 25)
    scan: list[int | None] = [0] * 25
    # chunks ranges: [0:4],[4:8],[8:12],[12:16],[16:20],[20:25]
    for p in (0, 4, 8, 12):  # one unknown in four chunks
        scan[p] = None
    scan[16] = 1  # one error in a fifth chunk
    stored = chunk_signatures(scan)
    queried = set(query_chunk_signatures(tag))
    assert stored & queried
    assert is_hit(scan, tag)
