"""Built-in demonstration scenario.

The grids are chosen so that one *occluded, confirmed* scan blocks a new tag
while the new tag's complete code does **not** directly collide with the
old box code:

``BOX-DEMO-01`` tag (rotationally symmetric ring + center)::

    1 1 1 1 1
    1 0 0 0 1
    1 0 1 0 1
    1 0 0 0 1
    1 1 1 1 1

Occluded scan ``DEMO_SCAN``: the four corners are unreadable (``None``)::

    ? 1 1 1 ?
    1 0 0 0 1
    1 0 1 0 1
    1 0 0 0 1
    ? 1 1 1 ?

Blocker tag ``DEMO_BLOCKER``: only those same four corners are flipped::

    0 1 1 1 0
    1 0 0 0 1
    1 0 1 0 1
    1 0 0 0 1
    0 1 1 1 0

The blocker differs from the complete old tag in 4 cells (> 1), so the
"complete code hits old box" rule does not fire -- but the occluded reading
matches the blocker with 0 errors on visible cells, so the confirmed
reading blocks registration and reports its reading id.
"""
from __future__ import annotations

from typing import Any

from . import services

DEMO_BOX_NUMBER = "BOX-DEMO-01"
DEMO_SCANNER_ID = "demo-gate"

DEMO_TAG: list[list[int]] = [
    [1, 1, 1, 1, 1],
    [1, 0, 0, 0, 1],
    [1, 0, 1, 0, 1],
    [1, 0, 0, 0, 1],
    [1, 1, 1, 1, 1],
]

DEMO_SCAN: list[list[int | None]] = [
    [None, 1, 1, 1, None],
    [1, 0, 0, 0, 1],
    [1, 0, 1, 0, 1],
    [1, 0, 0, 0, 1],
    [None, 1, 1, 1, None],
]

DEMO_BLOCKER: list[list[int]] = [
    [0, 1, 1, 1, 0],
    [1, 0, 0, 0, 1],
    [1, 0, 1, 0, 1],
    [1, 0, 0, 0, 1],
    [0, 1, 1, 1, 0],
]

# A clearly different valid tag that registers successfully after the demo.
DEMO_VALID_NEW: list[list[int]] = [
    [0, 1, 0, 1, 0],
    [1, 0, 1, 0, 1],
    [0, 1, 0, 1, 0],
    [1, 0, 1, 0, 1],
    [0, 1, 0, 1, 0],
]


def scenario_payload() -> dict[str, Any]:
    """Static description plus live reading id if the seed was applied."""
    reading_id: str | None = None
    seeded_box = services.get_box(DEMO_BOX_NUMBER)
    readings = services.list_readings(limit=500)
    for r in readings:
        if _same_grid(r["grid"], DEMO_SCAN):
            reading_id = r["reading_no"]
            break
    return {
        "title": "一条遮挡读数拦下新牌 / one occluded reading blocks a new tag",
        "steps": [
            "1) POST /api/demo/seed (or set SEED_DEMO=1 at startup) registers "
            f"{DEMO_BOX_NUMBER} and confirms the occluded scan.",
            "2) Open POST /api/boxes in /docs, choose the "
            "'demo_blocked_by_occluded_reading' example, Execute.",
            "3) Expect HTTP 409 with conflicting_box_numbers="
            f"['{DEMO_BOX_NUMBER}'] and related_reading_ids=[<the reading id>].",
            "4) Replay the occluded grid at POST /api/scans: same reading id, "
            "pinned to the original box.",
        ],
        "seeded_box": seeded_box,
        "occluded_reading_id": reading_id,
        "register_body": {
            "box_number": "BOX-DEMO-BLOCKER",
            "grid": DEMO_BLOCKER,
        },
        "scan_body": {"scanner_id": DEMO_SCANNER_ID, "grid": DEMO_SCAN},
        "valid_new_box_body": {"box_number": "BOX-DEMO-CHECK", "grid": DEMO_VALID_NEW},
    }


def _same_grid(a: Any, b: Any) -> bool:
    from .codes import grid_key, parse_grid

    return grid_key(parse_grid(a)) == grid_key(parse_grid(b))


def seed_demo() -> dict[str, Any]:
    """Idempotently create the demo box + confirmed occluded reading."""
    box = services.get_box(DEMO_BOX_NUMBER)
    if box is None:
        services.register_box(DEMO_TAG, box_number=DEMO_BOX_NUMBER)

    # Submitting the same grid is idempotent: it confirms once then reuses.
    scan_result = services.submit_scan(DEMO_SCAN, scanner_id=DEMO_SCANNER_ID)
    return scenario_payload() | {
        "seeded": True,
        "scan_result": {
            "confirmed": scan_result["confirmed"],
            "box_number": scan_result["box_number"],
            "reading_id": scan_result["reading_id"],
            "reused": scan_result["reused"],
        },
    }
