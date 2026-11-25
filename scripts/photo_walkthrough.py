#!/usr/bin/env python3
"""Dependency-free walkthrough of the photo recognition demo over HTTP.

Registers the photo demo box (idempotent), then submits one 64x64 grayscale
picture with two border quadrilaterals.  The two perspective samples differ,
yet both identify the same box; both readings are confirmed with their own
numbers.  Replaying the picture reuses those numbers.

Usage:
    python3 scripts/photo_walkthrough.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

# Keep these constants in sync with app/photo_demo.py.
PHOTO_TAG = [
    [1, 0, 0, 1, 0],
    [0, 1, 0, 0, 0],
    [0, 0, 1, 0, 1],
    [1, 0, 0, 0, 0],
    [0, 0, 1, 0, 1],
]
OUTER = [[11.0, 9.0], [47.0, 12.0], [44.0, 48.0], [9.0, 45.0]]
INNER = [[12.0, 10.0], [48.0, 13.0], [45.0, 49.0], [10.0, 46.0]]
DUST = ((2, 1), (1, 3))
SIZE = 64


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def homography(corners):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = corners
    dx1, dx2, dx3 = x1 - x2, x3 - x2, x0 - x1 + x2 - x3
    dy1, dy2, dy3 = y1 - y2, y3 - y2, y0 - y1 + y2 - y3
    if abs(dx3) < 1e-12 and abs(dy3) < 1e-12:
        return (x1 - x0, x3 - x0, x0, y1 - y0, y3 - y0, y0, 0.0, 0.0)
    denom = dx1 * dy2 - dx2 * dy1
    g = (dx3 * dy2 - dx2 * dy3) / denom
    h = (dx1 * dy3 - dx3 * dy1) / denom
    a = (x1 - x0) + g * x1
    b = (x3 - x0) + h * x3
    d = (y1 - y0) + g * y1
    e = (y3 - y0) + h * y3
    return (a, b, x0, d, e, y0, g, h)


def invert3(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]


def matrix(corners):
    a, b, c, d, e, f, g, h = homography(corners)
    return [[a, b, c], [d, e, f], [g, h, 1.0]]


def cell_index(corners, row, col):
    a, b, c, d, e, f, g, h = homography(corners)
    u, v = (col + 0.5) / 5, (row + 0.5) / 5
    w = g * u + h * v + 1
    return int((a * u + b * v + c) / w + 0.5), int((d * u + e * v + f) / w + 0.5)


def build_image():
    m = invert3(matrix(OUTER))
    pixels = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            w = m[2][0] * x + m[2][1] * y + m[2][2]
            u = (m[0][0] * x + m[0][1] * y + m[0][2]) / w
            v = (m[1][0] * x + m[1][1] * y + m[1][2]) / w
            if 0 <= u < 1 and 0 <= v < 1:
                bit = PHOTO_TAG[min(4, int(v * 5))][min(4, int(u * 5))]
                row.append(20 if bit else 235)  # registry bit 1 = black pixel
            else:
                row.append(170)
        pixels.append(row)
    for r, c in DUST:
        sx, sy = cell_index(OUTER, r, c)
        for dx in (0, 1):
            for dy in (0, 1):
                if 0 <= sx - 1 + dx < SIZE and 0 <= sy - 1 + dy < SIZE:
                    pixels[sy - 1 + dy][sx - 1 + dx] = 128
    return pixels


def main() -> int:
    status, health = call("GET", "/health")
    print(f"[health] {status} {health.get('stats')}")

    status, seed = call("POST", "/api/demo/seed", {})
    assert status == 200, (status, seed)
    print(f"[seed]   photo box = {seed['photo_demo']['seeded_box']['box_number']}")

    body = {
        "scanner_id": "photo-gate",
        "image": {"width": SIZE, "height": SIZE, "pixels": build_image()},
        "borders": [
            {"id": "card-outer", "corners": OUTER},
            {"id": "card-inner", "corners": INNER},
        ],
    }
    status, photo = call("POST", "/api/photo-scans", body)
    assert status == 200, (status, photo)
    print(f"[photo]  HTTP {status} confirmed={photo['confirmed']} box={photo['box_number']}")
    keys = photo["grid_keys_confirmed"]
    print(f"  distinct grids: {len(keys)}")
    for reading in photo["readings"]:
        print(f"  - grid {reading['grid_key']} -> {reading['reading_id']} "
              f"(unreadable={sum(v is None for row in reading['grid'] for v in row)})")
    assert photo["confirmed"] is True
    assert photo["box_number"] == "BOX-PHOTO-01"
    assert len(set(keys)) == 2

    status, again = call("POST", "/api/photo-scans", body)
    assert status == 200 and again["confirmed"] is True
    assert [r["reading_id"] for r in again["readings"]] == [
        r["reading_id"] for r in photo["readings"]
    ]
    assert all(r["reused"] for r in again["readings"])
    print(f"[replay] same numbers, reused={[r['reused'] for r in again['readings']]}")

    print("\nPHOTO WALKTHROUGH OK. Open /docs -> POST /api/photo-scans to replay.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
