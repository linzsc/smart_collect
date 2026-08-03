"""主勾选框 ROI 分类（纯本地，不调用 VLM）
============================================================================

输入必须是「目标勾选框」的小图（几十像素 ROI），不能包含其他供应商的蓝色勾选。

判定规则（蓝色像素占比）：
- 蓝色实心背景 + 白色勾   → CHECKED
- 灰色边框 + 白色内部     → UNCHECKED
- 无法可靠分类             → UNKNOWN

阈值可调；这是确定性算法，离线可测（用于 100 次成功率验证）。
"""

from __future__ import annotations

from PIL import Image

from collector.domain.checkbox import CheckboxState

# 蓝色像素判定：b 显著大于 r/g 且足够亮（高德勾选蓝）
_BLUE_MIN = 90
_BLUE_R_DELTA = 25
_BLUE_G_DELTA = 25

# 勾选框 ROI 的蓝色占比阈值
CHECKED_BLUE_MIN_RATIO = 0.15    # >= 视为已勾选
UNCHECKED_BLUE_MAX_RATIO = 0.04  # <= 视为未勾选


def crop_roi(image: Image.Image, bbox: list[int] | None) -> Image.Image:
    """按 bbox 裁剪 ROI（自动夹取到图像范围内）。"""
    if not bbox or len(bbox) != 4:
        return image
    x1, y1, x2, y2 = bbox
    w, h = image.size
    x1 = max(0, min(int(x1), w))
    y1 = max(0, min(int(y1), h))
    x2 = max(x1, min(int(x2), w))
    y2 = max(y1, min(int(y2), h))
    return image.crop((x1, y1, x2, y2))


def _blue_ratio(image: Image.Image) -> float:
    """计算图像中蓝色像素占比（0.0 ~ 1.0）。"""
    rgb = image.convert("RGB")
    pixels = list(rgb.getdata())
    total = len(pixels)
    if total == 0:
        return 0.0
    blue = 0
    for r, g, b in pixels:
        if b >= _BLUE_MIN and b > r + _BLUE_R_DELTA and b > g + _BLUE_G_DELTA:
            blue += 1
    return blue / total


def classify_checkbox_roi(
    image: Image.Image,
    checked_min_ratio: float = CHECKED_BLUE_MIN_RATIO,
    unchecked_max_ratio: float = UNCHECKED_BLUE_MAX_RATIO,
) -> CheckboxState:
    """判定单个勾选框 ROI 的状态。

    Args:
        image: 勾选框小图（ROI）。
        checked_min_ratio: 蓝色占比 >= 该值 → CHECKED。
        unchecked_max_ratio: 蓝色占比 <= 该值 → UNCHECKED。
    """
    w, h = image.size
    if w < 4 or h < 4:
        return CheckboxState.UNKNOWN

    ratio = _blue_ratio(image)
    if ratio >= checked_min_ratio:
        return CheckboxState.CHECKED
    if ratio <= unchecked_max_ratio:
        return CheckboxState.UNCHECKED
    return CheckboxState.UNKNOWN
