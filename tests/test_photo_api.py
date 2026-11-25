"""HTTP surface of the photo recognition endpoint."""
from __future__ import annotations

from app import photo_demo
from app.schemas import PHOTO_EXAMPLES


def _body():
    return PHOTO_EXAMPLES["two_borders_two_grids_same_box"]["value"]


def test_photo_endpoint_demo_two_borders_same_box(client):
    seed = client.post("/api/demo/seed")
    assert seed.status_code == 200
    photo_scenario = seed.json()["photo_demo"]
    assert photo_scenario["seeded_box"]["box_number"] == photo_demo.PHOTO_BOX_NUMBER

    r = client.post("/api/photo-scans", json=_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmed"] is True
    assert body["box_number"] == photo_demo.PHOTO_BOX_NUMBER
    assert len(body["readings"]) == 2
    assert len(body["grid_keys_confirmed"]) == 2
    assert body["grid_keys_confirmed"][0] != body["grid_keys_confirmed"][1]
    assert all(g["unique_hit"] for g in body["groups"])

    # Both confirmed readings are visible in the readings endpoints.
    listed = client.get("/api/readings").json()["readings"]
    ids = {r["reading_id"] for r in listed}
    assert {r["reading_id"] for r in body["readings"]} <= ids
    for reading in body["readings"]:
        assert client.get(f"/api/readings/{reading['reading_id']}").status_code == 200

    # Replaying the same photo reuses both numbers.
    again = client.post("/api/photo-scans", json=_body()).json()
    assert again["confirmed"] is True
    assert [r["reading_id"] for r in again["readings"]] == [
        r["reading_id"] for r in body["readings"]
    ]
    assert all(r["reused"] for r in again["readings"])


def test_photo_endpoint_non_convex_example_rejects_everything(client):
    client.post("/api/demo/seed")
    r = client.post("/api/photo-scans", json=PHOTO_EXAMPLES["non_convex_rejected"]["value"])
    assert r.status_code == 200
    body = r.json()
    assert body["confirmed"] is False
    assert body["box_number"] is None
    assert body["readings"] == []
    assert body["reasons"][0]["reason"] == "non_convex_corners"
    # No reading was written.
    assert client.get("/api/readings").json()["count"] == 1


def test_photo_endpoint_rejects_border_count_outside_1_to_4(client):
    body = _body()
    one = body["borders"][0]

    r = client.post("/api/photo-scans", json={**body, "borders": []})
    assert r.status_code == 422
    r = client.post("/api/photo-scans", json={**body, "borders": [one] * 5})
    assert r.status_code == 422


def test_photo_endpoint_rejects_bad_image_and_pixels(client):
    body = _body()
    r = client.post("/api/photo-scans", json={**body, "image": [[0] * 257]})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_photo"
    r = client.post("/api/photo-scans", json={**body, "image": [[0, 300], [1, 2]]})
    assert r.status_code == 422
    r = client.post("/api/photo-scans", json={**body, "image": "not-an-image"})
    assert r.status_code == 422


def test_photo_endpoint_accepts_flat_image_with_dimensions(client):
    client.post("/api/demo/seed")
    body = _body()
    pixels = body["image"]["pixels"]
    flat = [v for row in pixels for v in row]
    payload = {
        "scanner_id": "photo-gate",
        "image": flat,
        "width": len(pixels[0]),
        "height": len(pixels),
        "borders": body["borders"],
    }
    r = client.post("/api/photo-scans", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["confirmed"] is True
    assert len(r.json()["readings"]) == 2


def test_photo_endpoint_accepts_flat_corner_arrays(client):
    client.post("/api/demo/seed")
    body = _body()
    borders = [
        {"id": "outer", "corners": [c for point in b["corners"] for c in point]}
        for b in body["borders"]
    ]
    r = client.post("/api/photo-scans", json={**body, "borders": borders})
    assert r.status_code == 200
    assert r.json()["confirmed"] is True


def test_photo_endpoint_scenario_and_openapi(client):
    scenario = client.get("/api/demo/scenario").json()
    assert "photo_demo" in scenario
    assert scenario["photo_demo"]["expected"]["box_number"] == photo_demo.PHOTO_BOX_NUMBER

    spec = client.get("/openapi.json").json()
    assert "/api/photo-scans" in spec["paths"]
    examples = spec["paths"]["/api/photo-scans"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert "two_borders_two_grids_same_box" in examples
    assert "non_convex_rejected" in examples
    assert client.get("/docs").status_code == 200
