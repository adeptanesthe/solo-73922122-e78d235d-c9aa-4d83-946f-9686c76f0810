"""Pydantic request models and /docs examples."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .codes import (
    parse_grid,
    to_matrix,
    validate_for_registration,
    validate_for_scan,
)
from .demo import DEMO_BLOCKER, DEMO_SCAN, DEMO_TAG, DEMO_VALID_NEW
from . import photo, photo_demo

Grid = list[list[Optional[int]]]
ImagePixels = list[list[int]]


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


class PhotoScanCreate(BaseModel):
    """Photo scan: 8-bit grayscale image (side <= 256) + 1..4 border groups."""

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={"example": photo_demo.request_body()},
    )

    scanner_id: Optional[str] = Field(
        default=None,
        description="扫码器标识（可选）。",
        validation_alias=AliasChoices("scanner_id", "scannerId", "scanner"),
    )
    image: Any = Field(
        description=(
            "8 位灰度图，每边长 ≤ 256。可写 {width, height, pixels} 对象（示例即此形式），"
            "或嵌套二维数组 [[灰度, ...], ...]，或扁平数组 + width/height。"
            "灰度 0～80 记黑、176～255 记白，其余记看不清。"
        ),
        validation_alias=AliasChoices(
            "image", "pixels", "gray", "grayscale", "photo", "data"
        ),
    )
    width: Optional[int] = Field(
        default=None,
        description="仅当 image 是扁平数组时需要：图像宽度（像素）。",
        validation_alias=AliasChoices("width", "image_width", "imageWidth", "cols"),
    )
    height: Optional[int] = Field(
        default=None,
        description="仅当 image 是扁平数组时需要：图像高度（像素）。",
        validation_alias=AliasChoices("height", "image_height", "imageHeight", "rows"),
    )
    borders: list[Any] = Field(
        description=(
            "1～4 组候选边框。每组四角按左上、右上、右下、左下排列，"
            "可写成 [[x,y]×4]、{corners: [...], id: ...} 或 8 个扁平数字。"
            "四角须围成严格凸四边形；坐标取整后须落在图内。"
        ),
        validation_alias=AliasChoices("borders", "quads", "candidates", "regions", "边框"),
    )

    @field_validator("image", mode="before")
    @classmethod
    def _coerce_image(cls, value: Any) -> Any:
        # Shallow shape validation only; the full pixel validation runs in the
        # service so a per-group failure still yields a structured response.
        if isinstance(value, dict):
            return value
        if not isinstance(value, (list, tuple)):
            raise ValueError("image must be a 2D grayscale array or {width,height,pixels}")
        if not value:
            raise ValueError("image must not be empty")
        return value

    @field_validator("borders", mode="before")
    @classmethod
    def _coerce_borders(cls, value: Any) -> list[Any]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("borders must be a list of 1 to 4 corner groups")
        return list(value)

    @model_validator(mode="after")
    def _validate_counts(self) -> "PhotoScanCreate":
        if not 1 <= len(self.borders) <= photo.MAX_BORDERS:
            raise ValueError(
                f"borders must contain 1 to {photo.MAX_BORDERS} groups, "
                f"got {len(self.borders)}"
            )
        return self


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

#: Built at import time; the image is deterministic (see app/photo_demo.py).
PHOTO_EXAMPLE = photo_demo.request_body()

PHOTO_EXAMPLES: dict[str, dict[str, Any]] = {
    "two_borders_two_grids_same_box": {
        "summary": "一张照片两组边框：格阵不同，但认出同一箱 → 各得一个读数编号",
        "description": (
            "需先调用 POST /api/demo/seed（Compose 启动时已自动执行），"
            "它会登记 BOX-PHOTO-01。两组边框内/外沿透视采样得到的格阵不同"
            "（外沿上多两个看不清的格），但都唯一命中 BOX-PHOTO-01，"
            "因此一次确认两条不同格阵、各给一个编号。再 Execute 一次："
            "两个格阵沿用原编号（reused=true）。"
        ),
        "value": PHOTO_EXAMPLE,
    },
    "non_convex_rejected": {
        "summary": "拒绝示例：有一组四角顺序错成蝶形（预期 confirmed=false）",
        "description": "第二组把右下与左下顺序对调，围不成凸四边形，本次不确认任何读数。",
        "value": {
            "scanner_id": "photo-gate",
            "image": PHOTO_EXAMPLE["image"],
            "borders": [
                PHOTO_EXAMPLE["borders"][0],
                {
                    "id": "bad-bowtie",
                    "corners": [
                        [40.0, 12.0],
                        [48.0, 13.0],
                        [10.0, 46.0],
                        [45.0, 49.0],
                    ],
                },
            ],
        },
    },
}
