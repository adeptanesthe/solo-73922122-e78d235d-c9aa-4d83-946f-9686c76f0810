"""Business operations: box registration, scanning, conflict enforcement.

A process-wide re-entrant lock serializes check-then-act operations in the
single uvicorn worker; each operation additionally holds a SQLite
``BEGIN IMMEDIATE`` transaction, so correctness also holds if the file is
opened by another process.
"""
from __future__ import annotations

import threading
from typing import Any, Sequence

from . import repository as repo
from .codes import (
    GridError,
    grid_key,
    match_grid,
    parse_grid,
    to_matrix,
    validate_for_registration,
    validate_for_scan,
)
from .db import connect as repo_connect, immediate_tx

#: Serializes registrations/scans within this process (FastAPI sync
#: endpoints run in a thread pool).
OPERATION_LOCK = threading.RLock()


class ConflictError(Exception):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(payload.get("message", "registration conflict"))


# ------------------------------------------------------------- serialization

def box_to_dict(row: Any) -> dict[str, Any]:
    flat = repo.flat_of_box(row)
    return {
        "box_number": row["box_number"],
        "grid": to_matrix(flat),
        "grid_key": row["grid_key"],
        "created_at": row["created_at"],
    }


def reading_to_dict(row: Any) -> dict[str, Any]:
    flat = repo.flat_of_reading(row)
    return {
        "id": row["id"],
        "reading_id": row["reading_no"],
        "reading_no": row["reading_no"],
        "box_number": row["box_number"],
        "grid": to_matrix(flat),
        "grid_key": row["grid_key"],
        "orientation": row["orientation"],
        "mismatches": row["mismatches"],
        "visible_cells": row["visible"],
        "scanner_id": row["scanner_id"],
        "created_at": row["created_at"],
    }


# ------------------------------------------------------------ registration

def register_box(raw_grid: Any, box_number: str | None = None) -> dict[str, Any]:
    """Register a new box tag, enforcing both conflict rules."""
    tag = parse_grid(raw_grid)
    validate_for_registration(tag)

    with OPERATION_LOCK:
        conn = repo_connect()
        try:
            with immediate_tx(conn):
                if box_number and repo.box_number_exists(conn, box_number):
                    existing = repo.get_box(conn, box_number)
                    payload = {
                        "error": "box_number_taken",
                        "message": f"box number {box_number!r} is already registered",
                        "conflicting_box_numbers": [box_number],
                        "related_reading_ids": [
                            r["reading_no"] for r in repo.readings_for_box(conn, box_number)
                        ],
                        "conflicts": [
                            {
                                "kind": "box_number_already_exists",
                                "box_number": box_number,
                                "existing_box": box_to_dict(existing) if existing else None,
                            }
                        ],
                    }
                    raise ConflictError(payload)

                # Rule 1: the complete new code hits an old box.
                box_code_conflicts: list[dict[str, Any]] = []
                conflicting_box_numbers: list[str] = []
                related_reading_ids: list[str] = []

                for box in repo.list_boxes(conn):
                    stored = repo.flat_of_box(box)
                    hit = match_grid(tag, stored)
                    if hit is not None:
                        bn = box["box_number"]
                        box_readings = repo.readings_for_box(conn, bn)
                        rids = [r["reading_no"] for r in box_readings]
                        box_code_conflicts.append(
                            {
                                "kind": "complete_code_matches_existing_box",
                                "box_number": bn,
                                "match": hit.as_dict(),
                                "reading_ids": rids,
                            }
                        )
                        conflicting_box_numbers.append(bn)
                        related_reading_ids.extend(rids)

                # Rule 2 (indexed): any *confirmed* reading hits the new tag.
                reading_conflicts: list[dict[str, Any]] = []
                readings_total = repo.count_readings(conn)
                candidate_ids = repo.candidate_reading_ids_for_tag(conn, tag)
                # Batch primary-key fetch of only index candidates.
                candidate_rows = repo.get_readings_by_ids(conn, candidate_ids)
                for reading in candidate_rows:
                    rgrid = repo.flat_of_reading(reading)
                    hit = match_grid(rgrid, tag)
                    if hit is None:
                        continue  # false positive from a chunk signature
                    bn = reading["box_number"]
                    rno = reading["reading_no"]
                    reading_conflicts.append(
                        {
                            "kind": "confirmed_reading_matches_new_tag",
                            "box_number": bn,
                            "reading_id": rno,
                            "reading_pk": reading["id"],
                            "match": hit.as_dict(),
                        }
                    )
                    if bn not in conflicting_box_numbers:
                        conflicting_box_numbers.append(bn)
                    if rno not in related_reading_ids:
                        related_reading_ids.append(rno)

                index_audit = {
                    "full_reading_table_scan": False,
                    "confirmed_readings_total": readings_total,
                    "chunk_index_candidates": len(candidate_ids),
                    "verified_conflicts": len(reading_conflicts),
                    "chunk_count": 6,
                }

                if box_code_conflicts or reading_conflicts:
                    conflicts = box_code_conflicts + reading_conflicts
                    payload = {
                        "error": "registration_conflict",
                        "message": (
                            "new tag rejected: it collides with an existing box "
                            "or a confirmed reading"
                        ),
                        "conflicting_box_numbers": conflicting_box_numbers,
                        # Convenience alias for clients expecting a single value:
                        "conflicting_box_number": (
                            conflicting_box_numbers[0] if conflicting_box_numbers else None
                        ),
                        "related_reading_ids": related_reading_ids,
                        "related_reading_id": (
                            related_reading_ids[0] if related_reading_ids else None
                        ),
                        "conflicts": conflicts,
                        "index_audit": index_audit,
                    }
                    raise ConflictError(payload)

                if not box_number:
                    box_number = repo.next_box_number(conn)
                repo.insert_box(conn, box_number, tag)
                row = repo.get_box(conn, box_number)
                assert row is not None
                box_data = box_to_dict(row)
                return {
                    "box": box_data,
                    "created": True,
                    # Flat aliases for clients that read top-level fields:
                    "box_number": box_number,
                    "grid_key": box_data["grid_key"],
                    "grid": box_data["grid"],
                    "index_audit": index_audit,
                }
        finally:
            conn.close()


# ---------------------------------------------------------------- scanning

def submit_scan(raw_grid: Any, scanner_id: str | None = None) -> dict[str, Any]:
    scan = parse_grid(raw_grid)
    validate_for_scan(scan)
    key = grid_key(scan)

    with OPERATION_LOCK:
        conn = repo_connect()
        try:
            with immediate_tx(conn):
                # Idempotent rescan: the exact same grid array always reuses
                # its reading number and stays pinned to its original box.
                existing = repo.find_reading_by_grid_key(conn, key)
                if existing is not None:
                    data = reading_to_dict(existing)
                    return {
                        "grid_key": key,
                        "candidate_box_numbers": [existing["box_number"]],
                        "candidates": [existing["box_number"]],
                        "box_numbers": [existing["box_number"]],
                        "matches": [
                            {
                                "box_number": existing["box_number"],
                                "orientation": existing["orientation"],
                                "mismatches": existing["mismatches"],
                                "visible_cells": existing["visible"],
                                "pinned": True,
                            }
                        ],
                        "unique_hit": True,
                        "confirmed": True,
                        "pinned": True,
                        "reused": True,
                        "box_number": existing["box_number"],
                        "reading_id": existing["reading_no"],
                        "reading_pk": existing["id"],
                        "reading": data,
                    }

                matches: list[dict[str, Any]] = []
                for box in repo.list_boxes(conn):
                    stored = repo.flat_of_box(box)
                    hit = match_grid(scan, stored)
                    if hit is not None:
                        matches.append({"box_number": box["box_number"], **hit.as_dict()})

                matches.sort(key=lambda m: (m["mismatches"], m["box_number"]))
                candidate_numbers = sorted({m["box_number"] for m in matches})

                if len(candidate_numbers) != 1:
                    return {
                        "grid_key": key,
                        "candidate_box_numbers": candidate_numbers,
                        "candidates": candidate_numbers,
                        "box_numbers": candidate_numbers,
                        "matches": matches,
                        "unique_hit": len(candidate_numbers) == 1,
                        "confirmed": False,
                        "pinned": False,
                        "reused": False,
                        "box_number": None,
                        "reading_id": None,
                        "reading_pk": None,
                        "reading": None,
                    }

                box_number = candidate_numbers[0]
                best = min(
                    (m for m in matches if m["box_number"] == box_number),
                    key=lambda m: m["mismatches"],
                )
                next_pk, reading_no = repo.next_reading_number(conn)
                reading_pk = repo.insert_reading(
                    conn,
                    reading_no=reading_no,
                    grid_key=key,
                    flat=scan,
                    box_number=box_number,
                    orientation=best["orientation"],
                    mismatches=best["mismatches"],
                    visible=best["visible_cells"],
                    scanner_id=scanner_id,
                )
                row = repo.get_reading_by_id(conn, reading_pk)
                assert row is not None
                return {
                    "grid_key": key,
                    "candidate_box_numbers": candidate_numbers,
                    "candidates": candidate_numbers,
                    "box_numbers": candidate_numbers,
                    "matches": matches,
                    "unique_hit": True,
                    "confirmed": True,
                    "pinned": False,
                    "reused": False,
                    "box_number": box_number,
                    "reading_id": reading_no,
                    "reading_pk": reading_pk,
                    "reading": reading_to_dict(row),
                }
        finally:
            conn.close()


# ---------------------------------------------------------------- queries

def list_boxes() -> list[dict[str, Any]]:
    conn = repo_connect()
    try:
        return [box_to_dict(r) for r in repo.list_boxes(conn)]
    finally:
        conn.close()


def get_box(box_number: str) -> dict[str, Any] | None:
    conn = repo_connect()
    try:
        row = repo.get_box(conn, box_number)
        return box_to_dict(row) if row else None
    finally:
        conn.close()


def list_readings(limit: int = 200) -> list[dict[str, Any]]:
    conn = repo_connect()
    try:
        return [reading_to_dict(r) for r in repo.list_readings(conn, limit=limit)]
    finally:
        conn.close()


def get_reading(identifier: str) -> dict[str, Any] | None:
    conn = repo_connect()
    try:
        row = repo.get_reading_by_no(conn, identifier)
        if row is None and identifier.isdigit():
            row = repo.get_reading_by_id(conn, int(identifier))
        return reading_to_dict(row) if row else None
    finally:
        conn.close()


def index_stats() -> dict[str, Any]:
    conn = repo_connect()
    try:
        return repo.chunk_index_stats(conn)
    finally:
        conn.close()
