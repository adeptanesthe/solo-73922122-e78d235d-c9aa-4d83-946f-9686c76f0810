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



# ------------------------------------------------------------- photo scans

def _photo_group_report(
    index: int,
    group_id: str | None,
    *,
    corners: list[list[float]] | None = None,
    flat: Sequence[int | None] | None = None,
    reason: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Stable per-group response shape (all fields always present)."""
    return {
        "index": index,
        "id": group_id,
        "corners": corners,
        "valid": reason is None,
        "reason": reason,
        "message": message,
        "grid_key": grid_key(flat) if flat is not None else None,
        "grid": to_matrix(flat) if flat is not None else None,
        "unreadable_cells": (
            sum(1 for v in flat if v is None) if flat is not None else None
        ),
        "candidate_box_numbers": [],
        "candidates": [],
        "candidate_count": 0,
        "unique_hit": False,
        "box_number": None,
        "match": None,
        "reading_id": None,
        "reading_ids": [],
        "reused": False,
    }


def photo_scan(
    raw_image: Any,
    raw_borders: Any,
    *,
    width: int | None = None,
    height: int | None = None,
    scanner_id: str | None = None,
) -> dict[str, Any]:
    """Recognize every candidate border of one photo; confirm all or nothing.

    Per group: perspective-sample the 5x5 cell centres, classify black /
    white / unreadable, then match against the registry with the existing
    rules.  A group is usable only when it forms a strictly convex
    quadrilateral, has at most four unreadable cells, and hits exactly one
    box number.

    Nothing is confirmed if any group is unusable or if usable groups hit
    different boxes.  Otherwise every *distinct* grid is confirmed in one
    transaction -- existing grids reuse their original reading numbers --
    so a later registration check must cover all of them.
    """
    from . import photo

    image = photo.parse_image(raw_image, width=width, height=height)
    if not isinstance(raw_borders, (list, tuple)):
        raise photo.PhotoError("borders must be a list of 1 to 4 corner groups")
    if not 1 <= len(raw_borders) <= photo.MAX_BORDERS:
        raise photo.PhotoError(
            f"photo has {len(raw_borders)} border group(s); expected 1 to "
            f"{photo.MAX_BORDERS}"
        )

    groups: list[dict[str, Any]] = []
    samples: list[tuple[int, tuple[int | None, ...], dict[str, Any]]] = []
    for index, raw_group in enumerate(raw_borders):
        group_id: str | None = None
        if isinstance(raw_group, dict):
            group_id = raw_group.get("id") or raw_group.get("name")
        try:
            corners, flat = photo.extract_group(image, raw_group)
        except photo.GroupError as exc:
            groups.append(
                _photo_group_report(index, group_id, reason=exc.reason, message=exc.message)
            )
            continue
        report = _photo_group_report(
            index, group_id, corners=[list(p) for p in corners], flat=flat
        )
        try:
            photo.validate_group_readability(flat)
        except photo.GroupError as exc:
            report["reason"] = exc.reason
            report["message"] = exc.message
            report["valid"] = False
            groups.append(report)
            continue
        groups.append(report)
        samples.append((index, flat, report))

    with OPERATION_LOCK:
        conn = repo_connect()
        try:
            with immediate_tx(conn):
                box_tags = [
                    (row["box_number"], repo.flat_of_box(row))
                    for row in repo.list_boxes(conn)
                ]
                planned: list[dict[str, Any]] = []
                for index, flat, report in samples:
                    key = report["grid_key"]
                    # Existing confirmed grids are pinned: they only ever
                    # match their original box and keep their original
                    # number.  Looked up per grid key (unique-indexed).
                    pinned = repo.find_reading_by_grid_key(conn, key)
                    if pinned is not None:
                        report["candidate_box_numbers"] = [pinned["box_number"]]
                        report["candidates"] = [pinned["box_number"]]
                        report["candidate_count"] = 1
                        report["unique_hit"] = True
                        report["box_number"] = pinned["box_number"]
                        report["match"] = {
                            "box_number": pinned["box_number"],
                            "orientation": pinned["orientation"],
                            "mismatches": pinned["mismatches"],
                            "visible_cells": pinned["visible"],
                            "pinned": True,
                        }
                        planned.append({"flat": flat, "report": report, "pinned": pinned})
                        continue

                    matches = []
                    for bn, stored in box_tags:
                        hit = match_grid(flat, stored)
                        if hit is not None:
                            matches.append({"box_number": bn, **hit.as_dict()})
                    matches.sort(key=lambda m: (m["mismatches"], m["box_number"]))
                    candidates = sorted({m["box_number"] for m in matches})
                    report["candidate_box_numbers"] = candidates
                    report["candidates"] = candidates
                    report["candidate_count"] = len(candidates)
                    if not candidates:
                        report["valid"] = False
                        report["reason"] = "no_unique_hit"
                        report["message"] = "photo grid hits no registered box"
                        continue
                    if len(candidates) > 1:
                        report["valid"] = False
                        report["reason"] = "no_unique_hit"
                        report["message"] = (
                            "photo grid hits several registered boxes; cannot pick one"
                        )
                        continue
                    report["unique_hit"] = True
                    report["box_number"] = candidates[0]
                    best = min(
                        (m for m in matches if m["box_number"] == candidates[0]),
                        key=lambda m: (m["mismatches"], m["orientation"]),
                    )
                    report["match"] = best
                    planned.append({"flat": flat, "report": report, "pinned": None})

                usable = [g for g in groups if g["valid"] and g["unique_hit"]]
                reasons: list[dict[str, Any]] = []
                for report in groups:
                    if report["reason"] is not None:
                        reasons.append(
                            {
                                "scope": "group",
                                "group_index": report["index"],
                                "group_id": report["id"],
                                "reason": report["reason"],
                                "message": report["message"],
                            }
                        )
                hit_numbers = {g["box_number"] for g in usable}
                if not reasons and len(usable) == len(groups) and len(hit_numbers) != 1:
                    for report in groups:
                        reasons.append(
                            {
                                "scope": "batch",
                                "group_index": report["index"],
                                "group_id": report["id"],
                                "reason": "box_number_mismatch",
                                "message": (
                                    "groups hit different box numbers "
                                    f"{sorted(hit_numbers)}; nothing was confirmed"
                                ),
                            }
                        )
                if not reasons and len(usable) != len(groups):
                    # Defensive: every unusable planned group must have a
                    # reason; keep the response self-describing regardless.
                    for report in groups:
                        if report["reason"] is None and not report["unique_hit"]:
                            report["valid"] = False
                            report["reason"] = "no_unique_hit"
                            report["message"] = "photo grid could not be matched uniquely"
                            reasons.append(
                                {
                                    "scope": "group",
                                    "group_index": report["index"],
                                    "group_id": report["id"],
                                    "reason": report["reason"],
                                    "message": report["message"],
                                }
                            )

                can_confirm = (
                    not reasons and len(usable) == len(groups) and len(hit_numbers) == 1
                )
                if not can_confirm:
                    return {
                        "image": {"width": len(image[0]), "height": len(image)},
                        "border_count": len(groups),
                        "confirmed": False,
                        "box_number": None,
                        "box_numbers": sorted(hit_numbers),
                        "grid_keys_confirmed": [],
                        "readings": [],
                        "groups": groups,
                        "reasons": reasons,
                    }

                box_number = next(iter(hit_numbers))
                confirmed_by_key: dict[str, dict[str, Any]] = {}
                for item in planned:
                    report = item["report"]
                    key = report["grid_key"]
                    if key in confirmed_by_key:
                        # Same grid in two groups: one reading, referenced twice.
                        entry = confirmed_by_key[key]
                        report["reading_id"] = entry["reading_id"]
                        report["reading_ids"] = [entry["reading_id"]]
                        report["reused"] = entry["reused"]
                        continue

                    pinned = item["pinned"]
                    if pinned is not None:
                        if pinned["box_number"] != box_number:
                            raise ConflictError(
                                {
                                    "error": "reading_box_mismatch",
                                    "message": (
                                        f"grid {key} is already pinned to "
                                        f"{pinned['box_number']}, not {box_number}"
                                    ),
                                    "related_reading_ids": [pinned["reading_no"]],
                                }
                            )
                        row = pinned
                        reused = True
                    else:
                        best = report["match"]
                        _, reading_no = repo.next_reading_number(conn)
                        reading_pk = repo.insert_reading(
                            conn,
                            reading_no=reading_no,
                            grid_key=key,
                            flat=item["flat"],
                            box_number=box_number,
                            orientation=best["orientation"],
                            mismatches=best["mismatches"],
                            visible=best["visible_cells"],
                            scanner_id=scanner_id,
                        )
                        row = repo.get_reading_by_id(conn, reading_pk)
                        assert row is not None
                        reused = False

                    entry = {
                        "group_index": report["index"],
                        "group_id": report["id"],
                        "box_number": box_number,
                        "grid_key": key,
                        "grid": to_matrix(item["flat"]),
                        "reading_id": row["reading_no"],
                        "reading_no": row["reading_no"],
                        "reading_pk": row["id"],
                        "orientation": row["orientation"],
                        "mismatches": row["mismatches"],
                        "visible_cells": row["visible"],
                        "reused": reused,
                    }
                    confirmed_by_key[key] = entry
                    report["reading_id"] = entry["reading_id"]
                    report["reading_ids"] = [entry["reading_id"]]
                    report["reused"] = reused

                readings = list(confirmed_by_key.values())
                return {
                    "image": {"width": len(image[0]), "height": len(image)},
                    "border_count": len(groups),
                    "confirmed": True,
                    "box_number": box_number,
                    "box_numbers": [box_number],
                    "grid_keys_confirmed": list(confirmed_by_key),
                    "readings": readings,
                    "groups": groups,
                    "reasons": [],
                }
        finally:
            conn.close()
