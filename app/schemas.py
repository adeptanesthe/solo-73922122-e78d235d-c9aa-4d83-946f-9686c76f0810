"""Pydantic request models and /docs examples."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .codes import (
    parse_grid,
    to_matrix,
    validate_for_registration,
    validate_for_scan,
)
from .demo import DEMO_BLOCKER, DEMO_SCAN, DEMO_TAG, DEMO_VALID_NEW

Grid = list[list[Optional[int]]]


class BoxCreate(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {"box_number": "BOX-DEMO-BLOCKER", "grid": DEMO_BLOCKER}
        },
    )

    box_number: Optional[str] = Field(
        default=None,
        description="箱号；省略时由系统分配 BX-0001 样式编号。",
        validation_alias=AliasChoices(
            "box_number", "boxNumber", "box_no", "boxNo", "number"
        ),
    )
    grid: Grid = Field(
        description="完整的 5×5 黑白码，单元格取 0/1。也接受一维 25 格或 5 个字符串行。",
        validation_alias=AliasChoices("grid", "code", "matrix", "cells", "pattern"),
    )

    @field_validator("grid", mode="before")
    @classmethod
    def _coerce_and_validate(cls, value: Any) -> Grid:
        flat = parse_grid(value)
        validate_for_registration(flat)
        return to_matrix(flat)  # type: ignore[return-value]


class ScanCreate(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={"example": {"scanner_id": "demo-gate", "grid": DEMO_SCAN}},
    )

    scanner_id: Optional[str] = Field(
        default=None,
        description="扫码器标识（可选）。",
        validation_alias=AliasChoices("scanner_id", "scannerId", "scanner"),
    )
    grid: Grid = Field(
        description=(
            "拉正后的 5×5 格阵。看不清的格子用 null（或 '?'、'x'、-1、2），"
            "最多 4 格；可见格上某旋转朝向至多错 1 格即命中。"
        ),
        validation_alias=AliasChoices("grid", "scan", "matrix", "cells", "pattern"),
    )

    @field_validator("grid", mode="before")
    @classmethod
    def _coerce_and_validate(cls, value: Any) -> Grid:
        flat = parse_grid(value)
        validate_for_scan(flat)
        return to_matrix(flat)  # type: ignore[return-value]


# ------------------------------------------------------- /docs examples

BOX_EXAMPLES: dict[str, dict[str, Any]] = {
    "demo_blocked_by_occluded_reading": {
        "summary": "拦截示例：被一条遮挡确认读数拦下的新牌（预期 409）",
        "description": (
            "需先调用 POST /api/demo/seed。该码与旧箱完整码相差 4 格，"
            "但与那条四角遮挡读数在可见格上 0 误差。"
        ),
        "value": {"box_number": "BOX-DEMO-BLOCKER", "grid": DEMO_BLOCKER},
    },
    "valid_new_tag": {
        "summary": "可成功挂牌的差异明显新牌（预期 201）",
        "value": {"box_number": "BOX-DEMO-CHECK", "grid": DEMO_VALID_NEW},
    },
    "register_first_box": {
        "summary": "登记演示旧箱 BOX-DEMO-01",
        "value": {"box_number": "BOX-DEMO-01", "grid": DEMO_TAG},
    },
}

SCAN_EXAMPLES: dict[str, dict[str, Any]] = {
    "occluded_corners": {
        "summary": "遮挡示例：四角看不清（4 格），唯一命中并确认",
        "value": {"scanner_id": "demo-gate", "grid": DEMO_SCAN},
    },
    "clear_complete_code": {
        "summary": "清晰完整码（0 遮挡）",
        "value": {"scanner_id": "gate-7", "grid": DEMO_TAG},
    },
    "too_unreadable": {
        "summary": "非法：5 格看不清（预期 422）",
        "value": {
            "scanner_id": "gate-7",
            "grid": [
                [None, None, 1, 1, 1],
                [1, 0, 0, 0, 1],
                [1, 0, 1, 0, 1],
                [1, 0, 0, 0, 1],
                [None, None, None, 1, 1],
            ],
        },
    },
}
