"""视觉能力 Protocol（domain 层，实现可替换）
============================================================================

- 面向任务语义定义，不按实现技术（VLM/OCR/OpenCV）划分。
- domain 不导入任何 SDK；图像输入统一为「文件路径」或文档标注的类型。
- 现有裸 dict 客户端通过 infrastructure/vision/adapters.py 适配成本层接口。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from collector.domain.vision.models import (
    GroundingResult,
    MatchResult,
    RoiStateResult,
    TextExtractionResult,
    VisualQueryResult,
)


@runtime_checkable
class GroundingService(Protocol):
    """元素定位：截图 + 自然语言描述 → 屏幕坐标（含 bbox/center）。"""

    def ground(
        self,
        image_path: str,
        element_desc: str,
        screen_w: int,
        screen_h: int,
        ref_image: str | None = None,
        ref_images: list[str] | None = None,
    ) -> GroundingResult: ...


@runtime_checkable
class VisualQueryService(Protocol):
    """LLM 问答：纯文本 / 严格结构化 / 页面分类。"""

    def query_text(self, image_path: str, prompt: str) -> VisualQueryResult: ...

    def query_structured(
        self,
        image_path: str,
        system_prompt: str,
        user_prompt: str,
    ) -> VisualQueryResult: ...

    def classify_page(self, image_path: str) -> VisualQueryResult: ...


@runtime_checkable
class TextExtractor(Protocol):
    """本地 OCR：截图中提取文本块（实现待落地，CAP-02）。"""

    def extract(self, image_path: str) -> TextExtractionResult: ...


@runtime_checkable
class RoiClassifier(Protocol):
    """小图 ROI 状态判定（勾选框等）。

    Args:
        image: PIL.Image（已裁剪的 ROI 小图）。
    """

    def classify_roi(self, image: Any) -> RoiStateResult: ...


@runtime_checkable
class ImageMatcher(Protocol):
    """图像比对 / 位移估计（OpenCV 实现待落地，CAP-02/03）。"""

    def match(self, image_a_path: str, image_b_path: str) -> MatchResult: ...
