"""
Double Check Mock 测试
============================================================================

测试 VLMGrounder._parse() 对 SELECTED 字段的解析，
以及用真实素材 (prices_box_*.jpg) 通过 VLM API 验证。

用法:
  # Mock 测试 (无需设备/API)
  .venv/bin/python tests/test_double_check.py

  # 真实 VLM 测试
  .venv/bin/python tests/test_double_check.py --real-vlm \\
      --vlm-api-key "sk-..." \\
      --vlm-base-url "https://..."
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── 模拟响应 (用于不需要 API 的单元测试) ──

# VLM 返回 SELECTED=true 时需要同时返回 coordinate=[0,0] (系统prompt要求)
# 这是文档中 Explore agent 确认的最常见输出形式
MOCK_SELECTED_TRUE = """Action: 全选勾选框已选中，蓝底白勾，无需点击
SELECTED=true
<tool_call>{"name": "mobile_use", "arguments": {"action": "answer", "coordinate": [0, 0], "bbox": [0, 0, 0, 0]}}</tool_call>"""

# VLM 返回 SELECTED=false 时带有有效的坐标
MOCK_SELECTED_FALSE = """Action: 全选勾选框未选中，灰色空心圆，坐标(480, 620)
SELECTED=false
<tool_call>{"name": "mobile_use", "arguments": {"action": "answer", "coordinate": [480, 620], "bbox": [450, 580, 510, 660]}}</tool_call>"""

# VLM 找不到元素
MOCK_NOT_FOUND = """Action: 当前页面未找到全选勾选控件，可能是页面类型不匹配
<tool_call>{"name": "mobile_use", "arguments": {"action": "answer", "bbox": [0, 0, 0, 0]}}</tool_call>"""

# VLM 返回 SELECTED=true 但没有 coordinate (罕见但可能)
MOCK_SELECTED_TRUE_NO_COORD = """Action: 已确认全选已勾选
SELECTED=true
<tool_call>{"name": "mobile_use", "arguments": {"action": "answer", "bbox": [0, 0, 0, 0]}}</tool_call>"""

# ── Import 被测代码 ──
from collector.infrastructure.vision.vlm_grounder import VLMGrounder


# ======================================================================
# Test Suite 1: _parse() mock 测试
# ======================================================================

SCREEN_W, SCREEN_H = 2670, 1200  # 典型设备 (Pixel-like)


def test_parse_selected_true():
    """VLM 返回 SELECTED=true + coordinate=[0,0] + bbox=[0,0,0,0]"""
    result = VLMGrounder._parse(
        MOCK_SELECTED_TRUE, "全选勾选框", SCREEN_W, SCREEN_H, True,
    )
    assert result["selected"] is True, f"expected selected=True, got {result['selected']}"
    assert result["found"] is False, f"expected found=False (rejected sentinel), got {result['found']}"
    assert result["center"] is None, f"expected center=None, got {result['center']}"
    return "PASS ✓"


def test_parse_selected_false():
    """VLM 返回 SELECTED=false + 有效坐标"""
    result = VLMGrounder._parse(
        MOCK_SELECTED_FALSE, "全选勾选框", SCREEN_W, SCREEN_H, True,
    )
    assert result["selected"] is False, f"expected selected=False, got {result['selected']}"
    assert result["found"] is True, f"expected found=True, got {result['found']}"
    assert result["center"] is not None
    # 中心坐标应接近 (480/1000*screen_w, 620/1000*screen_h)
    expected_cx = round(480 / 1000 * SCREEN_W)
    expected_cy = round(620 / 1000 * SCREEN_H)
    assert abs(result["center"][0] - expected_cx) < 5, \
        f"center.x mismatch: {result['center'][0]} vs {expected_cx}"
    assert abs(result["center"][1] - expected_cy) < 5, \
        f"center.y mismatch: {result['center'][1]} vs {expected_cy}"
    return "PASS ✓"


def test_parse_not_found():
    """VLM 找不到元素 (bbox=[0,0,0,0], 无 SELECTED)"""
    result = VLMGrounder._parse(
        MOCK_NOT_FOUND, "全选勾选框", SCREEN_W, SCREEN_H, True,
    )
    assert result["selected"] is None, f"expected selected=None, got {result['selected']}"
    assert result["found"] is False, f"expected found=False, got {result['found']}"
    return "PASS ✓"


def test_parse_selected_true_no_coord():
    """VLM 返回 SELECTED=true 但没有 coordinate (只有 bbox=[0,0,0,0])"""
    result = VLMGrounder._parse(
        MOCK_SELECTED_TRUE_NO_COORD, "全选勾选框", SCREEN_W, SCREEN_H, True,
    )
    assert result["selected"] is True, f"expected selected=True, got {result['selected']}"
    assert result["found"] is False, f"expected found=False, got {result['found']}"
    return "PASS ✓"


def test_parse_api_failure():
    """API 调用失败时 selected 也要从 partial text 提取"""
    result = VLMGrounder._parse(
        MOCK_SELECTED_TRUE, "全选勾选框", SCREEN_W, SCREEN_H, False,
    )
    # api_success=False 时不会解析 tool_call, 但 selected 仍应从 raw_text 提取
    assert result["selected"] is True, f"expected selected=True even on API failure"
    return "PASS ✓"


def test_coordinate_zero_zero_rejected():
    """核心回归测试: coordinate=[0,0] 不被视为有效点击"""
    # 构造一个没有 SELECTED 但 coordinate=[0,0] 的响应 (孤立的 sentinel)
    raw = """Action: 已选中
<tool_call>{"name": "mobile_use", "arguments": {"action": "answer", "coordinate": [0, 0], "bbox": [0, 0, 0, 0]}}</tool_call>"""
    result = VLMGrounder._parse(raw, "checkbox", SCREEN_W, SCREEN_H, True)
    assert result["found"] is False, f"BUG REGRESSION: coordinate=[0,0] should NOT be treated as found"
    assert result["center"] is None
    return "PASS ✓ (回归: 不再在 (0,0) 点击)"


# ======================================================================
# Test Suite 2: 完整 double check 模拟（不调设备）
# ======================================================================

def test_doublecheck_decision_logic():
    """模拟 _do_ground_doublecheck 的决策逻辑"""
    from collector.infrastructure.vision.vlm_grounder import VLMGrounder

    results = []

    test_cases = [
        ("all_no (全未选)", MOCK_SELECTED_FALSE, "CLICK", None),
        ("all_yes (全已选)", MOCK_SELECTED_TRUE, "SKIP", True),
    ]

    for label, vlm_text, expected_decision, expected_selected in test_cases:
        result = VLMGrounder._parse(vlm_text, "checkbox", SCREEN_W, SCREEN_H, True)

        # 模拟修复后的决策逻辑
        selected = result.get("selected")
        found = result.get("found")

        if selected is True:
            decision = "SKIP"
        elif found and result.get("center"):
            decision = "CLICK"
        else:
            decision = "UNKNOWN"

        ok = decision == expected_decision
        status = "PASS ✓" if ok else f"FAIL ✗ (expected {expected_decision}, got {decision})"
        results.append((label, status, result))
        print(f"  {label}: decision={decision} selected={selected} found={found} → {status}")

    all_pass = all("PASS" in s for _, s, _ in results)
    return all_pass


# ======================================================================
# Test Suite 3: 真实 VLM API 测试
# ======================================================================

def real_vlm_tests(api_key: str, base_url: str) -> None:
    """真实 VLM 测试：目标锚定的全选状态判定（SEL-01 四张素材）。

    期望：
      prices_box_all_no.jpg    → UNCHECKED
      prices_box_some_yes.jpg  → UNCHECKED（子项已选 ≠ 全选已选）
      prices_box_all_yes.jpg   → CHECKED
      打车页.jpg("全选经济")     → UNCHECKED（即使子项已勾选）
    """
    from PIL import Image as _PILImage

    from collector.domain.checkbox import CheckboxState
    from collector.infrastructure.vision.vlm_grounder import VLMGrounder
    from collector.platform.gaode.select_all import detect_select_all_state

    grounder = VLMGrounder(
        api_key=api_key, base_url=base_url,
        model="qwen3-vl-plus",
        image_max_pixels=400000,
    )

    # (文件名, label, 期望状态, 说明)
    test_cases = [
        ("prices_box_all_no.jpg", "全选", CheckboxState.UNCHECKED, "全未选"),
        ("prices_box_some_yes.jpg", "全选", CheckboxState.UNCHECKED, "子项已选但全选未勾"),
        ("prices_box_all_yes.jpg", "全选", CheckboxState.CHECKED, "全已选"),
        ("打车页.jpg", "全选经济", CheckboxState.UNCHECKED, "打车页全选经济(即使子项已勾)"),
    ]

    print("\n" + "=" * 60)
    print("  Real VLM Tests (SEL-01 目标锚定全选)")
    print("=" * 60)

    passed = 0
    for fname, label, expected, note in test_cases:
        img_path = str(_PROJECT_ROOT / "assets" / fname)
        if not Path(img_path).exists():
            print(f"\n  ⚠ 跳过: {fname} 不存在")
            continue

        size = _PILImage.open(img_path).size  # 用实际图片尺寸做归一化
        print(f"\n── 测试: {fname} ({note}) label={label} 期望={expected.value}")

        state, target = detect_select_all_state(
            img_path, label=label, screen_size=size, grounder=grounder,
        )

        print(f"  state={state.value} | target_found={target.target_found} "
              f"| relation={target.relation_valid}")
        print(f"  checkbox_bbox={target.checkbox_bbox} | reason={target.reason or '-'}")

        ok = state == expected
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  Result: {status}")
        if ok:
            passed += 1

    print(f"\n  Real VLM (SEL-01): {passed}/{len(test_cases)} passed")


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Double Check Mock/Real Tests")
    parser.add_argument("--real-vlm", action="store_true",
                        help="用真实 VLM API 测试 3 张素材图")
    parser.add_argument("--vlm-api-key", help="API Key")
    parser.add_argument("--vlm-base-url", help="Base URL")
    args = parser.parse_args()

    print("=" * 60)
    print("  Double Check Tests")
    print("=" * 60)

    # ── Suite 1: Mock parse tests ──
    print("\n── Suite 1: _parse() mock tests ──")
    suite1 = [
        ("selected=true + coord=[0,0]",      test_parse_selected_true),
        ("selected=false + valid coord",      test_parse_selected_false),
        ("not found (no SELECTED)",           test_parse_not_found),
        ("selected=true without coordinate",  test_parse_selected_true_no_coord),
        ("API failure → extract selected",    test_parse_api_failure),
        ("REGRESSION: coord=[0,0] rejected",  test_coordinate_zero_zero_rejected),
    ]
    failed1 = 0
    for label, fn in suite1:
        try:
            s = fn()
            print(f"  [{s}] {label}")
            if "FAIL" in s:
                failed1 += 1
        except Exception as e:
            print(f"  [FAIL ✗] {label}: {e}")
            failed1 += 1
    print(f"  Suite 1: {len(suite1) - failed1}/{len(suite1)} passed")

    # ── Suite 2: Decision logic simulation ──
    print("\n── Suite 2: Double check decision logic ──")
    ok = test_doublecheck_decision_logic()
    print(f"  Suite 2: {'PASS' if ok else 'FAIL'}")

    # ── Suite 3: Real VLM (optional) ──
    if args.real_vlm:
        if not args.vlm_api_key or not args.vlm_base_url:
            print("\n❌ --real-vlm 需要 --vlm-api-key 和 --vlm-base-url")
            sys.exit(1)
        real_vlm_tests(args.vlm_api_key, args.vlm_base_url)
    else:
        print("\n  (跳过真实 VLM 测试，加 --real-vlm 启用)")

    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
