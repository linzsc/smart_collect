"""
domain/vision 统一接口层测试（WS-2 P1）
============================================================================

验证：
  - 结果模型默认值 / 字段规整（center 为 tuple、全零几何归一化为 None）
  - 裸 dict → 结构化结果转换（ground / query_text / classify_page）
  - VLMServiceAdapter 委托 legacy 客户端 + Protocol 符合性（结构类型）
  - CheckboxRoiClassifier 本地 ROI 分类（合成图，无需设备/API）
  - domain/vision 零 SDK 依赖（无 openai/cv2/PIL 导入）

用法:
  .venv/bin/python tests/test_vision_interfaces.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PIL import Image

from collector.domain.vision import (
    GroundingResult,
    RoiStateResult,
    VisualQueryResult,
)
from collector.domain.vision.interfaces import (
    GroundingService,
    RoiClassifier,
    VisualQueryService,
)
from collector.infrastructure.vision.adapters import (
    CheckboxRoiClassifier,
    VLMServiceAdapter,
    grounding_result_from_dict,
    visual_query_result_from_dict,
)


# ======================================================================
# Suite 1: 结果模型
# ======================================================================

def test_grounding_result_defaults():
    r = GroundingResult(element="搜索框")
    assert r.found is False
    assert r.bbox is None
    assert r.center is None
    assert r.confidence == 0.0
    assert r.selected is None
    assert r.has_geometry is False
    assert r.raw_response == ""
    return "PASS ✓"


def test_grounding_result_not_found_helper():
    r = GroundingResult.not_found("x", reason="页面不对")
    assert r.found is False and r.reason == "页面不对"
    return "PASS ✓"


def test_grounding_result_geometry():
    r = GroundingResult(element="x", found=True, bbox=[10, 20, 30, 40],
                        center=(25, 35), confidence=0.9)
    assert r.has_geometry is True
    assert r.center == (25, 35)
    return "PASS ✓"


def test_text_extraction_contains():
    from collector.domain.vision import TextBlock, TextExtractionResult
    res = TextExtractionResult(blocks=[TextBlock(text="预约用车", bbox=[1, 2, 3, 4])])
    assert res.contains("预约用车") is True
    assert res.contains("出租车") is False
    assert res.texts == ["预约用车"]
    return "PASS ✓"


# ======================================================================
# Suite 2: 裸 dict → 结构化结果转换
# ======================================================================

def test_grounding_from_dict_found_with_bbox():
    data = {
        "element": "打车tab", "found": True,
        "bbox": [100, 200, 300, 400], "center": [200, 300],
        "conf": 0.9, "selected": None, "reason": None, "raw_response": "raw",
    }
    r = grounding_result_from_dict(data)
    assert r.found is True
    assert r.bbox == [100, 200, 300, 400]
    assert r.center == (200, 300)          # list → tuple
    assert r.confidence == 0.9
    assert r.has_geometry is True
    return "PASS ✓"


def test_grounding_from_dict_center_only():
    data = {"element": "问号", "found": True, "bbox": None, "center": [480, 620], "conf": 0.7}
    r = grounding_result_from_dict(data)
    assert r.center == (480, 620)
    assert r.has_geometry is False          # 无 bbox → 不可直接点击判定
    return "PASS ✓"


def test_grounding_from_dict_zero_geometry_normalized():
    """全零 bbox/center（哨兵值）归一化为 None。"""
    data = {"element": "x", "found": False, "bbox": [0, 0, 0, 0], "center": [0, 0],
            "selected": True, "reason": "已选中"}
    r = grounding_result_from_dict(data)
    assert r.bbox is None
    assert r.center is None
    assert r.found is False
    assert r.selected is True
    assert r.reason == "已选中"
    return "PASS ✓"


def test_grounding_from_dict_bad_conf():
    data = {"element": "x", "conf": "high"}   # 非数值 conf → 0.0
    r = grounding_result_from_dict(data)
    assert r.confidence == 0.0
    return "PASS ✓"


def test_visual_query_from_dict_text():
    r = visual_query_result_from_dict({"raw_response": "YES", "success": True})
    assert r.raw_response == "YES" and r.success is True
    return "PASS ✓"


def test_visual_query_from_dict_classify():
    r = visual_query_result_from_dict({"page_type": "打车页", "confidence": "high",
                                       "raw_response": "..."})
    assert r.page_type == "打车页"
    return "PASS ✓"


def test_visual_query_from_dict_api_failure():
    r = visual_query_result_from_dict({"raw_response": "Error", "success": False})
    assert r.success is False
    return "PASS ✓"


# ======================================================================
# Suite 3: VLMServiceAdapter（fake legacy，无 API）
# ======================================================================

class _FakeLegacyGrounder:
    """模拟 VLMGrounder 的裸 dict API。"""

    def __init__(self):
        self.api_seconds = 1.5
        self.api_calls = 3
        self.calls = []

    def ground(self, image_path, element_desc, **kwargs):
        self.calls.append(("ground", image_path, element_desc, kwargs))
        return {
            "element": element_desc, "found": True,
            "bbox": [50, 60, 150, 160], "center": [100, 110],
            "conf": 0.95, "selected": None, "reason": None,
            "raw_response": "ok",
        }

    def query_text(self, image_path, prompt):
        self.calls.append(("query_text", image_path, prompt))
        return {"raw_response": "NO", "success": True}

    def query_structured(self, image_path, system_prompt, user_prompt):
        self.calls.append(("query_structured", image_path, system_prompt, user_prompt))
        return {"raw_response": '{"a": 1}', "success": True}

    def classify_page(self, image_path):
        self.calls.append(("classify_page", image_path))
        return {"page_type": "路径规划页", "confidence": "high", "raw_response": "..."}


def test_vlm_adapter_conforms_to_protocols():
    adapter = VLMServiceAdapter(_FakeLegacyGrounder())
    assert isinstance(adapter, GroundingService)
    assert isinstance(adapter, VisualQueryService)
    return "PASS ✓"


def test_vlm_adapter_ground_delegates_and_converts():
    legacy = _FakeLegacyGrounder()
    adapter = VLMServiceAdapter(legacy)
    r = adapter.ground("/tmp/a.jpg", "打车tab", screen_w=1080, screen_h=2400,
                       ref_image="/tmp/ref.png")
    assert r.found is True
    assert r.center == (100, 110)
    assert r.element == "打车tab"
    name, path, desc, kwargs = legacy.calls[0]
    assert path == "/tmp/a.jpg" and desc == "打车tab"
    assert kwargs["screen_w"] == 1080 and kwargs["screen_h"] == 2400
    assert kwargs["ref_image"] == "/tmp/ref.png"
    return "PASS ✓"


def test_vlm_adapter_query_text_delegates():
    legacy = _FakeLegacyGrounder()
    adapter = VLMServiceAdapter(legacy)
    r = adapter.query_text("/tmp/a.jpg", "是否出现「预约用车」？只回答 YES 或 NO")
    assert r.raw_response == "NO" and r.success is True
    return "PASS ✓"


def test_vlm_adapter_classify_page_delegates():
    legacy = _FakeLegacyGrounder()
    adapter = VLMServiceAdapter(legacy)
    r = adapter.classify_page("/tmp/a.jpg")
    assert r.page_type == "路径规划页"
    return "PASS ✓"


def test_vlm_adapter_api_stats_passthrough():
    adapter = VLMServiceAdapter(_FakeLegacyGrounder())
    assert adapter.api_seconds == 1.5
    assert adapter.api_calls == 3
    # 无统计属性的 legacy → 0
    assert VLMServiceAdapter(object()).api_seconds == 0.0
    assert VLMServiceAdapter(object()).api_calls == 0
    return "PASS ✓"


# ======================================================================
# Suite 4: CheckboxRoiClassifier（合成图）
# ======================================================================

def test_roi_classifier_checked():
    img = Image.new("RGB", (40, 40), (20, 40, 220))   # 高德勾选蓝
    r = CheckboxRoiClassifier().classify_roi(img)
    assert isinstance(r, RoiStateResult)
    assert r.state == "checked"
    assert r.confidence == 1.0
    return "PASS ✓"


def test_roi_classifier_unchecked():
    img = Image.new("RGB", (40, 40), (210, 210, 210))  # 灰色空心
    r = CheckboxRoiClassifier().classify_roi(img)
    assert r.state == "unchecked"
    assert r.confidence == 1.0
    return "PASS ✓"


def test_roi_classifier_unknown():
    img = Image.new("RGB", (2, 2), (128, 128, 128))    # 过小 → 无法判定
    r = CheckboxRoiClassifier().classify_roi(img)
    assert r.state == "unknown"
    assert r.confidence == 0.0
    return "PASS ✓"


# ======================================================================
# Suite 4b: 结构化行为增强（WS-2 P2）
# ======================================================================

def test_visual_query_is_affirmative():
    from collector.domain.vision import VisualQueryResult
    assert VisualQueryResult(raw_response="YES，出现了蓝色预约用车").is_affirmative is True
    assert VisualQueryResult(raw_response="NO，未出现").is_affirmative is False
    assert VisualQueryResult(raw_response="yes").is_affirmative is True   # 大小写不敏感
    assert VisualQueryResult(raw_response="").is_affirmative is False
    return "PASS ✓"


def test_visual_query_structured_extracted_from_codeblock():
    r = visual_query_result_from_dict({
        "raw_response": '```json\n{"suppliers": ["曹操出行"], "economy_ended": true}\n```',
        "success": True,
    })
    assert r.structured == {"suppliers": ["曹操出行"], "economy_ended": True}, r.structured
    return "PASS ✓"


def test_visual_query_structured_extracted_from_tool_call():
    r = visual_query_result_from_dict({
        "raw_response": '<tool_call>{"name": "mobile_use", "arguments": {"action": "answer", "page_type": "打车页"}}</tool_call>',
        "success": True,
    })
    assert r.structured is not None
    assert r.structured.get("name") == "mobile_use"
    return "PASS ✓"


def test_visual_query_structured_none_when_non_json():
    r = visual_query_result_from_dict({"raw_response": "全选已经勾选了", "success": True})
    assert r.structured is None
    return "PASS ✓"


# ======================================================================
# Suite 5: domain/vision 零 SDK 依赖
# ======================================================================

def test_domain_vision_no_sdk_imports():
    """domain/vision 不得导入 openai / cv2 / PIL 等 SDK。"""
    for f in sorted(Path("collector/domain/vision").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for banned in ("import openai", "from openai",
                       "import cv2", "from cv2",
                       "from PIL", "import PIL",
                       "from collector.infrastructure"):
            assert banned not in src, f"{f} 含 SDK 依赖: {banned}"
    return "PASS ✓"


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    print("=" * 60)
    print("  Vision Interfaces Tests (WS-2 P1)")
    print("=" * 60)

    suites = [
        ("Suite 1: 结果模型", [
            ("GroundingResult 默认值", test_grounding_result_defaults),
            ("not_found 工厂", test_grounding_result_not_found_helper),
            ("has_geometry", test_grounding_result_geometry),
            ("TextExtractionResult.contains", test_text_extraction_contains),
        ]),
        ("Suite 2: 裸 dict → 结构化结果", [
            ("found + bbox/center（list→tuple）", test_grounding_from_dict_found_with_bbox),
            ("仅 center", test_grounding_from_dict_center_only),
            ("全零几何归一化", test_grounding_from_dict_zero_geometry_normalized),
            ("非数值 conf → 0.0", test_grounding_from_dict_bad_conf),
            ("query_text 转换", test_visual_query_from_dict_text),
            ("classify_page 转换", test_visual_query_from_dict_classify),
            ("API 失败转换", test_visual_query_from_dict_api_failure),
        ]),
        ("Suite 3: VLMServiceAdapter", [
            ("Protocol 符合性", test_vlm_adapter_conforms_to_protocols),
            ("ground 委托 + 转换", test_vlm_adapter_ground_delegates_and_converts),
            ("query_text 委托", test_vlm_adapter_query_text_delegates),
            ("classify_page 委托", test_vlm_adapter_classify_page_delegates),
            ("耗时统计透传", test_vlm_adapter_api_stats_passthrough),
        ]),
        ("Suite 4: CheckboxRoiClassifier", [
            ("checked 合成图", test_roi_classifier_checked),
            ("unchecked 合成图", test_roi_classifier_unchecked),
            ("unknown（过小）", test_roi_classifier_unknown),
        ]),
        ("Suite 4b: 结构化行为增强", [
            ("is_affirmative", test_visual_query_is_affirmative),
            ("structured 从代码块提取", test_visual_query_structured_extracted_from_codeblock),
            ("structured 从 tool_call 提取", test_visual_query_structured_extracted_from_tool_call),
            ("非 JSON → structured None", test_visual_query_structured_none_when_non_json),
        ]),
        ("Suite 5: domain 零 SDK 依赖", [
            ("无 openai/cv2/PIL 导入", test_domain_vision_no_sdk_imports),
        ]),
    ]

    total = failed = 0
    for suite_name, tests in suites:
        print(f"\n── {suite_name} ──")
        for label, fn in tests:
            total += 1
            try:
                s = fn()
                print(f"  [{s}] {label}")
                if "FAIL" in s:
                    failed += 1
            except Exception as e:
                print(f"  [FAIL ✗] {label}: {e}")
                failed += 1
        print(f"  {suite_name}: {len(tests) - sum(1 for _, f in tests if False)} 组")

    print(f"\n  通过 {total - failed}/{total}")
    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
