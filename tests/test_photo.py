"""Photo recognition: geometry, thresholds, and the all-or-nothing rules."""
from __future__ import annotations

import pytest

import app.repository as repo
import app.services as services
from app import photo, photo_demo
from app.codes import grid_key
from app.db import connect


# --------------------------------------------------------------- geometry

def test_identity_quad_samples_straight_grid():
    # 50x50 image, a border on the full image extent.  Cell centres fall on
    # 5, 15, 25, 35, 45 for both axes.
    image = [[0 if ((x // 10) + (y // 10)) % 2 == 0 else 255 for x in range(50)]
             for y in range(50)]
    corners = [(0.0, 0.0), (49.0, 0.0), (49.0, 49.0), (0.0, 49.0)]
    flat = photo.sample_grid(image, corners)
    # Dark pixels (gray 0) are the registry's black marker ``1``.
    expected = tuple((1 if ((c + r) % 2 == 0) else 0) for r in range(5) for c in range(5))
    assert flat == expected


def test_classify_threshold_boundaries():
    assert photo.classify_gray(0) == 1  # black -> registry marker 1
    assert photo.classify_gray(80) == 1
    assert photo.classify_gray(81) is None
    assert photo.classify_gray(175) is None
    assert photo.classify_gray(176) == 0  # white -> 0
    assert photo.classify_gray(255) == 0


def test_convexity_rejects_bowtie_and_collinear():
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert photo.is_convex_quad(square) is True
    bowtie = [(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)]
    assert photo.is_convex_quad(bowtie) is False
    collinear = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (10.0, 10.0)]
    assert photo.is_convex_quad(collinear) is False


def test_perspective_transform_recovers_rendered_tag():
    tag = [[1, 0, 1, 1, 0],
           [0, 1, 0, 0, 0],
           [1, 1, 1, 0, 0],
           [0, 0, 1, 1, 0],
           [1, 0, 0, 0, 1]]
    corners = [[4.0, 2.0], [57.0, 6.0], [52.0, 60.0], [6.0, 55.0]]
    image = photo_demo.render_quad(64, corners, tag)
    flat = photo.sample_grid(image, [tuple(p) for p in corners])
    assert flat == tuple(v for row in tag for v in row)


def test_cell_centres_are_rounded_half_up():
    # 26x26 image; border spans 0..25 so centres land exactly on x.5.
    image = [[255 if x % 2 == 0 else 0 for x in range(26)] for _ in range(26)]
    corners = [(0.0, 0.0), (25.0, 0.0), (25.0, 25.0), (0.0, 25.0)]
    flat = photo.sample_grid(image, corners)
    # Half-up -> x = 3, 8, 13, 18, 23 -> dark, light, dark, light, dark.
    # Round-half-even would give x = 2, 8, 12, 18, 22 -> all light.
    expected_row = (1, 0, 1, 0, 1)
    for row in range(5):
        assert flat[row * 5:(row + 1) * 5] == expected_row


def test_corner_outside_image_is_rejected():
    image = [[0] * 10 for _ in range(10)]
    with pytest.raises(photo.GroupError) as err:
        photo.sample_grid(image, [(-1.0, 0.0), (9.0, 0.0), (9.0, 9.0), (0.0, 9.0)])
    assert err.value.reason == "invalid_corners"


def test_non_convex_samples_rejected():
    image = [[0] * 10 for _ in range(10)]
    with pytest.raises(photo.GroupError) as err:
        photo.sample_grid(image, [(0.0, 0.0), (9.0, 9.0), (9.0, 0.0), (0.0, 9.0)])
    assert err.value.reason == "non_convex_corners"


def test_corner_shapes_normalized():
    pairs = [[1, 2], [3, 4], [5, 6], [7, 8]]
    objects = [{"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6}, {"x": 7, "y": 8}]
    flat = [1, 2, 3, 4, 5, 6, 7, 8]
    assert photo.normalize_corners(pairs) == photo.normalize_corners(objects)
    assert photo.normalize_corners(pairs) == photo.normalize_corners(flat)
    assert photo.normalize_corners({"corners": pairs}) == photo.normalize_corners(pairs)
    with pytest.raises(photo.GroupError):
        photo.normalize_corners(pairs[:3])


def test_image_validation():
    assert photo.parse_image([[0, 255], [10, 20]]) == [[0, 255], [10, 20]]
    with pytest.raises(photo.PhotoError):
        photo.parse_image([[0, 256]])
    with pytest.raises(photo.PhotoError):
        photo.parse_image([[-1]])
    with pytest.raises(photo.PhotoError):
        photo.parse_image([[0, 1], [2]])
    with pytest.raises(photo.PhotoError):
        photo.parse_image([[0] * 257])
    with pytest.raises(photo.PhotoError):
        photo.parse_image([])
    # Flat arrays need explicit dimensions.
    with pytest.raises(photo.PhotoError):
        photo.parse_image([0, 1, 2, 3])
    assert photo.parse_image([0, 1, 2, 3], width=2, height=2) == [[0, 1], [2, 3]]
    with pytest.raises(photo.PhotoError):
        photo.parse_image([0, 1, 2], width=2, height=2)
    # Object form.
    assert photo.parse_image({"width": 2, "height": 2, "pixels": [0, 1, 2, 3]}) == [
        [0, 1], [2, 3]
    ]


# ------------------------------------------------------------------ demo

def test_photo_demo_same_box_two_distinct_grids():
    assert photo_demo.INNER_GRID != photo_demo.OUTER_GRID
    assert grid_key(photo_demo.INNER_GRID) != grid_key(photo_demo.OUTER_GRID)
    assert sum(v is None for v in photo_demo.INNER_GRID) == 0
    assert sum(v is None for v in photo_demo.OUTER_GRID) == 2


def test_photo_demo_image_is_within_limits():
    assert len(photo_demo.IMAGE) <= photo.MAX_IMAGE_SIDE
    assert len(photo_demo.IMAGE[0]) <= photo.MAX_IMAGE_SIDE


# ------------------------------------------------------------- service

def _flat(grid):
    return tuple(v for row in grid for v in row)


def test_service_confirms_all_distinct_grids_same_box():
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    body = photo_demo.request_body()
    out = services.photo_scan(
        body["image"], body["borders"], scanner_id=photo_demo.PHOTO_SCANNER_ID
    )
    assert out["confirmed"] is True
    assert out["box_number"] == photo_demo.PHOTO_BOX_NUMBER
    assert out["box_numbers"] == [photo_demo.PHOTO_BOX_NUMBER]
    assert len(out["readings"]) == 2
    assert {r["reading_id"] for r in out["readings"]} == {"RD-0001", "RD-0002"}
    assert out["grid_keys_confirmed"] == [
        grid_key(photo_demo.OUTER_GRID),
        grid_key(photo_demo.INNER_GRID),
    ]
    assert all(g["valid"] for g in out["groups"])

    # Nothing was confirmed twice: exactly one reading per distinct grid.
    conn = connect()
    try:
        assert repo.count_readings(conn) == 2
    finally:
        conn.close()

    # Replay reuses both numbers and confirms again.
    again = services.photo_scan(body["image"], body["borders"])
    assert again["confirmed"] is True
    assert [r["reading_id"] for r in again["readings"]] == ["RD-0001", "RD-0002"]
    assert all(r["reused"] for r in again["readings"])
    assert all(g["reused"] for g in again["groups"])


def test_service_duplicate_grid_in_two_groups_confirmed_once():
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    body = photo_demo.request_body()
    inner = body["borders"][1]
    out = services.photo_scan(body["image"], [inner, dict(inner, id="copy")])
    assert out["confirmed"] is True
    assert len(out["readings"]) == 1
    assert [g["reading_id"] for g in out["groups"]] == ["RD-0001", "RD-0001"]
    conn = connect()
    try:
        assert repo.count_readings(conn) == 1
    finally:
        conn.close()


def test_service_non_convex_group_blocks_all_confirmation():
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    body = photo_demo.request_body()
    bad = {"id": "bowtie", "corners": [[40.0, 12.0], [48.0, 13.0], [10.0, 46.0], [45.0, 49.0]]}
    out = services.photo_scan(body["image"], [body["borders"][0], bad])
    assert out["confirmed"] is False
    assert out["box_number"] is None
    assert out["grid_keys_confirmed"] == []
    assert out["readings"] == []
    reasons = {(r["scope"], r["reason"]) for r in out["reasons"]}
    assert ("group", "non_convex_corners") in reasons
    # The good group is still reported, but not confirmed.
    assert out["groups"][0]["unique_hit"] is True
    assert out["groups"][0]["reading_id"] is None
    conn = connect()
    try:
        assert repo.count_readings(conn) == 0
    finally:
        conn.close()


def test_service_too_many_unreadable_blocks_all():
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    # Cover a fifth of the picture in "unreadable" gray -> too many cells.
    image = photo_demo.request_body()["image"]
    for row in image["pixels"]:
        row[0] = 120
    corners = [[0.0, 0.0], [63.0, 0.0], [63.0, 63.0], [0.0, 63.0]]
    out = services.photo_scan(image, [{"id": "mostly-dark", "corners": corners}])
    assert out["confirmed"] is False
    assert out["groups"][0]["reason"] == "too_many_unreadable"


def test_service_ambiguous_group_blocks_all():
    # Two boxes differ only where a dust block makes the cell unreadable, so
    # that group has two candidates and nothing may be confirmed.
    z = [[0] * 5 for _ in range(5)]
    services.register_box(z, box_number="BX-Z")
    b = [row[:] for row in z]
    for r, c in ((1, 1), (2, 2), (3, 3)):
        b[r][c] = 1
    services.register_box(b, box_number="BX-O")

    tag = [row[:] for row in z]
    image = photo_demo.render_quad(64, photo_demo.INNER_CORNERS, tag)
    # Paint the three differing centres as dust for this border.
    from app.photo_demo import _cell_index
    for r, c in ((1, 1), (2, 2), (3, 3)):
        sx, sy = _cell_index(photo_demo.INNER_CORNERS, r, c)
        image[sy][sx] = 120
    out = services.photo_scan(image, [{"corners": photo_demo.INNER_CORNERS}])
    assert out["confirmed"] is False
    assert out["groups"][0]["reason"] == "no_unique_hit"
    assert set(out["groups"][0]["candidate_box_numbers"]) == {"BX-Z", "BX-O"}


def test_service_groups_hitting_different_boxes_confirm_nothing():
    z = [[0] * 5 for _ in range(5)]
    b = [row[:] for row in z]
    for r, c in ((0, 0), (1, 2), (3, 4), (4, 1)):
        b[r][c] = 1
    services.register_box(z, box_number="BX-Z")
    services.register_box(b, box_number="BX-O")

    image_z = photo_demo.render_quad(64, photo_demo.INNER_CORNERS, z)
    image_b = photo_demo.render_quad(64, photo_demo.INNER_CORNERS, b)
    # Two cards side by side in one 128x64 photo.
    combined = [image_z[y] + image_b[y] for y in range(64)]
    left = [[float(x), float(y)] for x, y in photo_demo.INNER_CORNERS]
    right = [[float(x) + 64.0, float(y)] for x, y in photo_demo.INNER_CORNERS]
    out = services.photo_scan(
        combined, [{"corners": left}, {"corners": right}]
    )
    assert out["confirmed"] is False
    assert out["box_numbers"] == ["BX-O", "BX-Z"]
    assert {r["reason"] for r in out["reasons"]} == {"box_number_mismatch"}
    conn = connect()
    try:
        assert repo.count_readings(conn) == 0
    finally:
        conn.close()


def test_service_registration_after_photo_scan_sees_both_grids():
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    body = photo_demo.request_body()
    result = services.photo_scan(body["image"], body["borders"])
    assert result["confirmed"] is True

    # A new tag that only the *inner* (fully readable) photo grid hits must
    # still be rejected, and must cite that reading id.
    inner_reading = next(
        r for r in result["readings"] if r["grid_key"] == grid_key(photo_demo.INNER_GRID)
    )
    near = [row[:] for row in photo_demo.PHOTO_TAG]
    near[0][0] ^= 1
    with pytest.raises(services.ConflictError) as err:
        services.register_box(near, box_number="BX-NEAR")
    payload = err.value.payload
    assert photo_demo.PHOTO_BOX_NUMBER in payload["conflicting_box_numbers"]
    assert inner_reading["reading_id"] in payload["related_reading_ids"]
    assert payload["index_audit"]["full_reading_table_scan"] is False


def test_registration_check_covers_the_occluded_photo_grid_too():
    """After a photo scan confirms two grids, a tag that only the occluded
    outer grid hits must still be rejected by the indexed check."""
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    body = photo_demo.request_body()
    result = services.photo_scan(body["image"], body["borders"])
    outer_reading = next(
        r for r in result["readings"] if r["grid_key"] == grid_key(photo_demo.OUTER_GRID)
    )

    blocker = [row[:] for row in photo_demo.PHOTO_TAG]
    for row, col in photo_demo.DUST_CELLS:
        blocker[row][col] ^= 1
    # Sanity: the complete code alone would NOT catch this tag.
    from app.codes import match_grid
    assert match_grid(_flat(blocker), _flat(photo_demo.PHOTO_TAG)) is None

    with pytest.raises(services.ConflictError) as err:
        services.register_box(blocker, box_number="BX-BLOCKED")
    payload = err.value.payload
    kinds = {c["kind"] for c in payload["conflicts"]}
    assert kinds == {"confirmed_reading_matches_new_tag"}
    assert outer_reading["reading_id"] in payload["related_reading_ids"]
    assert payload["conflicts"][0]["match"]["visible_cells"] == 23


def test_service_group_with_no_candidate_blocks_all():
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    unknown_tag = [[1] * 5 for _ in range(5)]
    image = photo_demo.render_quad(64, photo_demo.INNER_CORNERS, unknown_tag)
    out = services.photo_scan(image, [{"id": "unknown", "corners": photo_demo.INNER_CORNERS}])
    assert out["confirmed"] is False
    assert out["groups"][0]["reason"] == "no_unique_hit"
    assert out["groups"][0]["candidate_box_numbers"] == []
    assert out["reasons"][0]["scope"] == "group"
    conn = connect()
    try:
        assert repo.count_readings(conn) == 0
    finally:
        conn.close()


def test_service_one_good_and_one_blank_group_confirms_nothing():
    services.register_box(photo_demo.PHOTO_TAG, box_number=photo_demo.PHOTO_BOX_NUMBER)
    body = photo_demo.request_body()
    # Second border spans the whole picture and is mostly background gray,
    # i.e. unreadable in every cell.
    blank = [row[:] for row in body["image"]["pixels"]]
    corners = [[0.0, 0.0], [63.0, 0.0], [63.0, 63.0], [0.0, 63.0]]
    out = services.photo_scan(blank, [body["borders"][0], {"corners": corners}])
    assert out["confirmed"] is False
    assert out["groups"][0]["reading_id"] is None
    assert out["groups"][1]["reason"] == "too_many_unreadable"
