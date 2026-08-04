"""视觉能力统一结果模型（domain 层，零 SDK 依赖）
============================================================================

原则：envelope 统一（found / confidence / reason / raw），payload 按能力不同。
所有坐标统一为「屏幕像素」；center 使用 tuple[int, int]（区别于 legacy dict 的 list）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GroundingResult:
    """元素定位结果（VLM ground / 本地模板匹配的统一形态）。"""

    element: str
    found: bool = False
    bbox: list[int] | None = None            # [x1, y1, x2, y2] 屏幕像素
    center: tuple[int, int] | None = None    # 屏幕像素
    confidence: float = 0.0
    selected: bool | None = None             # 已选中标记（未给出为 None）
    reason: str | None = None
    raw_response: str = ""

    @property
    def has_geometry(self) -> bool:
        """是否具备可点击的几何信息（bbox + center 齐全）。"""
        return bool(self.found and self.bbox and self.center)

    @classmethod
    def not_found(cls, element: str, reason: str | None = None,
                  raw_response: str = "") -> "GroundingResult":
        return cls(element=element, found=False, reason=reason, raw_response=raw_response)


@dataclass
class VisualQueryResult:
    """LLM 问答结果（文本 / 严格结构化 / 页面分类统一 envelope）。"""

    raw_response: str = ""
    success: bool = True
    structured: dict[str, Any] | None = None   # 严格结构化解析结果（如 JSON）
    page_type: str | None = None               # classify_page 的页面类别
    reason: str | None = None

    @property
    def is_affirmative(self) -> bool:
        """原始回复是否含肯定标记（YES）。用于「预约用车」等 YES/NO 判定。"""
        return "YES" in (self.raw_response or "").upper()


@dataclass
class TextBlock:
    """OCR 单条文本块。"""

    text: str
    bbox: list[int] | None = None              # [x1, y1, x2, y2] 屏幕像素
    confidence: float = 1.0


@dataclass
class TextExtractionResult:
    """OCR 提取结果（TextExtractor 统一形态）。"""

    blocks: list[TextBlock] = field(default_factory=list)
    success: bool = True
    reason: str | None = None

    @property
    def texts(self) -> list[str]:
        return [b.text for b in self.blocks]

    def contains(self, text: str) -> bool:
        """是否包含给定文本（子串匹配，用于「预约用车」等锚点检测）。"""
        return any(text in b.text for b in self.blocks)


@dataclass
class RoiStateResult:
    """小图 ROI 状态判定结果（勾选框等）。"""

    state: str = "unknown"                     # checked / unchecked / unknown
    confidence: float = 0.0
    reason: str | None = None


@dataclass
class MatchResult:
    """图像比对 / 位移估计结果（ImageMatcher 统一形态）。"""

    success: bool = False
    offset_y: int | None = None                # 纵向位移（像素，正=向下滚动）
    overlap_ratio: float = 0.0                 # 相邻帧内容重叠比例
    changed_ratio: float = 0.0                 # 变化像素比例
    confidence: float = 0.0
    reason: str | None = None
