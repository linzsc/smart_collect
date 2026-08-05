"""SEL-01 目标锚定的幂等全选 — 测试
============================================================================

- 勾选框 ROI 分类（合成图，100 次成功率）
- 空间关系校验
- ensure_all_selected 编排（已勾选/未勾选→点击→重验/点击后未变→失败）
- 真实素材 100 次成功率验证（素材就位后生效）：
    assets/select_all_yes.jpg → CHECKED
    assets/select_all_no.jpg  → UNCHECKED

用法:
  .venv/bin/python tests/test_select_all.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PIL import Image, ImageDraw

from collector.domain.checkbox import CheckboxState, SelectAllTarget, validate_select_all_relation
from collector.infrastructure.vision.checkbox import classify_checkbox_roi, crop_roi
from collector.platform.gaode.select_all import (
    SelectAllError,
    detect_select_all_state,
    ensure_all_selected,
)

# ======================================================================
# 合成图工具
# ======================================================================

def _make_checked_roi(size: int = 60) -> Image.Image:
    """蓝色实心勾选框（已勾选）。"""
    img = Image.new("RGB", (size, size), (30, 120, 240))
    return img


def _make_unchecked_roi(size: int = 60) -> Image.Image:
    """白底灰色圆环（未勾选）。"""
    img = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, size - 8, size - 8), outline=(150, 150, 150), width=4)
    return img


def _make_unknown_roi(size: int = 100) -> Image.Image:
    """蓝色占比 ~10%（介于两阈值之间）→ UNKNOWN。"""
    img = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 10, size), fill=(30, 120, 240))  # 10% 蓝色
    return img


def _make_synthetic_screen(checkbox_state: str) -> Path:
    """构造一张合成“截图”：左侧灰色 label 条 + 右侧勾选框。"""
    import tempfile
    from pathlib import Path as _P

    tmp = _P(tempfile.mkdtemp(prefix="sel_"))
    path = tmp / "screen.png"
    img = Image.new("RGB", (200, 200), "white")
    d = ImageDraw.Draw(img)
    # label 条（20,50)-(80,90)
    d.rectangle((20, 50, 80, 90), fill=(180, 180, 180))
    # 勾选框（100,55)-(140,95)
    if checkbox_state == "checked":
        d.rectangle((100, 55, 140, 95), fill=(30, 120, 240))
    else:
        d.ellipse((100, 55, 140, 95), outline=(150, 150, 150), width=3)
    img.save(path)
    return path


def _mock_grounder(label_bbox_1k, checkbox_bbox_1k, state="checked"):
    g = MagicMock()
    g.query_structured.return_value = {
        "raw_response": json.dumps({
            "target_found": True,
            "target": "全选右侧主勾选框",
            "label_bbox": label_bbox_1k,
            "checkbox_bbox": checkbox_bbox_1k,
            "state": state,
            "relation": "right_same_row",
        }),
        "success": True,
    }
    return g


# ======================================================================
# Suite 1: ROI 分类（100 次成功率）
# ======================================================================

def test_classify_checkbox_roi_100x():
    """合成勾选框图各跑 100 次，成功率应为 100%. """
    cases = [
        ("checked", _make_checked_roi(), CheckboxState.CHECKED),
        ("unchecked", _make_unchecked_roi(), CheckboxState.UNCHECKED),
    ]
    for name, img, expected in cases:
        ok = 0
        fails = []
        for i in range(100):
            state = classify_checkbox_roi(img)
            if state == expected:
                ok += 1
            else:
                fails.append((i, state.value))
        rate = ok / 100 * 100
        print(f"  {name}: {ok}/100 ({rate:.1f}%) 失败 {100 - ok}/100")
        assert ok == 100, f"{name} 未达 100%: {fails[:5]}"
    return "PASS ✓"


def test_classify_checkbox_roi_unknown():
    """蓝色占比居中的图 → UNKNOWN。"""
    state = classify_checkbox_roi(_make_unknown_roi())
    assert state == CheckboxState.UNKNOWN, f"期望 UNKNOWN, 实际 {state.value}"
    return "PASS ✓"


# ======================================================================
# Suite 2: 空间关系校验
# ======================================================================

def test_validate_select_all_relation():
    label = [20, 50, 80, 90]      # 左侧文字
    check_right = [100, 55, 140, 95]   # 右侧同行
    screen = (200, 200)

    ok, _ = validate_select_all_relation(label, check_right, screen_size=screen)
    assert ok, "右侧同行应通过"

    # 不同行
    check_wrong_row = [100, 130, 140, 170]
    ok, reason = validate_select_all_relation(label, check_wrong_row, screen_size=screen)
    assert not ok, "不同行应失败"
    assert "同一行" in reason

    # 在文字左侧
    check_left = [10, 55, 40, 95]
    ok, reason = validate_select_all_relation(label, check_left, screen_size=screen)
    assert not ok and "右侧" in reason

    # 过小
    check_tiny = [100, 55, 105, 60]
    ok, reason = validate_select_all_relation(label, check_tiny, screen_size=screen)
    assert not ok and "过小" in reason

    # 预期区域外
    ok, reason = validate_select_all_relation(
        label, check_right, screen_size=screen, expected_region=[0, 120, 200, 200])
    assert not ok and "预期区域" in reason
    return "PASS ✓"


# ======================================================================
# Suite 3: 端到端定位（结构化 VLM + ROI 分类）
# ======================================================================

def test_detect_select_all_state_structured():
    """VLM 返回严格 JSON → 定位 → 裁剪 ROI → 分类 CHECKED。"""
    screen = _make_synthetic_screen("checked")
    # 200x200 → 0-1000 归一化（×5）
    g = _mock_grounder(
        label_bbox_1k=[100, 250, 400, 450],
        checkbox_bbox_1k=[500, 275, 700, 475],
        state="checked",
    )
    state, target = detect_select_all_state(
        str(screen), label="全选", screen_size=(200, 200), grounder=g,
    )
    assert state == CheckboxState.CHECKED, f"期望 CHECKED, 实际 {state.value}"
    assert target.relation_valid, f"空间关系应有效: {target.reason}"
    assert target.checkbox_bbox == [100, 55, 140, 95], f"bbox 重缩放错误: {target.checkbox_bbox}"
    return "PASS ✓"


def test_detect_select_all_state_unstructured_is_unknown():
    """结构化结果缺失 → UNKNOWN（不做自然语言兜底）。"""
    screen = _make_synthetic_screen("checked")
    g = MagicMock()
    g.query_structured.return_value = {
        "raw_response": "全选已经勾选了，其他服务商也都已勾选",  # 自然语言，无 JSON
        "success": True,
    }
    state, target = detect_select_all_state(
        str(screen), label="全选", screen_size=(200, 200), grounder=g,
    )
    assert state == CheckboxState.UNKNOWN, f"无结构化结果应 UNKNOWN, 实际 {state.value}"
    assert not target.target_found
    return "PASS ✓"


# ======================================================================
# Suite 4: ensure_all_selected 编排（注入 locator/classifier）
# ======================================================================

def _locator_for(target: SelectAllTarget):
    def _loc(grounder, image_path, label, screen_size, expected_region, stats, verbose):
        return target
    return _loc


def _run_ensure(tmp, checked_seq, click_center=None):
    """跑一次 ensure_all_selected；返回 (adb, result_or_error)。"""
    import tempfile
    from pathlib import Path as _P

    if tmp is None:
        tmp = _P(tempfile.mkdtemp(prefix="sel_"))

    def shot(name: str) -> str:
        p = _P(tmp) / f"{name}.jpg"
        Image.new("RGB", (10, 10), "white").save(p)
        return str(p)

    target = SelectAllTarget(
        target_found=True, target_label="全选经济",
        label_bbox=[20, 50, 80, 90],
        checkbox_bbox=[100, 55, 140, 95],
        checkbox_center=(120, 75),
        relation_valid=True,
        state=CheckboxState.UNCHECKED.value,
    )
    seq = list(checked_seq)

    def classifier(roi):
        return seq.pop(0) if seq else CheckboxState.UNKNOWN

    adb = MagicMock()
    try:
        result = ensure_all_selected(
            adb=adb,
            grounder=MagicMock(),
            label="全选经济",
            screen_size=(200, 200),
            screenshot=shot,
            stats={},
            verbose=False,
            wait_after_click=0.0,
            locator=_locator_for(target),
            classifier=classifier,
        )
        return adb, result, None
    except SelectAllError as e:
        return adb, None, e


def test_ensure_all_selected_already_checked():
    """已勾选 → 不点击，直接成功；但也要截 select_all_after 作为打车页证据（RES-01 冒泡页）。"""
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as tmp:
        adb, result, err = _run_ensure(tmp, [CheckboxState.CHECKED])
        assert err is None, f"不应失败: {err}"
        assert result.state == CheckboxState.CHECKED.value
        assert not adb.click.called, "已勾选不应点击"
        assert (_P(tmp) / "select_all_after.jpg").exists(), "已勾选也应截 select_all_after"
    return "PASS ✓"


def test_ensure_all_selected_unchecked_then_click():
    """未勾选 → 点击一次 → 重验 CHECKED → 成功。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        adb, result, err = _run_ensure(
            tmp, [CheckboxState.UNCHECKED, CheckboxState.CHECKED])
        assert err is None, f"不应失败: {err}"
        assert result.state == CheckboxState.CHECKED.value
        assert adb.click.called, "未勾选应点击一次"
        adb.click.assert_called_once_with(120, 75)
    return "PASS ✓"


def test_ensure_all_selected_click_not_verified_fails():
    """点击后仍 UNCHECKED → 抛 SelectAllError（禁止盲点）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        adb, result, err = _run_ensure(
            tmp, [CheckboxState.UNCHECKED, CheckboxState.UNCHECKED])
        assert err is not None, "点击后未变应失败"
        assert "验证失败" in str(err)
        assert adb.click.called
    return "PASS ✓"


def test_ensure_all_selected_unknown_fails():
    """状态 UNKNOWN → 抛 SelectAllError（禁止盲点）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        adb, result, err = _run_ensure(tmp, [CheckboxState.UNKNOWN])
        assert err is not None and "禁止盲点" in str(err)
        assert not adb.click.called, "UNKNOWN 不应点击"
    return "PASS ✓"


# ======================================================================
# Suite 5: 真实素材 100 次成功率验证（素材已就位）
# ======================================================================
# 素材：
#   assets/select_all_yes/select_all_yes_1..3.jpg → 期望 CHECKED
#   assets/select_all_no/select_all_no_1..4.jpg   → 期望 UNCHECKED
# 每张整页截图自动定位主勾选框（离线启发式）→ 裁剪 ROI → 分类 100 次。

_SELECT_ALL_YES_GLOB = "select_all_yes/select_all_yes_*.jpg"
_SELECT_ALL_NO_GLOB = "select_all_no/select_all_no_*.jpg"


def test_select_all_offline_100x():
    """离线 100 次：整页截图 → 定位主勾选框 → 裁剪 ROI → 分类 100 次，统计成功率/失败率。"""
    from collector.platform.gaode.select_all import locate_select_all_checkbox_offline

    cases = []
    for p in sorted((_PROJECT_ROOT / "assets").glob(_SELECT_ALL_YES_GLOB)):
        cases.append((p, CheckboxState.CHECKED))
    for p in sorted((_PROJECT_ROOT / "assets").glob(_SELECT_ALL_NO_GLOB)):
        cases.append((p, CheckboxState.UNCHECKED))

    if not cases:
        print("  ⚠ 素材缺失，跳过")
        return "SKIP (素材缺失)"

    total_ok, total = 0, 0
    for p, expected in cases:
        img = Image.open(p)
        target = locate_select_all_checkbox_offline(img)
        if not target.target_found or not target.checkbox_bbox:
            print(f"  ⚠ {p.name}: 未定位到主勾选框，跳过")
            continue
        roi = crop_roi(img, target.checkbox_bbox)
        ok = 0
        fails = []
        for i in range(100):
            state = classify_checkbox_roi(roi)
            if state == expected:
                ok += 1
            else:
                fails.append((i, state.value))
        rate = ok / 100 * 100
        total_ok += ok
        total += 100
        print(f"  {p.name}: 期望={expected.value} ROI={target.checkbox_bbox} "
              f"({target.reason}) 成功 {ok}/100 ({rate:.1f}%) 失败 {100 - ok}/100")
        assert ok == 100, f"{p.name} 未达 100%: 失败样例 {fails[:5]}"

    if total == 0:
        return "SKIP (未定位到任何主勾选框)"
    return f"PASS ✓ ({total_ok}/{total})"


def real_vlm_100x(api_key: str, base_url: str, iterations: int = 100, label: str = "全选") -> None:
    """在线 100 次：VLM 定位「label」右侧主勾选框 → 裁剪 ROI 分类。

    每张素材跑 iterations 次，统计成功率/失败率（需真实 VLM key）。
    """
    from collector.infrastructure.vision.vlm_grounder import VLMGrounder

    grounder = VLMGrounder(
        api_key=api_key, base_url=base_url,
        model="qwen3-vl-plus", image_max_pixels=400000,
    )
    cases = [
        ("select_all_yes", CheckboxState.CHECKED),
        ("select_all_no", CheckboxState.UNCHECKED),
    ]
    print(f"\n  在线验证: label='{label}' 每张 {iterations} 次")
    total_ok, total = 0, 0
    for group, expected_state in cases:
        for p in sorted((_PROJECT_ROOT / "assets" / group).glob("*.jpg")):
            ok = 0
            fails = []
            size = Image.open(p).size
            for i in range(iterations):
                state, _ = detect_select_all_state(
                    str(p), label=label, screen_size=size, grounder=grounder,
                )
                if state == expected_state:
                    ok += 1
                else:
                    fails.append((i, state.value))
            rate = ok / iterations * 100
            total_ok += ok
            total += iterations
            print(f"  {group}/{p.name}: 期望={expected_state.value}, "
                  f"成功 {ok}/{iterations} ({rate:.1f}%), 失败 {iterations - ok}/{iterations}")
            assert ok >= iterations * 0.9, \
                f"{p.name} 成功率不足 90%: 失败样例 {fails[:5]}"
    print(f"  在线总计: {total_ok}/{total}")


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    print("=" * 60)
    print("  SEL-01 全选勾选框测试")
    print("=" * 60)

    suite1 = [
        ("ROI 分类 100x", test_classify_checkbox_roi_100x),
        ("ROI 分类 UNKNOWN", test_classify_checkbox_roi_unknown),
    ]
    suite2 = [("空间关系校验", test_validate_select_all_relation)]
    suite3 = [
        ("端到端定位(结构化+ROI)", test_detect_select_all_state_structured),
        ("无结构化→UNKNOWN", test_detect_select_all_state_unstructured_is_unknown),
    ]
    suite4 = [
        ("已勾选跳过", test_ensure_all_selected_already_checked),
        ("未勾选→点击→重验", test_ensure_all_selected_unchecked_then_click),
        ("点击后未变→失败", test_ensure_all_selected_click_not_verified_fails),
        ("UNKNOWN→禁止盲点", test_ensure_all_selected_unknown_fails),
    ]
    suite5 = [("离线素材 100x (ROI)", test_select_all_offline_100x)]

    parser = argparse.ArgumentParser(description="SEL-01 全选测试")
    parser.add_argument("--real-vlm", action="store_true", help="在线 VLM 验证")
    parser.add_argument("--vlm-api-key", help="API Key")
    parser.add_argument("--vlm-base-url", help="Base URL")
    parser.add_argument("--iterations", type=int, default=100, help="在线每张图次数")
    parser.add_argument("--label", default="全选", help="主勾选框 label")
    args = parser.parse_args()

    all_pass = True
    for label, fn in suite1 + suite2 + suite3 + suite4 + suite5:
        try:
            s = fn()
            print(f"  [{s}] {label}")
            if "FAIL" in s:
                all_pass = False
        except Exception as e:
            print(f"  [FAIL ✗] {label}: {e}")
            import traceback
            traceback.print_exc()
            all_pass = False

    # 在线 100 次成功率（需真实 VLM key）
    if args.real_vlm:
        if not args.vlm_api_key or not args.vlm_base_url:
            print("\n❌ --real-vlm 需要 --vlm-api-key 和 --vlm-base-url")
            sys.exit(1)
        try:
            real_vlm_100x(args.vlm_api_key, args.vlm_base_url,
                          iterations=args.iterations, label=args.label)
        except Exception as e:
            print(f"  [FAIL ✗] 在线 100x: {e}")
            import traceback
            traceback.print_exc()
            all_pass = False

    print("=" * 60)
    print(f"  {'✓ 全部通过' if all_pass else '✗ 存在失败'}")
    print("=" * 60)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
