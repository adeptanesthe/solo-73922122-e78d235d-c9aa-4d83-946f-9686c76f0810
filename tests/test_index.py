"""The registration conflict check must use the chunk index, not scan all
confirmed readings -- and the index must remain complete at scale."""
from __future__ import annotations

import random
import re
import time

import app.repository as repo
import app.services as services
from app.codes import grid_key, is_hit, rotate
from app.db import connect, immediate_tx


def test_chunk_lookup_query_plan_uses_index():
    conn = connect()
    try:
        rows = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT reading_id FROM reading_chunks WHERE chunk_index = ? AND bits = ?",
            (3, "0101"),
        ).fetchall()
        plan = " | ".join(r[-1] for r in rows)
        assert "SCAN reading_chunks" not in plan, plan
        assert "SEARCH reading_chunks" in plan or "USING INDEX" in plan, plan
    finally:
        conn.close()


def test_registration_never_calls_list_readings(monkeypatch):
    services.register_box([[0] * 5 for _ in range(5)], box_number="BX-A")

    def _boom(*a, **k):
        raise AssertionError("registration must not full-list readings")

    monkeypatch.setattr(repo, "list_readings", _boom)
    r = services.register_box(
        [[1, 0, 1, 0, 1]] * 5, box_number="BX-B"
    )
    assert r["created"] is True


def _seed_scale_dataset(n: int, seed: int = 7):
    rng = random.Random(seed)
    conn = connect()
    try:
        with immediate_tx(conn):
            for i in range(n):
                tag = tuple(rng.randrange(2) for _ in range(25))
                bn = f"BX-S{i:05d}"
                repo.insert_box(conn, bn, tag)
                scan: list[int | None] = list(rotate(tag, rng.randrange(4)))
                for p in rng.sample(range(25), rng.randrange(5)):
                    scan[p] = None
                repo.insert_reading(
                    conn,
                    reading_no=f"RD-SCALE-{i:06d}",
                    grid_key=grid_key(scan),
                    flat=scan,
                    box_number=bn,
                    orientation=0,
                    mismatches=0,
                    visible=sum(v is not None for v in scan),
                    scanner_id=None,
                )
    finally:
        conn.close()


def test_indexed_conflict_check_matches_bruteforce_at_scale():
    n = 1200
    _seed_scale_dataset(n)

    # Brute-force oracle (test-only full scan).
    conn = connect()
    try:
        boxes = {r["box_number"]: repo.flat_of_box(r) for r in repo.list_boxes(conn)}
        all_readings = repo.list_readings(conn, limit=1000000)
        reading_grids = [(r["reading_no"], r["box_number"], repo.flat_of_reading(r))
                         for r in all_readings]
    finally:
        conn.close()

    rng = random.Random(424242)
    started = time.perf_counter()
    extra_boxes: dict[str, tuple] = {}
    for t in range(25):
        tag = tuple(rng.randrange(2) for _ in range(25))
        all_boxes = {**boxes, **extra_boxes}

        expected_box_conflict = any(is_hit(tag, stored) for stored in all_boxes.values())
        expected_reading_conflicts = [
            (rno, bn) for rno, bn, g in reading_grids if is_hit(g, tag)
        ]
        expected_reject = expected_box_conflict or bool(expected_reading_conflicts)

        if expected_box_conflict:
            continue

        if expected_reject:
            try:
                services.register_box(tag, box_number=f"BX-NEW{t}")
                raise AssertionError("expected conflict")
            except services.ConflictError as e:
                assert e.payload["index_audit"]["full_reading_table_scan"] is False
                got = set(e.payload["related_reading_ids"])
                want = {rno for rno, _ in expected_reading_conflicts}
                # Every true conflict must be reported (no false negatives).
                assert want <= got, (want - got)
        else:
            out = services.register_box(tag, box_number=f"BX-NEW{t}")
            assert out["index_audit"]["confirmed_readings_total"] == n
            extra_boxes[f"BX-NEW{t}"] = tag
    elapsed = time.perf_counter() - started
    # Generous bound; indexed path should be far below this for 1.2k rows.
    assert elapsed < 10.0, f"indexed checks too slow: {elapsed:.2f}s"


def test_registration_sql_never_unconditionally_reads_reading_rows(monkeypatch):
    statements: list[str] = []
    original_connect = services.repo_connect

    def traced_connect():
        c = original_connect()
        c.set_trace_callback(lambda sql: statements.append(sql))
        return c

    monkeypatch.setattr(services, "repo_connect", traced_connect)
    services.register_box([[0] * 5 for _ in range(5)], box_number="BX-A")
    services.submit_scan([[0] * 5 for _ in range(5)])
    statements.clear()

    try:
        services.register_box([1] + [0] * 24, box_number="BX-B")
    except services.ConflictError:
        pass

    reading_row_selects = [
        s for s in statements
        if re.search(r"\bFROM readings\b", s, re.IGNORECASE)
        and "COUNT(" not in s.upper()
    ]
    for s in reading_row_selects:
        assert re.search(r"WHERE", s, re.IGNORECASE), s
        assert re.search(r"\bid\s+IN\b|\bid =|\bbox_number =|\bgrid_key =", s, re.IGNORECASE), s
