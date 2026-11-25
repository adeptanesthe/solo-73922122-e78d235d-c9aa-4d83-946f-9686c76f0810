"""Concurrency invariants.

When registrations and scans arrive simultaneously:

* a confirmed reading is always pinned to its original box;
* no state may exist where a confirmed reading geometrically hits a box
  other than its pinned one (registration must be rejected instead);
* identical concurrent scans produce exactly one reading number.
"""
from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import uvicorn

import app.repository as repo
import app.services as services
from app.codes import grid_key, is_hit, parse_grid
from app.db import connect
from app.main import app

ZERO = [[0] * 5 for _ in range(5)]
UNKNOWN_POSITIONS = [0, 6, 18, 24]


def _occluded():
    g = [row[:] for row in ZERO]
    for p in UNKNOWN_POSITIONS:
        r, c = divmod(p, 5)
        g[r][c] = None
    return g


def _blocker():
    g = [row[:] for row in ZERO]
    for p in UNKNOWN_POSITIONS:
        r, c = divmod(p, 5)
        g[r][c] = 1
    return g


def assert_global_invariant():
    conn = connect()
    try:
        boxes = {r["box_number"]: repo.flat_of_box(r) for r in repo.list_boxes(conn)}
        readings = repo.list_readings(conn, limit=10_000_000)
        for rd in readings:
            g = repo.flat_of_reading(rd)
            for bn, tag in boxes.items():
                if is_hit(g, tag):
                    assert bn == rd["box_number"], (
                        f"reading {rd['reading_no']} pinned to {rd['box_number']} "
                        f"also hits {bn}"
                    )
    finally:
        conn.close()


def test_service_level_registration_scan_race():
    services.register_box(ZERO, box_number="BX-A")
    scan = _occluded()
    blocker = _blocker()
    barrier = threading.Barrier(12)
    results: list[dict] = []
    errors: list[BaseException] = []

    def scan_worker(i):
        barrier.wait()
        try:
            results.append(services.submit_scan(scan, scanner_id=f"s{i}"))
        except BaseException as e:  # pragma: no cover - failure reporting
            errors.append(e)

    def register_worker(i):
        barrier.wait()
        try:
            services.register_box(blocker, box_number=f"BX-RACE-{i}")
            results.append({"registered": f"BX-RACE-{i}"})
        except services.ConflictError:
            results.append({"rejected": i})
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = []
        for i in range(6):
            futs.append(pool.submit(register_worker, i))
        for i in range(6):
            futs.append(pool.submit(scan_worker, i))
        for f in futs:
            f.result(timeout=30)

    assert not errors, errors
    assert_global_invariant()

    key = grid_key(parse_grid(scan))
    conn = connect()
    try:
        row = repo.find_reading_by_grid_key(conn, key)
        race_boxes = [
            r["box_number"] for r in repo.list_boxes(conn)
            if r["box_number"].startswith("BX-RACE-")
        ]
    finally:
        conn.close()

    if row is not None:
        # Scan won the race: every registration was rejected, no RACE box.
        assert row["box_number"] == "BX-A"
        assert race_boxes == []
        confirmed_scans = [r for r in results if r.get("confirmed")]
        assert confirmed_scans, results
        assert {r["reading_id"] for r in confirmed_scans} == {row["reading_no"]}
        # Rescans stay pinned forever.
        again = services.submit_scan(scan)
        assert again["reading_id"] == row["reading_no"]
        assert again["candidate_box_numbers"] == ["BX-A"]
        assert again["pinned"] is True
    else:
        # Registration won: at least one RACE box exists and the scan was
        # always ambiguous (never confirmed against the wrong box).
        assert race_boxes
        scan_results = [r for r in results if "candidate_box_numbers" in r]
        assert scan_results
        for r in scan_results:
            assert r["confirmed"] is False
            assert r["reading_id"] is None


def test_concurrent_identical_scans_create_one_reading():
    services.register_box(ZERO, box_number="BX-A")
    barrier = threading.Barrier(16)
    ids: list[str | None] = []

    def worker():
        barrier.wait()
        ids.append(services.submit_scan(ZERO)["reading_id"])

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(worker) for _ in range(16)]
        for f in futs:
            f.result(timeout=30)

    assert set(ids) == {"RD-0001"}
    conn = connect()
    try:
        assert repo.count_readings(conn) == 1
    finally:
        conn.close()
    assert_global_invariant()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_level_simultaneous_registration_and_scans():
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", workers=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started

        base = f"http://127.0.0.1:{port}"
        r = httpx.post(f"{base}/api/boxes", json={"box_number": "BX-A", "grid": ZERO}, timeout=10)
        assert r.status_code == 201, r.text

        scan_body = {"grid": _occluded()}
        blocker_body = _blocker()
        barrier = threading.Barrier(14)
        statuses: list[tuple[str, int, dict]] = []

        def reg(i):
            barrier.wait()
            resp = httpx.post(
                f"{base}/api/boxes",
                json={"box_number": f"BX-H{i}", "grid": blocker_body},
                timeout=30,
            )
            statuses.append(("reg", resp.status_code, resp.json()))

        def scan(i):
            barrier.wait()
            resp = httpx.post(f"{base}/api/scans", json=scan_body, timeout=30)
            statuses.append(("scan", resp.status_code, resp.json()))

        with ThreadPoolExecutor(max_workers=14) as pool:
            futs = [pool.submit(reg, i) for i in range(7)]
            futs += [pool.submit(scan, i) for i in range(7)]
            for f in futs:
                f.result(timeout=40)

        # No server errors anywhere.
        assert all(code < 500 for _, code, _ in statuses), statuses

        conn = connect()
        try:
            row = repo.find_reading_by_grid_key(conn, grid_key(parse_grid(_occluded())))
            h_boxes = [
                x["box_number"] for x in repo.list_boxes(conn)
                if x["box_number"].startswith("BX-H")
            ]
        finally:
            conn.close()

        if row is not None:
            assert row["box_number"] == "BX-A"
            assert h_boxes == []
            for kind, code, body in statuses:
                if kind == "reg":
                    assert code == 409
                    assert row["reading_no"] in body["related_reading_ids"]
                else:
                    if body.get("confirmed"):
                        assert body["box_number"] == "BX-A"
                        assert body["reading_id"] == row["reading_no"]
        else:
            assert h_boxes
            for kind, code, body in statuses:
                if kind == "scan":
                    assert body["confirmed"] is False

        assert_global_invariant()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
