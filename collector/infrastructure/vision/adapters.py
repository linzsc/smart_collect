"""视觉能力统一接口的适配器层
============================================================================

把现有「裸 dict」视觉客户端适配为 domain/vision 里的 Protocol，向新代码提供
结构化结果（envelope: found / confidence / reason / raw + 能力 payload）。

只做「字段搬运 + 类型规整」，不改变底层实现；现有调用方（FlowEngine、计价采集、select_all）继续使用裸 dict 客户端，行为不受影响。
"""

from __future__ import annotations

import json
import re
from typing import Any

from collector.domain.vision.interfaces import (
    GroundingService,
    RoiClassifier,
    VisualQueryService,
)
from collector.domain.vision.models import (
    GroundingResult,
    RoiStateResult,
    VisualQueryResult,
)

# 哨兵值：legacy 客户端用 [0,0,0,0] / [0,0] 表示「无有效几何」
_ZERO_BBOX = [0, 0, 0, 0]
_ZERO_CENTER = [0, 0]


# ---------------------------------------------------------------------------
# 结果转换（纯函数，便于离线测试）
# ---------------------------------------------------------------------------

def grounding_result_from_dict(
    data: dict[str, Any],
    element: str | None = None,
) -> GroundingResult:
    """VLMGrounder.ground() / ground_with_context() 的裸 dict → GroundingResult。"""
    bbox_raw = data.get("bbox")
    center_raw = data.get("center")

    # 归一化：全零视为无有效几何
    bbox: list[int] | None = None
    if isinstance(bbox_raw, list) and len(bbox_raw) == 4 and bbox_raw != _ZERO_BBOX:
        bbox = bbox_raw

    center: tuple[int, int] | None = None
    if (isinstance(center_raw, (list, tuple)) and len(center_raw) == 2
            and list(center_raw) != _ZERO_CENTER):
        center = (int(center_raw[0]), int(center_raw[1]))

    conf = data.get("conf", 0.0)
    if not isinstance(conf, (int, float)):
        conf = 0.0

    return GroundingResult(
        element=str(data.get("element", element or "")),
        found=bool(data.get("found", False)),
        bbox=bbox,
        center=center,
        confidence=float(conf),
        selected=data.get("selected"),
        reason=data.get("reason"),
        raw_response=str(data.get("raw_response", "") or ""),
    )


def _extract_structured(raw: str) -> dict | None:
    """从原始回复中宽松提取 JSON 对象（tool_call / 代码块 / 裸 JSON）。

    原 select_all._extract_json 的集中实现：各调用点不再各自剥 JSON。
    """
    if not raw:
        return None
    candidates = [
        r'<tool_call>\s*(.*?)\s*</tool_call>',
        r'```(?:json)?\s*(\{.*?\})\s*```',
        r'(\{.*\})',
    ]
    for pat in candidates:
        m = re.search(pat, raw, re.DOTALL)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def visual_query_result_from_dict(data: dict[str, Any]) -> VisualQueryResult:
    """VLMGrounder.query_text() / query_structured() / classify_page() → VisualQueryResult。

    structured 优先取 data 自带字段；否则从 raw_response 宽松提取 JSON（无则 None）。
    """
    raw = str(data.get("raw_response", "") or "")
    structured = data.get("structured")
    if structured is None:
        structured = _extract_structured(raw)
    return VisualQueryResult(
        raw_response=raw,
        success=bool(data.get("success", True)),
        structured=structured,
        page_type=data.get("page_type"),
        reason=data.get("reason"),
    )


# ---------------------------------------------------------------------------
# VLM 适配器：VLMGrounder（裸 dict）→ GroundingService + VisualQueryService
# ---------------------------------------------------------------------------

class VLMServiceAdapter(GroundingService, VisualQueryService):
    """把 legacy 裸 dict grounder 适配为统一接口。

    Args:
        legacy: 任意具备 ground/query_text/query_structured/classify_page
                且返回裸 dict 的客户端（当前为 VLMGrounder）。
    """

    def __init__(self, legacy: Any):
        self._legacy = legacy

    # ── 耗时统计透传（供上层统一归因）──

    @property
    def api_seconds(self) -> float:
        v = getattr(self._legacy, "api_seconds", 0.0)
        return v if isinstance(v, (int, float)) else 0.0

    @property
    def api_calls(self) -> int:
        v = getattr(self._legacy, "api_calls", 0)
        return v if isinstance(v, int) else 0

    # ── GroundingService ──

    def ground(
        self,
        image_path: str,
        element_desc: str,
        screen_w: int,
        screen_h: int,
        ref_image: str | None = None,
        ref_images: list[str] | None = None,
    ) -> GroundingResult:
        data = self._legacy.ground(
            image_path,
            element_desc,
            screen_w=screen_w,
            screen_h=screen_h,
            ref_image=ref_image,
            ref_images=ref_images,
        )
        return grounding_result_from_dict(data, element=element_desc)

    # ── VisualQueryService ──

    def query_text(self, image_path: str, prompt: str) -> VisualQueryResult:
        data = self._legacy.query_text(image_path, prompt)
        return visual_query_result_from_dict(data)

    def query_structured(
        self,
        image_path: str,
        system_prompt: str,
        user_prompt: str,
    ) -> VisualQueryResult:
        data = self._legacy.query_structured(
            image_path, system_prompt, user_prompt,
        )
        return visual_query_result_from_dict(data)

    def classify_page(self, image_path: str) -> VisualQueryResult:
        data = self._legacy.classify_page(image_path)
        return visual_query_result_from_dict(data)


# ---------------------------------------------------------------------------
# 本地 ROI 分类适配器：classify_checkbox_roi → RoiClassifier
# ---------------------------------------------------------------------------

class CheckboxRoiClassifier(RoiClassifier):
    """勾选框 ROI 分类（本地蓝像素占比算法）。

    包装 collector.infrastructure.vision.checkbox.classify_checkbox_roi；
    状态确定性命中（CHECKED/UNCHECKED）置信度 1.0，无法判定 UNKNOWN 置信度 0.0。
    """

    def __init__(self, checked_min_ratio: float | None = None,
                 unchecked_max_ratio: float | None = None):
        self._checked_min = checked_min_ratio
        self._unchecked_max = unchecked_max_ratio

    def classify_roi(self, image: Any) -> RoiStateResult:
        from collector.domain.checkbox import CheckboxState
        from collector.infrastructure.vision.checkbox import classify_checkbox_roi

        if self._checked_min is None or self._unchecked_max is None:
            state = classify_checkbox_roi(image)
        else:
            state = classify_checkbox_roi(
                image,
                checked_min_ratio=self._checked_min,
                unchecked_max_ratio=self._unchecked_max,
            )
        if state is CheckboxState.UNKNOWN:
            return RoiStateResult(state=state.value, confidence=0.0,
                                  reason="blue-ratio classifier: UNKNOWN")
        return RoiStateResult(state=state.value, confidence=1.0,
                              reason="blue-ratio classifier: deterministic hit")
