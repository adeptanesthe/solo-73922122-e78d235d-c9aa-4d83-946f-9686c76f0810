"""A photo scan confirms several grids at once; a concurrent registration
must still be checked against *every* one of them."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import app.repository as repo
import app.services as services
from app import photo_demo
from app.codes import grid_key, is_hit
from app.db import connect


def _blocker_tag():
    """Tag that only the *outer* (dust-occluded) photo grid hits.

    Flipping the two dust cells differs from the complete box tag in two
    visible cells, so the "complete code hits old box" rule must not fire;
    only the occluded confirmed reading can catch it.
    """
    tag = [row[:] for row in photo_demo.PHOTO_TAG]
    for row, col in photo_demo.DUST_CELLS:
        tag[row][col] ^= 1
    return tag


def _assert_global_invariant():
    conn = connect()
    try:
        boxes = {r["box_number"]: repo.flat_of_box(r) for r in repo.list_boxes(conn)}
        for reading in repo.list_readings(conn, limit=10_000_000):
            grid = repo.flat_of_reading(reading)
            for bn, tag in boxes.items():
                if is_hit(grid, tag):
                    assert bn == reading["box_number"], (
                        f"reading {reading['reading_no']} pinned to "
                        f"{reading['box_number']} also hits {bn}"
                    )
    finally:
        conn.close()


def test_registration_race_covers_every_photo_grid():
    body = photo_demo.request_body()
    blocker = _blocker_tag()
    outer_key = grid_key(photo_demo.OUTER_GRID)

    for attempt in range(6):
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM reading_chunks")
            conn.execute("DELETE FROM readings")
            conn.execute("DELETE FROM boxes")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('readings')")
            conn.execute("COMMIT")
        finally:
            conn.close()

        services.register_box(photo_demo.PHOTO_TAG, box_number="BOX-A")
        barrier = threading.Barrier(10)
        outcomes: list[dict] = []
        errors: list[BaseException] = []

        def scan_worker():
            barrier.wait()
            try:
                outcomes.append(services.photo_scan(body["image"], body["borders"]))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def register_worker(i):
            barrier.wait()
            try:
                services.register_box(blocker, box_number=f"BOX-R{attempt}-{i}")
                outcomes.append({"registered": f"BOX-R{attempt}-{i}"})
            except services.ConflictError as exc:
                outcomes.append({"rejected": exc.payload})
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(register_worker, i) for i in range(5)]
            futures += [pool.submit(scan_worker) for _ in range(5)]
            for future in futures:
                future.result(timeout=60)

        assert not errors, errors
        _assert_global_invariant()

        conn = connect()
        try:
            outer = repo.find_reading_by_grid_key(conn, outer_key)
            race_boxes = [
                r["box_number"] for r in repo.list_boxes(conn)
                if r["box_number"].startswith(f"BOX-R{attempt}-")
            ]
        finally:
            conn.close()

        confirmed = [o for o in outcomes if o.get("confirmed")]
        rejected = [o["rejected"] for o in outcomes if "rejected" in o]

        if outer is not None:
            # Photo scan won: every concurrent registration was rejected and
            # cited the outer (occluded) reading among its conflicts.
            assert race_boxes == []
            assert len(rejected) == 5
            for payload in rejected:
                assert outer["reading_no"] in payload["related_reading_ids"]
                assert payload["index_audit"]["full_reading_table_scan"] is False
            assert confirmed
        else:
            # Registration won: the outer photo grid now has two candidates,
            # so the photo scan confirms nothing at all.
            assert race_boxes
            for out in confirmed:
                assert out["confirmed"] is False, out
            for out in outcomes:
                if "confirmed" in out:
                    assert out["confirmed"] is False
                    reasons = {r["reason"] for r in out["reasons"]}
                    assert "no_unique_hit" in reasons


def test_simultaneous_photo_scans_write_each_grid_once():
    services.register_box(photo_demo.PHOTO_TAG, box_number="BOX-A")
    body = photo_demo.request_body()
    barrier = threading.Barrier(12)
    results: list[dict] = []

    def worker():
        barrier.wait()
        results.append(services.photo_scan(body["image"], body["borders"]))

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(worker) for _ in range(12)]
        for future in futures:
            future.result(timeout=60)

    assert all(r["confirmed"] for r in results)
    number_sets = {frozenset(r["reading_id"] for r in out["readings"]) for out in results}
    assert len(number_sets) == 1, number_sets
    assert len(next(iter(number_sets))) == 2  # two distinct grids, two numbers

    conn = connect()
    try:
        assert repo.count_readings(conn) == 2
    finally:
        conn.close()
    _assert_global_invariant()
