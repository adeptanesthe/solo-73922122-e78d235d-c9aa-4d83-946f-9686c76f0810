#!/usr/bin/env python3
"""Fire registrations and scans simultaneously at the API and verify the
outcome branch is consistent (no 5xx; confirmed readings stay unique/pinned).

Usage:
    python3 scripts/race_check.py [base_url] [workers]
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 16
STAMP = str(int(time.time() * 1000))[-7:]

UNKNOWN = [0, 6, 18, 24]
ZERO = [[0] * 5 for _ in range(5)]


def grid_with(positions, value):
    g = [row[:] for row in ZERO]
    for p in positions:
        r, c = divmod(p, 5)
        g[r][c] = value
    return g


SCAN = grid_with(UNKNOWN, None)
BLOCKER = grid_with(UNKNOWN, 1)
BOX_A = f"RACE-A-{STAMP}"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main():
    status, body = call("POST", "/api/boxes", {"box_number": BOX_A, "grid": ZERO})
    assert status == 201, (status, body)
    print(f"prepared {BOX_A}")

    barrier = threading.Barrier(WORKERS)
    results = []

    def reg(i):
        barrier.wait()
        return "reg", *call("POST", "/api/boxes",
                            {"box_number": f"RACE-B-{STAMP}-{i}", "grid": BLOCKER})

    def scan(i):
        barrier.wait()
        return "scan", *call("POST", "/api/scans", {"grid": SCAN})

    n_reg = WORKERS // 2
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = []
        for i in range(n_reg):
            futures.append(pool.submit(reg, i))
        for i in range(WORKERS - n_reg):
            futures.append(pool.submit(scan, i))
        for f in cf.as_completed(futures):
            results.append(f.result())

    server_errors = [(k, s, b) for k, s, b in results if s >= 500]
    assert not server_errors, server_errors

    reg_codes = {}
    scan_confirms = []
    reading_ids = set()
    for kind, status, body in results:
        if kind == "reg":
            reg_codes[status] = reg_codes.get(status, 0) + 1
        else:
            if body.get("confirmed"):
                scan_confirms.append((body["box_number"], body["reading_id"]))
                reading_ids.add(body["reading_id"])

    print("registration status codes:", reg_codes)
    print("confirmed scans:", scan_confirms)

    if scan_confirms:
        # Scan won: exactly one reading pinned to BOX_A, all regs rejected.
        assert reading_ids == {scan_confirms[0][1]}
        assert all(bn == BOX_A for bn, _ in scan_confirms)
        assert reg_codes.get(201, 0) == 0, reg_codes
        assert reg_codes.get(409) == n_reg, reg_codes
        print("BRANCH: scan confirmed first -> every registration got 409; pinned to", BOX_A)
    else:
        # Registration won: scans were ambiguous, none confirmed.
        assert reg_codes.get(201, 0) >= 1
        print("BRANCH: registration won -> simultaneous scans stayed unconfirmed")

    # Final rescan must always pin the original box.
    status, body = call("POST", "/api/scans", {"grid": SCAN})
    print("final rescan:", status,
          {k: body.get(k) for k in ("confirmed", "pinned", "reused", "box_number", "reading_id")})
    if body.get("confirmed"):
        assert body["box_number"] == BOX_A
    print("RACE CHECK OK")


if __name__ == "__main__":
    main()
