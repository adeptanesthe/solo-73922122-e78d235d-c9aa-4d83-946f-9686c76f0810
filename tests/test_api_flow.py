"""End-to-end API behavior for registration, scanning, idempotency, conflicts."""
from __future__ import annotations

from app.demo import DEMO_BLOCKER, DEMO_SCAN, DEMO_TAG

ZERO = [[0] * 5 for _ in range(5)]


def flip(grid, positions):
    g = [row[:] for row in grid]
    for p in positions:
        r, c = divmod(p, 5)
        g[r][c] = 1 - g[r][c]
    return g


def occluded(positions):
    g = [row[:] for row in ZERO]
    for p in positions:
        r, c = divmod(p, 5)
        g[r][c] = None
    return g


def test_health_reports_empty_registry(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["stats"]["boxes"] == 0


def test_register_then_list_and_get(client):
    r = client.post("/api/boxes", json={"box_number": "BX-A", "grid": DEMO_TAG})
    assert r.status_code == 201, r.text
    assert r.json()["box"]["box_number"] == "BX-A"

    assert client.get("/api/boxes").json()["count"] == 1
    assert client.get("/api/boxes/BX-A").status_code == 200
    assert client.get("/api/boxes/NOPE").status_code == 404


def test_auto_box_number_assigned(client):
    r = client.post("/api/boxes", json={"grid": ZERO})
    assert r.status_code == 201, r.text
    assert r.json()["box"]["box_number"] == "BX-0001"


def test_duplicate_box_number_rejected(client):
    assert client.post("/api/boxes", json={"box_number": "BX-A", "grid": ZERO}).status_code == 201
    r = client.post("/api/boxes", json={"box_number": "BX-A", "grid": DEMO_TAG})
    assert r.status_code == 409
    assert r.json()["error"] == "box_number_taken"


def test_complete_code_one_bit_from_old_box_rejected(client):
    client.post("/api/boxes", json={"box_number": "BX-A", "grid": ZERO})
    near = flip(ZERO, [7])
    r = client.post("/api/boxes", json={"box_number": "BX-C", "grid": near})
    assert r.status_code == 409
    body = r.json()
    assert "BX-A" in body["conflicting_box_numbers"]
    assert body["conflicts"][0]["kind"] == "complete_code_matches_existing_box"


def test_scan_unique_hit_confirms_and_rescan_reuses_number(client):
    client.post("/api/boxes", json={"box_number": "BX-A", "grid": DEMO_TAG})

    r1 = client.post("/api/scans", json={"scanner_id": "g1", "grid": DEMO_SCAN})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["candidate_box_numbers"] == ["BX-A"]
    assert b1["confirmed"] is True
    assert b1["unique_hit"] is True
    assert b1["box_number"] == "BX-A"
    assert b1["reading_id"] == "RD-0001"
    assert b1["reused"] is False

    # Exact same grid array -> same number, marked reused + pinned.
    r2 = client.post("/api/scans", json={"scanner_id": "g1", "grid": DEMO_SCAN})
    b2 = r2.json()
    assert b2["reading_id"] == "RD-0001"
    assert b2["reused"] is True
    assert b2["pinned"] is True
    assert b2["candidate_box_numbers"] == ["BX-A"]

    readings = client.get("/api/readings").json()["readings"]
    assert len(readings) == 1
    assert client.get("/api/readings/RD-0001").status_code == 200


def test_distinct_grids_get_distinct_numbers(client):
    client.post("/api/boxes", json={"box_number": "BX-A", "grid": DEMO_TAG})
    assert client.post("/api/scans", json={"grid": DEMO_SCAN}).json()["reading_id"] == "RD-0001"

    clear = client.post("/api/scans", json={"grid": DEMO_TAG}).json()
    assert clear["confirmed"] and clear["reading_id"] == "RD-0002"

    # Rescan first grid still returns its own original number.
    again = client.post("/api/scans", json={"grid": DEMO_SCAN}).json()
    assert again["reading_id"] == "RD-0001"
    assert again["box_number"] == "BX-A"


def test_rotation_tolerant_matching_over_api(client):
    from app.codes import rotate

    client.post("/api/boxes", json={"box_number": "BX-A", "grid": DEMO_TAG})
    rotated = [
        list(rotate([c for row in DEMO_TAG for c in row], 2)[i * 5:(i + 1) * 5])
        for i in range(5)
    ]
    body = client.post("/api/scans", json={"grid": rotated}).json()
    assert body["candidate_box_numbers"] == ["BX-A"]
    assert body["confirmed"] is True


def test_ambiguous_scan_returns_candidates_without_confirming(client):
    # S has four unknown positions. A = all zero; B = ones exactly in those
    # four positions, so A and B differ in 4 cells and may coexist.
    unknown_positions = [0, 6, 18, 24]
    scan = occluded(unknown_positions)
    box_b = flip(ZERO, unknown_positions)

    assert client.post("/api/boxes", json={"box_number": "BX-A", "grid": ZERO}).status_code == 201
    rr = client.post("/api/boxes", json={"box_number": "BX-B", "grid": box_b})
    assert rr.status_code == 201, rr.text

    body = client.post("/api/scans", json={"grid": scan}).json()
    assert set(body["candidate_box_numbers"]) == {"BX-A", "BX-B"}
    assert body["confirmed"] is False
    assert body["reading_id"] is None
    assert client.get("/api/readings").json()["count"] == 0


def test_more_than_four_unreadable_is_rejected(client):
    bad = occluded([0, 1, 2, 3, 4])
    r = client.post("/api/scans", json={"grid": bad})
    assert r.status_code == 422


def test_malformed_grid_shape_rejected(client):
    r = client.post("/api/scans", json={"grid": [[0, 1], [1, 0]]})
    assert r.status_code == 422
    r2 = client.post("/api/boxes", json={"grid": [0, 1, 0]})
    assert r2.status_code == 422


def test_registration_requires_complete_code(client):
    r = client.post("/api/boxes", json={"box_number": "BX-X", "grid": DEMO_SCAN})
    assert r.status_code == 422


def test_demo_occluded_reading_blocks_new_tag(client):
    seed = client.post("/api/demo/seed")
    assert seed.status_code == 200, seed.text
    seed_body = seed.json()
    reading_id = seed_body["scan_result"]["reading_id"]
    assert reading_id == "RD-0001"
    assert seed_body["scan_result"]["confirmed"] is True

    # The blocker's complete code is 4 cells away from the old tag, so it is
    # the confirmed occluded reading that must block it.
    r = client.post("/api/boxes", json={
        "box_number": "BOX-DEMO-BLOCKER",
        "grid": DEMO_BLOCKER,
    })
    assert r.status_code == 409, r.text
    body = r.json()
    assert "BOX-DEMO-01" in body["conflicting_box_numbers"]
    assert reading_id in body["related_reading_ids"]
    kinds = {c["kind"] for c in body["conflicts"]}
    assert "confirmed_reading_matches_new_tag" in kinds
    assert body["index_audit"]["full_reading_table_scan"] is False
    assert body["index_audit"]["confirmed_readings_total"] >= 1

    # The confirmed reading is still pinned to its original box.
    rescan = client.post("/api/scans", json={"grid": DEMO_SCAN}).json()
    assert rescan["reading_id"] == reading_id
    assert rescan["box_number"] == "BOX-DEMO-01"
    assert rescan["candidate_box_numbers"] == ["BOX-DEMO-01"]
    assert rescan["pinned"] is True

    # Scenario endpoint exposes the live reading id for /docs walkthrough.
    sc = client.get("/api/demo/scenario").json()
    assert sc["occluded_reading_id"] == reading_id


def test_alias_paths_work(client):
    assert client.post("/register", json={"box_number": "BX-A", "grid": ZERO}).status_code == 201
    r = client.post("/recognize", json={"grid": ZERO})
    assert r.status_code == 200
    assert r.json()["reading_id"] == "RD-0001"
