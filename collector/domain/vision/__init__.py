"""domain.vision — 视觉能力统一契约（结果模型 + Protocol）

面向任务的视觉能力接口族。实现（VLM / OCR / OpenCV / 本地算法）可替换，
domain 只定义契约，不依赖任何 SDK / 平台代码。

能力划分（按任务语义，不按实现技术）：
- GroundingService    元素定位（截图 + 描述 → 屏幕坐标）
- VisualQueryService  LLM 问答（文本 / 严格结构化 / 页面分类）
- TextExtractor       OCR 文本提取（实现待落地，CAP-02）
- RoiClassifier       小图 ROI 状态判定（勾选框等）
- ImageMatcher        图像比对 / 位移估计（实现待落地，CAP-02/03）
"""
from collector.domain.vision.interfaces import (
    GroundingService,
    ImageMatcher,
    RoiClassifier,
    TextExtractor,
    VisualQueryService,
)
from collector.domain.vision.models import (
    GroundingResult,
    MatchResult,
    RoiStateResult,
    TextBlock,
    TextExtractionResult,
    VisualQueryResult,
)

__all__ = [
    "GroundingService",
    "VisualQueryService",
    "TextExtractor",
    "RoiClassifier",
    "ImageMatcher",
    "GroundingResult",
    "VisualQueryResult",
    "TextBlock",
    "TextExtractionResult",
    "RoiStateResult",
    "MatchResult",
]
