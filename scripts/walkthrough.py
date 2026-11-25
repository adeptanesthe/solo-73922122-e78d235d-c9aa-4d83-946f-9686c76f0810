#!/usr/bin/env python3
"""Dependency-free walkthrough of the headline scenario over HTTP.

Usage:
    python3 scripts/walkthrough.py [base_url]
    BASE_URL=http://localhost:8000 python3 scripts/walkthrough.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

DEMO_SCAN = [
    [None, 1, 1, 1, None],
    [1, 0, 0, 0, 1],
    [1, 0, 1, 0, 1],
    [1, 0, 0, 0, 1],
    [None, 1, 1, 1, None],
]
DEMO_BLOCKER = [
    [0, 1, 1, 1, 0],
    [1, 0, 0, 0, 1],
    [1, 0, 1, 0, 1],
    [1, 0, 0, 0, 1],
    [0, 1, 1, 1, 0],
]


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> int:
    status, health = call("GET", "/health")
    print(f"[health] {status} {health.get('stats')}")

    status, seed = call("POST", "/api/demo/seed", {})
    reading_id = seed["scan_result"]["reading_id"]
    print(f"[seed]   {status} box=BOX-DEMO-01 occluded_reading={reading_id}")

    status, conflict = call("POST", "/api/boxes", {
        "box_number": "BOX-DEMO-BLOCKER", "grid": DEMO_BLOCKER,
    })
    print(f"[blocker registration] HTTP {status}")
    print("  conflicting_box_numbers:", conflict.get("conflicting_box_numbers"))
    print("  related_reading_ids:   ", conflict.get("related_reading_ids"))
    print("  kinds:                 ",
          [c["kind"] for c in conflict.get("conflicts", [])])
    assert status == 409, "blocker must be rejected with 409"
    assert "BOX-DEMO-01" in conflict["conflicting_box_numbers"]
    assert reading_id in conflict["related_reading_ids"]

    status, rescan = call("POST", "/api/scans", {"grid": DEMO_SCAN})
    print(f"[rescan] HTTP {status} reading_id={rescan['reading_id']} "
          f"box={rescan['box_number']} reused={rescan['reused']} pinned={rescan['pinned']}")
    assert rescan["reading_id"] == reading_id and rescan["pinned"] is True

    print("\nWALKTHROUGH OK. Open /docs to replay interactively.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
