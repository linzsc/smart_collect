"""全选勾选框领域模型与空间关系校验
============================================================================

纯逻辑，不依赖任何 SDK / 平台代码。

设计要点（SEL-01）：
- 「定位目标」与「判断状态」拆开；
- 只检查「全选/全选经济」文字右侧同一行的主勾选框；
- 已勾选也必须返回真实 checkbox_bbox，禁止全零；
- 结构化结果缺失 → UNKNOWN，不做自然语言兜底。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CheckboxState(str, Enum):
    CHECKED = "checked"
    UNCHECKED = "unchecked"
    UNKNOWN = "unknown"


@dataclass
class SelectAllTarget:
    """定位「全选」主勾选框的结构化结果。"""

    target_found: bool
    target_label: str
    label_bbox: list[int] | None = None          # [x1, y1, x2, y2] 屏幕像素
    checkbox_bbox: list[int] | None = None       # [x1, y1, x2, y2] 屏幕像素（真实值，即使已勾选）
    checkbox_center: tuple[int, int] | None = None
    relation_valid: bool = False
    state: str = CheckboxState.UNKNOWN.value     # checked / unchecked / unknown
    reason: str = ""


# ---------------------------------------------------------------------------
# 空间关系校验（label 与 checkbox 必须同行、checkbox 在 label 右侧）
# ---------------------------------------------------------------------------

def validate_select_all_relation(
    label_bbox: list[int] | None,
    checkbox_bbox: list[int] | None,
    screen_size: tuple[int, int] | None = None,
    expected_region: list[int] | None = None,
) -> tuple[bool, str]:
    """校验 checkbox 与 label 的空间关系。

    Args:
        label_bbox: 文字区域 [x1, y1, x2, y2]。
        checkbox_bbox: 勾选框 [x1, y1, x2, y2]。
        screen_size: (w, h)，非 None 时校验越界。
        expected_region: [x1, y1, x2, y2]，非 None 时要求勾选框中心位于区域内。

    Returns:
        (is_valid, reason)
    """
    if not label_bbox or len(label_bbox) != 4:
        return False, "label_bbox 缺失"
    if not checkbox_bbox or len(checkbox_bbox) != 4:
        return False, "checkbox_bbox 缺失"

    lx1, ly1, lx2, ly2 = label_bbox
    cx1, cy1, cx2, cy2 = checkbox_bbox

    # 尺寸合理（普通勾选框范围）
    cw, ch = cx2 - cx1, cy2 - cy1
    if cw < 10 or ch < 10:
        return False, f"勾选框过小 {cw}x{ch}"
    if cw > 300 or ch > 300:
        return False, f"勾选框过大 {cw}x{ch}"

    # 在 label 右侧（允许少量重叠，但主体必须在右侧）
    if cx1 < lx2 - cw * 0.5:
        return False, "勾选框不在文字右侧"

    # 同一行：垂直中心接近
    label_cy = (ly1 + ly2) / 2.0
    check_cy = (cy1 + cy2) / 2.0
    row_tol = max(ly2 - ly1, ch) * 0.8
    if abs(label_cy - check_cy) > row_tol:
        return False, "勾选框与文字不在同一行"

    # 屏幕边界
    if screen_size is not None:
        sw, sh = screen_size
        if cx2 > sw or cy2 > sh or cx1 < 0 or cy1 < 0:
            return False, "勾选框越出屏幕"

    # 预期区域（勾选框中心必须落入）
    if expected_region:
        ex1, ey1, ex2, ey2 = expected_region
        ccx, ccy = (cx1 + cx2) / 2.0, (cy1 + cy2) / 2.0
        if not (ex1 <= ccx <= ex2 and ey1 <= ccy <= ey2):
            return False, "勾选框不在预期区域"

    return True, "OK"
