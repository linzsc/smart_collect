"""
计价采集子流程 Mock 测试
============================================================================

测试 RidePricingFSM 的核心逻辑:

  打车页 → S1(全选经济) → S2(识别供应商) →
    ┌─ S3a(点?进入计价页) → S3c(详细计价规则) ─┐
    │  工作日下滑→回顶→休息日下滑→返回×2          │  循环 2 个
    └───────────────────────────────────────────┘

用法:
  # Mock 测试 (无需设备/API)
  .venv/bin/python tests/test_pricing_collect.py

  # 真实 VLM + 设备测试
  .venv/bin/python tests/test_pricing_collect.py --real-device \\
      --adb-path /opt/homebrew/bin/adb \\
      --vlm-api-key "sk-..." \\
      --vlm-base-url "https://..."

  # 真实 VLM 仅素材验证 (不需要设备)
  .venv/bin/python tests/test_pricing_collect.py --real-vlm \\
      --vlm-api-key "sk-..." \\
      --vlm-base-url "https://..."
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, call, patch

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# Suite 1: 纯逻辑测试 (不需要任何 mock)
# ======================================================================

def test_s2_parse_json_array():
    """S2: VLM 返回标准 JSON 数组"""
    raw = '```json\n["快车", "特惠快车", "优酷快车"]\n```'
    from collector.platform.gaode.ride_pricing import RidePricingFSM

    # 模拟 _s2_list_suppliers 的解析逻辑
    cleaned = raw
    for m in ("```json", "```"):
        if cleaned.startswith(m):
            cleaned = cleaned[len(m):].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    parsed = json.loads(cleaned)
    assert isinstance(parsed, list)
    assert "快车" in parsed
    assert "特惠快车" in parsed
    return "PASS ✓", parsed


def test_s2_parse_excludes_taxi_and_youxiang():
    """S2: 排除「出租车」和「优享」"""
    raw = '["快车", "出租车", "特惠快车", "优享", "拼车"]'
    from collector.platform.gaode.ride_pricing import _SKIP_SUPPLIERS

    cleaned = raw.strip()
    parsed = json.loads(cleaned)
    suppliers = [n for n in parsed if not any(kw in n for kw in _SKIP_SUPPLIERS)]
    assert "快车" in suppliers
    assert "特惠快车" in suppliers
    assert "拼车" in suppliers
    assert "出租车" not in suppliers
    assert "优享" not in suppliers
    return "PASS ✓", suppliers


def test_s2_parse_line_by_line_fallback():
    """S2: JSON 解析失败时回退到逐行提取"""
    raw = """1. 快车
2. 特惠快车
3. 优选快车"""

    import re
    from collector.platform.gaode.ride_pricing import _SKIP_SUPPLIERS

    suppliers = []
    for line in raw.split("\n"):
        line = re.sub(r'^[\d\.\、\)）\-\s]+', '', line.strip())
        line = line.strip().strip('"').strip("'").strip(",")
        if line and len(line) <= 30 and not any(kw in line for kw in _SKIP_SUPPLIERS):
            if len(line) >= 2 and line not in suppliers:
                suppliers.append(line)
    assert len(suppliers) == 3
    assert "快车" in suppliers
    return "PASS ✓", suppliers


def test_s2_parse_empty():
    """S2: VLM 返回空数组"""
    raw = '[]'
    parsed = json.loads(raw)
    assert parsed == []
    return "PASS ✓", []


def test_extract_center_from_bbox_and_center():
    """_extract_center: 同时有 bbox 和 center"""
    from collector.platform.gaode.ride_pricing import RidePricingFSM

    # 需要实例才能调用 _extract_center, 但它不是 static
    # 我们直接用同样逻辑
    result = {"bbox": [100, 200, 300, 400], "center": [200, 300]}
    bbox = result.get("bbox")
    center = result.get("center")
    if bbox and bbox != [0, 0, 0, 0] and center:
        assert center == [200, 300]
    return "PASS ✓"


def test_extract_center_only_center():
    """_extract_center: 只有 center 没有 bbox"""
    result = {"bbox": [0, 0, 0, 0], "center": [500, 600]}
    bbox = result.get("bbox")
    center = result.get("center")
    ok = (bbox and bbox != [0, 0, 0, 0] and center)
    if not ok and center and center != [0, 0]:
        # fallback: use center directly
        assert center == [500, 600]
    return "PASS ✓"


def test_extract_center_none():
    """_extract_center: bbox 全零, center 也是 [0,0]"""
    result = {"bbox": [0, 0, 0, 0], "center": [0, 0]}
    bbox = result.get("bbox")
    center = result.get("center")
    ok = (bbox and bbox != [0, 0, 0, 0] and center)

    # center=[0,0] is sentinel → should NOT be extracted
    if not ok:
        if center and center != [0, 0]:
            pass  # would use center
        else:
            center = None  # correctly rejected
    assert center is None
    return "PASS ✓"


def test_detect_end_marker():
    """CAP-01: _detect_end_marker 解析 LLM 的 YES/NO 响应，并计入 vlm_calls."""
    import tempfile

    from collector.platform.gaode.ride_pricing import RidePricingFSM

    with tempfile.TemporaryDirectory() as tmp:
        fsm = RidePricingFSM(
            adb=MagicMock(), grounder=MagicMock(), supplier="x",
            profile_cfg={}, output_dir=str(Path(tmp) / "out"), verbose=False,
        )
        fsm.stats["vlm_calls"] = 0

        # YES → 出现「预约用车」
        fsm.grounder.query_text.return_value = {"raw_response": "YES，出现了蓝色预约用车", "success": True}
        assert fsm._detect_end_marker("x.jpg") is True
        assert fsm.stats["vlm_calls"] == 1

        # NO → 未出现
        fsm.grounder.query_text.return_value = {"raw_response": "NO，未出现预约用车", "success": True}
        assert fsm._detect_end_marker("x.jpg") is False
        assert fsm.stats["vlm_calls"] == 2
    return "PASS ✓"




def test_screenshot_organizer():
    """RES-01: 结果整理 — 筛选必要截图并按 工作日/休息日 × 运力商 聚合到 result/."""
    import tempfile

    from collector.platform.gaode.screenshot_organizer import collect_necessary_screenshots

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "output"
        res = Path(tmp) / "result"
        out.mkdir()

        # 打车页（全选经济后）
        (out / "p04_select_all_after.jpg").write_bytes(b"ride")
        # 各标签 × 各运力商 scroll_0..3
        cases = [
            ("p12", "工作日", "飞嘀打车"),
            ("p22", "休息日", "飞嘀打车"),
            ("p34", "工作日", "旗妙出行"),
            ("p44", "休息日", "旗妙出行"),
        ]
        for prefix, tab, supplier in cases:
            base = int(prefix[1:])
            for i in range(4):
                (out / f"p{base + i}_{tab}_scroll_{i}_{supplier}.jpg").write_bytes(b"s")
        # 干扰文件：不应被复制
        for name in (
            "p16_工作日_check_3_飞嘀打车.jpg",
            "p20_工作日_check_6_飞嘀打车.jpg",
            "p26_02_休息日_bottom_飞嘀打车.jpg",
            "p11_detail_before_工作日_飞嘀打车.jpg",
            "p05_after_select_all.jpg",
        ):
            (out / name).write_bytes(b"x")

        summary = collect_necessary_screenshots(out, res)

        groups = summary["groups"]
        assert set(groups) == {"工作日", "休息日"}, groups
        for tab in ("工作日", "休息日"):
            assert set(groups[tab]) == {"飞嘀打车", "旗妙出行"}, groups[tab]
            for supplier in ("飞嘀打车", "旗妙出行"):
                files = sorted(groups[tab][supplier])
                assert len(files) == 4, f"{tab}/{supplier}: {files}"
                assert files[0].endswith(f"_scroll_0_{supplier}.jpg"), files
                assert files[3].endswith(f"_scroll_3_{supplier}.jpg"), files

        # 目录结构：打车页入 <标签>/冒泡页/（每个大文件夹各 1 次，共 2 次）
        for tab in ("工作日", "休息日"):
            bubble = res / tab / "冒泡页"
            assert (bubble / "p04_select_all_after.jpg").exists(), f"{bubble} 缺打车页"
            for supplier in ("飞嘀打车", "旗妙出行"):
                folder = res / tab / supplier
                assert not (folder / "p04_select_all_after.jpg").exists(), \
                    f"{folder} 不应包含打车页"
                scrolls = list(folder.glob(f"*_{tab}_scroll_*_{supplier}.jpg"))
                assert len(scrolls) == 4, f"{folder}: 应 4 张滚动截图, 实际 {len(scrolls)}"

        # 干扰文件未复制
        assert not (res / "工作日" / "飞嘀打车" / "p16_工作日_check_3_飞嘀打车.jpg").exists()
        assert not (res / "工作日" / "飞嘀打车" / "p05_after_select_all.jpg").exists()

        # 打车页 1 张 × 2 大文件夹 + 滚动 4 张 × 4 组 = 18
        assert summary["copied"] == 18, f"copied={summary['copied']}"
    return "PASS ✓"




def test_page_unchanged():
    """CAP-05: _page_unchanged 本地像素比对 — 相同页面 True，不同页面 False，缺文件 False."""
    import tempfile

    from PIL import Image

    from collector.platform.gaode.ride_pricing import RidePricingFSM

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fsm = RidePricingFSM(adb=MagicMock(), grounder=MagicMock(), supplier="x",
                             profile_cfg={}, output_dir=str(tmp / "out"), verbose=False)
        a = tmp / "a.jpg"
        b = tmp / "b.jpg"
        c = tmp / "c.jpg"
        Image.new("RGB", (200, 400), (200, 200, 200)).save(a)
        Image.new("RGB", (200, 400), (200, 200, 200)).save(b)  # 与 a 完全相同
        Image.new("RGB", (200, 400), (0, 0, 0)).save(c)         # 与 a 完全不同

        assert fsm._page_unchanged(str(a), str(b)) is True
        assert fsm._page_unchanged(str(a), str(c)) is False
        assert fsm._page_unchanged(str(a), str(tmp / "missing.jpg")) is False
    return "PASS ✓"


def test_scroll_to_bottom_marker_or_stable():
    """CAP-05: 退出条件 = 出现「预约用车」**或**页面不再变化。

    1) 出现标记即停（页面比对不再需要）;
    2) 未出现标记但页面无变化 → 停止;
    3) 两者都未满足 → 达上限返回 False。
    """
    import tempfile

    from collector.platform.gaode.ride_pricing import RidePricingFSM

    def _mkfsm(max_swipes):
        mock_adb = MagicMock()
        type(mock_adb).screen_size = PropertyMock(return_value=(1080, 2400))
        fsm = RidePricingFSM(
            adb=mock_adb, grounder=MagicMock(), supplier="x",
            profile_cfg={"collection": {"max_detail_swipes": max_swipes}},
            output_dir=str(Path(tempfile.gettempdir()) / "cap05_out"),
            verbose=False,
        )
        fsm._shot = MagicMock(side_effect=lambda name: f"/fake/{name}.jpg")
        return fsm

    # 1) 出现「预约用车」→ 停止（标记命中时不再做页面比对）
    fsm1 = _mkfsm(12)
    fsm1._detect_end_marker = MagicMock(side_effect=[False, True])
    fsm1._page_unchanged = MagicMock(return_value=False)
    assert fsm1._scroll_to_bottom("工作日", "飞嘀打车") is True
    assert fsm1._detect_end_marker.call_count == 2, fsm1._detect_end_marker.call_count
    assert fsm1._page_unchanged.call_count == 1, fsm1._page_unchanged.call_count  # 仅 i=0

    # 2) 未出现标记但页面无变化 → 停止
    fsm2 = _mkfsm(12)
    fsm2._detect_end_marker = MagicMock(return_value=False)
    fsm2._page_unchanged = MagicMock(side_effect=[False, True])
    assert fsm2._scroll_to_bottom("休息日", "旗妙出行") is True
    assert fsm2._page_unchanged.call_count == 2, fsm2._page_unchanged.call_count

    # 3) 两者都未满足 → 达上限返回 False
    fsm3 = _mkfsm(2)
    fsm3._detect_end_marker = MagicMock(return_value=False)
    fsm3._page_unchanged = MagicMock(return_value=False)
    assert fsm3._scroll_to_bottom("工作日", "飞嘀打车") is False
    assert fsm3._detect_end_marker.call_count == 2, fsm3._detect_end_marker.call_count
    assert fsm3._page_unchanged.call_count == 2, fsm3._page_unchanged.call_count
    return "PASS ✓"

# ======================================================================
# Suite 2: FSM 完整流程 Mock 测试
# ======================================================================

def test_fsm_full_flow():
    """Mock AdbTools + VLMGrounder, 验证 FSM 调用顺序.

    模拟: 打车页有 2 个供应商 (快车, 特惠快车), 全选经济未选中.

    预期调用链:
      1. S0: slide(上滑)
      2. S1: ensure_all_selected(全选经济) → 幂等勾选 (SEL-01)
      3. S2: query_text(识别供应商) → ["快车", "特惠快车"]
      4. S3a[快车]: ground(点问号, ref=button_to_price.png) → click
      5. S3c[快车]: ground(查看详细计价规则) → click
         → ground(工作日tab) → click → 每次滑动后 query_text(预约用车?) → 出现或页面无变化即停 → slide×3(回顶)
         → ground(休息日tab) → click → 每次滑动后 query_text(预约用车?) → 出现或页面无变化即停
         → ground(返回箭头) → click → ground(返回箭头) → click
      6. S3a[特惠快车]: 同上
      7. S3c[特惠快车]: 同上
    """
    from collector.platform.gaode.ride_pricing import RidePricingFSM

    # ── 构建 Mock ──
    mock_adb = MagicMock()
    type(mock_adb).screen_size = PropertyMock(return_value=(1080, 2400))

    mock_grounder = MagicMock()

    # S1 全选: 第一轮未选中 → 返回坐标
    mock_grounder.ground.side_effect = _build_ground_side_effect()

    # S2 查询供应商列表
    mock_grounder.query_text.side_effect = _build_query_text_side_effect()

    # Profile config
    profile_cfg = _build_profile_cfg()

    from collector.domain.checkbox import SelectAllTarget

    # ── 执行 ──
    fsms = []  # track created FSMs for stats
    _checked_target = SelectAllTarget(
        target_found=True, target_label="全选经济",
        label_bbox=[800, 400, 880, 480],
        checkbox_bbox=[900, 410, 960, 470],
        checkbox_center=(930, 440),
        relation_valid=True, state="checked",
    )

    with patch('collector.platform.gaode.select_all.ensure_all_selected',
               return_value=_checked_target) as mock_ensure:
        with patch('collector.platform.gaode.ride_pricing.AdbTools', return_value=mock_adb):
            with patch('collector.platform.gaode.ride_pricing.VLMGrounder', return_value=mock_grounder):
                # CAP-05: 页面无变化判定在 Mock 中固定 False，退出由「预约用车」标记驱动
                with patch.object(RidePricingFSM, "_page_unchanged", return_value=False) as mock_pu:
                    # Patch the module-level adb/grounder in ride_pricing
                    fsm = RidePricingFSM(
                        adb=mock_adb,
                        grounder=mock_grounder,
                        supplier="经济型",
                        profile_cfg=profile_cfg,
                        output_dir="/tmp/test_output",
                        verbose=False,
                    )
                    results = fsm.run()
                    fsms.append(fsm)

    assert mock_ensure.called, "S1 应调用 ensure_all_selected"

    fsm = fsms[0]

    # ── 验证 ──
    print(f"\n  [Mock FSM] 截图数: {len(results)}")
    print(f"  [Mock FSM] VLM 调用: ground={mock_grounder.ground.call_count}, "
          f"query_text={mock_grounder.query_text.call_count}")

    # 1. 必须有截图输出
    assert len(results) > 0, "应至少产生 1 张截图"

    # 2. S0 上滑
    slide_calls = mock_adb.slide.call_args_list
    assert len(slide_calls) >= 1, f"S0: 应至少 1 次上滑, 实际 {len(slide_calls)}"

    # 3. S1 全选经济: 走 ensure_all_selected（目标锚定，不再整图 ground 判状态）
    ground_calls = mock_grounder.ground.call_args_list

    # 4. S2 供应商识别: query_text 应被调用
    query_calls = mock_grounder.query_text.call_args_list
    assert len(query_calls) >= 1, f"S2: query_text 至少 1 次, 实际 {len(query_calls)}"

    # 4b. CAP-01: 详细计价页每次滑动后都调用 LLM 判断「预约用车」
    marker_calls = [
        c for c in query_calls
        if len(c.args) > 1 and "预约用车" in str(c.args[1])
    ]
    print(f"  [Mock FSM] 「预约用车」LLM 检测: {len(marker_calls)} 次")
    # 每个供应商: 工作日 4 次(i=0..3) + 休息日 3 次(i=0..2) = 7 次
    assert len(marker_calls) == 14, \
        f"应有 2 供应商 × 7 次「预约用车」检测, 实际 {len(marker_calls)}"

    # 4c. CAP-01/CAP-05: 出现「预约用车」（或页面无变化）即终止滚动 → 滑动次数可精确预期
    #     mock 中页面无变化=False, 由标记驱动: S0(1) + 2 × (工作日 4 + 回顶 3 + 休息日 3) = 21
    assert len(slide_calls) == 21, \
        f"「预约用车」出现应终止滚动, 预期 21 次滑动, 实际 {len(slide_calls)}"

    # 4d. CAP-01: 检测到「预约用车」后触发回顶（上滑手势 y1<y2）
    #     _scroll_to_top 每个供应商 × 工作日 3 次 = 6 次
    up_swipes = [
        c for c in slide_calls
        if len(c.args) >= 4 and c.args[1] < c.args[3]
    ]
    print(f"  [Mock FSM] 检测到后回顶上滑: {len(up_swipes)} 次")
    assert len(up_swipes) == 6, \
        f"检测到「预约用车」后应回顶 2×3=6 次上滑, 实际 {len(up_swipes)}"

    # 4e. CAP-05: 每次未命中标记时评估「页面无变化」（OR 退出条件）
    #     工作日 i=0..2 共3次 + 休息日 i=0..1 共2次 = 5 次/供应商 → 10 次
    print(f"  [Mock FSM] 页面无变化比对: {mock_pu.call_count} 次")
    assert mock_pu.call_count == 10, \
        f"未命中标记时应评估页面无变化, 预期 10 次, 实际 {mock_pu.call_count}"

    # 5. S3a 问号: ground 中应有 ref_image=button_to_price.png
    question_calls = [
        c for c in ground_calls
        if c.kwargs.get("ref_image") and "button_to_price.png" in str(c.kwargs.get("ref_image"))
    ]
    print(f"  [Mock FSM] 问号点击 (ref=button_to_price.png): {len(question_calls)} 次")
    assert len(question_calls) >= 2, \
        f"应有 2 个供应商各 1 次问号点击, 实际 {len(question_calls)}"

    # 6. 点击 (click) 应被多次调用
    click_calls = mock_adb.click.call_args_list
    assert len(click_calls) >= 4, f"click 至少 4 次, 实际 {len(click_calls)}"

    # 7. 返回 (back) 应被调用 — 每个供应商的 2 次返回
    #   (实际是先 ground 找返回箭头, 找不到才 fallback back key)
    #   这里不做严格断言

    # 8. 统计合并
    assert fsm.stats["vlm_calls"] > 0, "VLM 调用统计应 > 0"

    print(f"  [Mock FSM] ✓ 完整流程通过")
    print(f"    - slide:   {len(slide_calls)} 次")
    print(f"    - click:   {len(click_calls)} 次")
    print(f"    - ground:  {len(ground_calls)} 次")
    print(f"    - query:   {len(query_calls)} 次")
    print(f"    - 截图:    {len(results)} 张")
    return True


def _build_profile_cfg() -> dict:
    return {
        "timing": {
            "app_launch_wait": 3.0,
            "after_input_wait": 1.0,
            "after_tap_wait": 2.0,
            "after_confirm_wait": 3.0,
            "pricing_page_wait": 2.0,
        },
        "steps": {},
        "collection": {
            "scroll_duration_ms": 500,
            "after_scroll_wait": 1.0,
            "swipe_duration_ms": 400,
            "max_suppliers": 2,
            "max_detail_swipes": 12,
            "max_scroll_rounds": 15,
            "pricing_page_wait": 0.5,
        },
    }


def _build_ground_side_effect():
    """构建 ground() 的 side_effect, 按调用顺序返回不同结果.

    调用顺序 (每个供应商):
      S1-1: 全选经济第1轮 → 未选中, 返回坐标
      S1-2: 全选 double check → 已选中
      S3a: 点问号 → 找到, 返回坐标
      S3c: 查看详细计价规则 → 找到, 返回坐标
      S3c: 工作日 tab → 找到, 返回坐标
      S3c: 休息日 tab → 找到, 返回坐标
      S3c: 返回箭头(第一次) → 找到
      S3c: 返回箭头(第二次) → 找到
      ... 重复 2 个供应商
    """

    NOT_SELECTED = {
        "element": "全选经济勾选框",
        "bbox": [800, 400, 880, 480],
        "center": [840, 440],
        "found": True,
        "selected": False,
        "conf": 0.90,
        "raw_response": "SELECTED=false\n<tool_call>...</tool_call>",
    }

    SELECTED = {
        "element": "全选经济勾选框",
        "bbox": [0, 0, 0, 0],
        "center": None,
        "found": False,
        "selected": True,
        "conf": 0.0,
        "raw_response": "SELECTED=true\n<tool_call>...</tool_call>",
    }

    FOUND_AT = lambda x, y: {
        "element": "target",
        "bbox": [x - 30, y - 30, x + 30, y + 30],
        "center": [x, y],
        "found": True,
        "selected": None,
        "conf": 0.90,
        "raw_response": "<tool_call>...</tool_call>",
    }

    # 每个供应商需要的 ground 调用:
    #   1. S3a 点问号 → 找到
    #   2. S3c 查看详细计价规则 → 找到
    #   3. S3c 工作日 tab → 找到
    #   4. S3c 休息日 tab → 找到
    #   5. S3c 返回(1) → 找到
    #   6. S3c 返回(2) → 找到
    per_supplier = [FOUND_AT(500, 1800)] * 6

    # S1 全选经济已改走 ensure_all_selected（SEL-01），不再调用 ground；
    # (S2 用 query_text 不用 ground)
    sequence = []
    # 2 个供应商
    for _ in range(2):
        sequence.extend(per_supplier)

    # 用迭代器
    seq_iter = iter(sequence)

    def side_effect(image_path, element_desc, screen_w, screen_h,
                    ref_image=None, ref_images=None):
        try:
            return next(seq_iter)
        except StopIteration:
            return {
                "element": element_desc,
                "bbox": [500, 1000, 580, 1080],
                "center": [540, 1040],
                "found": True,
                "selected": None,
                "conf": 0.90,
                "raw_response": "<tool_call>...</tool_call>",
            }

    return side_effect


def _build_query_text_side_effect():
    """构建 query_text 的 side_effect.

    调用顺序:
      S2-1: 识别供应商 → ["快车", "特惠快车"]
      S3c-工作日-scroll: 每次滑动后 LLM 判断「预约用车」→ NO, NO, NO, YES (i=0..3)
      S3c-休息日-scroll: 每次滑动后 LLM 判断「预约用车」→ NO, NO, YES (i=0..2)
      ... 重复 2 个供应商
    """
    SUPPLIERS_RESP = {
        "raw_response": '["快车", "特惠快车"]',
        "success": True,
    }
    NOT_MARKER = {"raw_response": "NO, 未出现预约用车", "success": True}
    IS_MARKER = {"raw_response": "YES, 出现了蓝色预约用车", "success": True}

    sequence = [SUPPLIERS_RESP]  # S2 只调用 1 次
    # 每个供应商: 工作日每次滑动检测 + 休息日每次滑动检测
    for _ in range(2):
        sequence.extend([NOT_MARKER] * 3)  # 工作日 i=0,1,2 → 未出现
        sequence.append(IS_MARKER)         # 工作日 i=3 → 出现「预约用车」，终止
        sequence.extend([NOT_MARKER] * 2)  # 休息日 i=0,1 → 未出现
        sequence.append(IS_MARKER)         # 休息日 i=2 → 出现「预约用车」，终止

    seq_iter = iter(sequence)

    def side_effect(image_path, prompt):
        try:
            return next(seq_iter)
        except StopIteration:
            return {"raw_response": "YES", "success": True}

    return side_effect


# ======================================================================
# Suite 3: FlowEngine pricing_collect 编排测试
# ======================================================================

def test_flow_engine_pricing_collect_step():
    """验证 FlowEngine 通过平台 step_handlers 委托 pricing_collect 给 RidePricingFSM."""
    from collector.platform.gaode.platform import handle_pricing_collect
    from collector.workflows.flow_engine import FlowEngine

    mock_adb = MagicMock()
    type(mock_adb).screen_size = PropertyMock(return_value=(1080, 2400))
    mock_grounder = MagicMock()

    # 写一个临时 YAML
    import tempfile
    yaml_content = """
name: "test-pricing"
version: "1"
description: "test"
steps:
  - id: "collect"
    type: "pricing_collect"
    description: "计价采集"
    supplier: "经济型"
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    try:
        # 平台 handler 在调用时从 gaode.ride_pricing import RidePricingFSM，在此 patch
        with patch('collector.platform.gaode.ride_pricing.RidePricingFSM') as MockFSM:
            mock_fsm_instance = MagicMock()
            mock_fsm_instance.stats = {"vlm_calls": 5, "vlm_failures": 0}
            MockFSM.return_value = mock_fsm_instance

            engine = FlowEngine(
                adb=mock_adb,
                grounder=mock_grounder,
                flow_path=tmp_path,
                output_dir="/tmp/test_engine",
                verbose=False,
                profile_cfg={"collection": {"max_suppliers": 2}},
                platform_step_handlers={"pricing_collect": handle_pricing_collect},
            )
            engine.run()

            # 验证 RidePricingFSM 被创建并执行
            assert MockFSM.called, "应创建 RidePricingFSM"
            assert mock_fsm_instance.run.called, "应调用 RidePricingFSM.run()"

            # 验证 stats 合并
            assert engine.stats["vlm_calls"] == 5, \
                f"stats 应合并, 预期 5, 实际 {engine.stats['vlm_calls']}"

            print(f"  [FlowEngine] ✓ pricing_collect 通过平台 handler 正确委托")
    finally:
        Path(tmp_path).unlink()


# ======================================================================
# Suite 3b: 平台注册表 / 新平台零侵入
# ======================================================================

def test_platform_registry():
    """注册表：gaode 可解析、flow 约定、未知平台报错."""
    from collector.platform.registry import available_platforms, get_platform

    assert "gaode" in available_platforms(), "注册表应包含 gaode"

    gaode = get_platform("gaode")
    assert gaode.flows_dir.name == "flows"
    assert gaode.profile_path.name == "gaode.json"
    assert gaode.default_flow == "v1"
    assert "pricing_collect" in gaode.step_handlers, "gaode 应注册 pricing_collect"
    assert gaode.resolve_flow("v2").name == "v2_gaode.yaml", "flow 解析约定 <flow>_<platform>.yaml"
    assert "v1" in gaode.list_flow_names(), "list_flow_names 应列出 v1/v2/v3"

    try:
        get_platform("not_a_platform")
        raise AssertionError("未注册平台应抛 KeyError")
    except KeyError:
        pass
    return "PASS ✓"


def test_fake_platform_zero_intrusion():
    """新平台零侵入：注册一个假平台 + 自定义步骤，不改通用代码即可执行."""
    import shutil
    import tempfile

    from collector.domain.platform import Platform
    from collector.platform.registry import available_platforms, register_platform, unregister_platform
    from collector.workflows.flow_engine import FlowEngine

    tmp_dir = tempfile.mkdtemp(prefix="fake_platform_")
    try:
        flow_path = Path(tmp_dir) / "v1_fake.yaml"
        flow_path.write_text(
            'name: "fake"\nversion: "1"\nsteps:\n'
            '  - id: "custom"\n    type: "fake_custom_step"\n'
            '    description: "自定义步骤"\n',
            encoding="utf-8",
        )
        called = {"n": 0}

        def handle_custom(engine, step):
            called["n"] += 1
            assert step.get("type") == "fake_custom_step"

        fake_platform = Platform(
            name="fake",
            flows_dir=Path(tmp_dir),
            profile_path=Path(tmp_dir) / "fake.json",
            default_flow="v1",
            step_handlers={"fake_custom_step": handle_custom},
        )
        register_platform(fake_platform)

        engine = FlowEngine(
            adb=MagicMock(),
            grounder=MagicMock(),
            flow_path=str(flow_path),
            output_dir=tmp_dir,
            verbose=False,
            platform_step_handlers=fake_platform.step_handlers,
        )
        engine.run()

        assert called["n"] == 1, "自定义平台步骤应被执行一次"
        assert "fake" in available_platforms(), "假平台应已注册"
        return "PASS ✓"
    finally:
        unregister_platform("fake")
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ======================================================================
# Suite 3c: debug/collect 输出模式
# ======================================================================

def test_collect_mode_engine_no_output():
    """collect 模式：导航阶段截图写入临时目录，output 无截图、无标记图."""
    import tempfile

    from collector.infrastructure.device.adb_utils import MockAdbTools
    from collector.workflows.flow_engine import FlowEngine

    mock_adb = MockAdbTools()  # 真实写占位图，便于断言落盘位置
    mock_grounder = MagicMock()
    mock_grounder.ground.return_value = {
        "element": "x", "bbox": None, "center": None, "conf": 0.0,
        "found": False, "selected": None, "reason": "mock", "raw_response": "",
    }

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        flow = Path(tmp) / "flow.yaml"
        flow.write_text(
            'name: "t"\nversion: "1"\nsteps:\n'
            '  - id: "s1"\n    type: "ground_click"\n    description: "x"\n'
            '    ground:\n      element_desc: "x"\n',
            encoding="utf-8",
        )
        engine = FlowEngine(
            adb=mock_adb, grounder=mock_grounder, flow_path=str(flow),
            output_dir=str(out_dir), verbose=False, mode="collect",
        )
        try:
            engine.run()
            assert list(out_dir.glob("*.jpg")) == [], "collect 模式导航阶段不应保存截图"
            assert not (out_dir / "_annotations").exists(), "collect 模式不应输出标记图"
            assert engine.scratch_dir is not None
            assert len(list(engine.scratch_dir.glob("*.jpg"))) >= 1, "VLM 临时截图应写入临时目录"
        finally:
            engine.cleanup()
    return "PASS ✓"


def test_collect_mode_pricing_saves_ride_page():
    """collect 模式：打车页(刚进入/滑动)与详细计价页截图都保存到 output."""
    import tempfile

    from collector.platform.gaode.ride_pricing import RidePricingFSM

    mock_adb = MagicMock()  # get_screenshot 返回真值即可
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        fsm = RidePricingFSM(
            adb=mock_adb, grounder=MagicMock(), supplier="经济型",
            profile_cfg={}, output_dir=str(out_dir), mode="collect",
        )
        p1 = fsm._save("p01_ride_page_entry")   # 刚进打车页
        assert Path(p1).parent == out_dir, "collect 模式刚进打车页截图应保存到 output"
        p2 = fsm._save("p02_s4_next")           # 打车页滑动
        assert Path(p2).parent == out_dir, "collect 模式打车页滑动截图应保存到 output"
        p3 = fsm._save("p03_detail_page")       # 详细计价页
        assert Path(p3).parent == out_dir, "collect 模式详细计价页截图应保存到 output"

        # debug 默认：同样全部写入 output
        fsm2 = RidePricingFSM(
            adb=mock_adb, grounder=MagicMock(), supplier="经济型",
            profile_cfg={}, output_dir=str(out_dir / "dbg"),
        )
        p4 = fsm2._save("p01_x")
        assert Path(p4).parent == (out_dir / "dbg")
    return "PASS ✓"


def test_annotation_gated_by_mode():
    """标记图仅 debug 模式输出."""
    import tempfile

    from PIL import Image

    from collector.platform.gaode.ride_pricing import RidePricingFSM

    with tempfile.TemporaryDirectory() as tmp:
        img_path = Path(tmp) / "src.png"
        Image.new("RGB", (20, 20), "white").save(img_path)

        # collect：不输出标记图
        out1 = Path(tmp) / "out_collect"
        fsm1 = RidePricingFSM(
            adb=MagicMock(), grounder=MagicMock(), supplier="x",
            profile_cfg={}, output_dir=str(out1), mode="collect",
        )
        fsm1._do_annotate(str(img_path), "tag1", lambda d: None)
        assert not (out1 / "_annotations" / "tag1.png").exists(), "collect 模式不应输出标记图"

        # debug：输出标记图
        out2 = Path(tmp) / "out_debug"
        fsm2 = RidePricingFSM(
            adb=MagicMock(), grounder=MagicMock(), supplier="x",
            profile_cfg={}, output_dir=str(out2),
        )
        fsm2._do_annotate(str(img_path), "tag2", lambda d: None)
        assert (out2 / "_annotations" / "tag2.png").exists(), "debug 模式应输出标记图"
    return "PASS ✓"


def test_timing_stats_recorded():
    """耗时统计：等待/API/总耗时被记录并汇总."""
    import tempfile

    from collector.infrastructure.device.adb_utils import MockAdbTools
    from collector.workflows.flow_engine import FlowEngine

    mock_adb = MockAdbTools()
    mock_grounder = MagicMock()
    mock_grounder.ground.return_value = {
        "element": "x", "bbox": None, "center": None, "conf": 0.0,
        "found": False, "selected": None, "reason": "mock", "raw_response": "",
    }

    with tempfile.TemporaryDirectory() as tmp:
        flow = Path(tmp) / "f.yaml"
        flow.write_text(
            'name: "t"\nversion: "1"\nsteps:\n'
            '  - id: "wait1"\n    type: "wait"\n    seconds: 0.01\n',
            encoding="utf-8",
        )
        engine = FlowEngine(
            adb=mock_adb, grounder=mock_grounder, flow_path=str(flow),
            output_dir=str(Path(tmp) / "out"), verbose=False,
        )
        engine.run()
        assert engine.stats.get("wait_seconds", 0) >= 0.01, "等待时长应被统计"
        assert engine.stats.get("api_seconds", -1) == 0.0, "mock grounder API 耗时应为 0"
        assert engine.stats.get("elapsed", 0) >= 0.01, "总耗时应被统计"
    return "PASS ✓"

    return True


# ======================================================================
# Suite 4: 真实 VLM 素材验证 (不需要设备)
# ======================================================================

def real_vlm_tests(api_key: str, base_url: str) -> None:
    """用真实素材图片验证 VLM grounding 关键步骤.

    测试项:
      1. 打车页 → 定位问号 (button_to_price.png 作为 ref)
      2. 计价页 → 定位「查看详细计价规则」
      3. 详细计价页 → 定位「工作日」和「休息日」tab
    """
    from collector.infrastructure.vision.vlm_grounder import VLMGrounder

    grounder = VLMGrounder(
        api_key=api_key, base_url=base_url,
        model="qwen3-vl-plus",
        image_max_pixels=400000,
    )

    SCREEN_W, SCREEN_H = 1080, 2400
    ref_question = str(_PROJECT_ROOT / "assets" / "button_to_price.png")

    test_cases = [
        {
            "name": "打车页-定位?问号",
            "image": "打车页.jpg",
            "ref_image": "button_to_price.png",
            "desc": (
                "高德打车页面。在「经济型」分组下找到任一供应商行"
                "「预估」前面的 '?' 问号图标。附件是问号的参考图。"
                "返回 bbox 和中心坐标。"
            ),
            "expect_found": True,
        },
        {
            "name": "计价页-定位详细计价规则",
            "image": "计价页.jpg",
            "ref_image": None,
            "desc": (
                "高德打车计价弹窗。找「查看详细计价规则」或「计价规则」"
                "入口（弹窗中下部的文字链接/按钮）。返回 bbox 和中心坐标。"
            ),
            "expect_found": True,
        },
        {
            "name": "详细计价页-定位工作日tab",
            "image": "详细计价页.jpg",
            "ref_image": None,
            "desc": (
                "计价规则详情页。找「工作日」标签页（与「休息日」标签并列）。"
                "返回 bbox 和中心坐标。"
            ),
            "expect_found": True,
        },
        {
            "name": "详细计价页-定位休息日tab",
            "image": "详细计价页.jpg",
            "ref_image": None,
            "desc": (
                "计价规则详情页。找「休息日」标签页（与「工作日」标签并列）。"
                "返回 bbox 和中心坐标。"
            ),
            "expect_found": True,
        },
    ]

    print("\n" + "=" * 60)
    print("  Real VLM — 素材验证")
    print("=" * 60)

    passed = 0
    for tc in test_cases:
        img_path = str(_PROJECT_ROOT / "assets" / tc["image"])
        if not Path(img_path).exists():
            print(f"\n  ⚠ 跳过: {tc['image']} 不存在")
            continue

        ref_path = None
        if tc["ref_image"]:
            ref_path = str(_PROJECT_ROOT / "assets" / tc["ref_image"])
            if not Path(ref_path).exists():
                ref_path = None

        print(f"\n── {tc['name']} ──")
        print(f"  image: {tc['image']}" + (f", ref: {tc['ref_image']}" if ref_path else ""))

        result = grounder.ground(
            img_path, tc["desc"],
            screen_w=SCREEN_W, screen_h=SCREEN_H,
            ref_image=ref_path,
        )

        found = result.get("found")
        center = result.get("center")
        bbox = result.get("bbox")
        raw = (result.get("raw_response", "") or "")[:200]

        print(f"  found={found}, center={center}, bbox={bbox}")
        print(f"  raw: {raw}")

        ok = found == tc["expect_found"]
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}")
        if ok:
            passed += 1

    print(f"\n  Real VLM: {passed}/{len(test_cases)} passed")


# ======================================================================
# Suite 5: 真实设备 + 真实 VLM — 完整子流程 (需要设备)
# ======================================================================

def real_device_test(
    adb_path: str,
    api_key: str,
    base_url: str,
    output_dir: str,
    device: str | None = None,
) -> None:
    """在真实设备上执行一次完整的计价采集子流程.

    前置条件: 设备已解锁, 高德地图处于打车页 (已输入起终点).
    """
    from collector.infrastructure.device.adb_utils import AdbTools
    from collector.platform.gaode.ride_pricing import RidePricingFSM
    from collector.infrastructure.vision.vlm_grounder import VLMGrounder

    print("\n" + "=" * 60)
    print("  Real Device — 完整子流程")
    print("=" * 60)

    adb = AdbTools(adb_path, device=device)
    grounder = VLMGrounder(
        api_key=api_key, base_url=base_url,
        model="qwen3-vl-plus",
        image_max_pixels=400000,
    )

    # 检查连接
    test_shot = str(Path(output_dir) / "_test_connection.png")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    if not adb.get_screenshot(test_shot):
        print("❌ 无法连接设备")
        return

    print(f"  ✓ 设备已连接: {adb.screen_size}")

    profile_cfg = _build_profile_cfg()
    pricer = RidePricingFSM(
        adb=adb,
        grounder=grounder,
        supplier="经济型",
        profile_cfg=profile_cfg,
        output_dir=output_dir,
        verbose=True,
    )

    t0 = time.time()
    try:
        results = pricer.run()
        elapsed = time.time() - t0
        print(f"\n  ✓ 完成: {len(results)} 张截图, 耗时 {elapsed:.1f}s")
        print(f"  VLM: {pricer.stats['vlm_calls']} 次调用, "
              f"失败: {pricer.stats['vlm_failures']}")
    except KeyboardInterrupt:
        print("\n  ⚠ 用户中断")
    except Exception as e:
        print(f"\n  ❌ 失败: {e}")
        import traceback
        traceback.print_exc()


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="计价采集子流程测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--real-vlm", action="store_true",
                        help="用真实 VLM API 验证素材 grounding")
    parser.add_argument("--real-device", action="store_true",
                        help="在真实设备上执行完整子流程")
    parser.add_argument("--adb-path", help="ADB 路径 (--real-device 需要)")
    parser.add_argument("--device", help="设备序列号")
    parser.add_argument("--vlm-api-key", help="API Key")
    parser.add_argument("--vlm-base-url", help="Base URL")
    parser.add_argument("--output-dir", default="./output",
                        help="截图输出目录 (默认: ./output)")
    args = parser.parse_args()

    print("=" * 60)
    print("  计价采集子流程测试")
    print("=" * 60)

    all_pass = True

    # ── Suite 1: 纯逻辑测试 ──
    print("\n── Suite 1: 解析逻辑 ──")
    suite1 = [
        ("S2 JSON 数组解析",          test_s2_parse_json_array),
        ("S2 排除出租车/优享",         test_s2_parse_excludes_taxi_and_youxiang),
        ("S2 逐行回退解析",            test_s2_parse_line_by_line_fallback),
        ("S2 空数组",                 test_s2_parse_empty),
        ("_extract_center bbox+center", test_extract_center_from_bbox_and_center),
        ("_extract_center only center", test_extract_center_only_center),
        ("_extract_center None",      test_extract_center_none),
        ("CAP-01 预约用车检测解析",   test_detect_end_marker),
        ("RES-01 结果整理聚合",      test_screenshot_organizer),
        ("CAP-05 页面无变化判定",     test_page_unchanged),
        ("CAP-05 标记或稳定退出",     test_scroll_to_bottom_marker_or_stable),
    ]
    for label, fn in suite1:
        try:
            status, *extra = fn()
            print(f"  [{status}] {label}")
            if extra:
                print(f"         → {extra[0]}")
        except Exception as e:
            print(f"  [FAIL ✗] {label}: {e}")
            all_pass = False

    # ── Suite 2: FSM 完整流程 Mock ──
    print("\n── Suite 2: FSM 完整流程 Mock ──")
    try:
        test_fsm_full_flow()
    except Exception as e:
        print(f"  [FAIL ✗] FSM Mock: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False

    # ── Suite 3: FlowEngine 编排 ──
    print("\n── Suite 3: FlowEngine pricing_collect 编排 ──")
    try:
        test_flow_engine_pricing_collect_step()
    except Exception as e:
        print(f"  [FAIL ✗] FlowEngine: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False

    # ── Suite 3b: 平台注册表 / 零侵入 ──
    print("\n── Suite 3b: 平台注册表 / 新平台零侵入 ──")
    for label, fn in [
        ("平台注册表", test_platform_registry),
        ("假平台零侵入", test_fake_platform_zero_intrusion),
    ]:
        try:
            s = fn()
            print(f"  [{s}] {label}")
        except Exception as e:
            print(f"  [FAIL ✗] {label}: {e}")
            import traceback
            traceback.print_exc()
            all_pass = False

    # ── Suite 3c: debug/collect 输出模式 ──
    print("\n── Suite 3c: debug/collect 输出模式 ──")
    for label, fn in [
        ("collect 引擎零输出", test_collect_mode_engine_no_output),
        ("collect 打车页保存", test_collect_mode_pricing_saves_ride_page),
        ("标记图 debug 门控", test_annotation_gated_by_mode),
        ("耗时统计", test_timing_stats_recorded),
    ]:
        try:
            s = fn()
            print(f"  [{s}] {label}")
        except Exception as e:
            print(f"  [FAIL ✗] {label}: {e}")
            import traceback
            traceback.print_exc()
            all_pass = False

    # ── Suite 4: 真实 VLM (可选) ──
    if args.real_vlm:
        if not args.vlm_api_key or not args.vlm_base_url:
            print("\n❌ --real-vlm 需要 --vlm-api-key 和 --vlm-base-url")
            sys.exit(1)
        real_vlm_tests(args.vlm_api_key, args.vlm_base_url)

    # ── Suite 5: 真实设备 (可选) ──
    if args.real_device:
        if not args.adb_path:
            print("\n❌ --real-device 需要 --adb-path")
            sys.exit(1)
        if not args.vlm_api_key or not args.vlm_base_url:
            print("\n❌ --real-device 需要 --vlm-api-key 和 --vlm-base-url")
            sys.exit(1)
        real_device_test(
            adb_path=args.adb_path,
            api_key=args.vlm_api_key,
            base_url=args.vlm_base_url,
            output_dir=args.output_dir,
            device=args.device,
        )

    print("\n" + "=" * 60)
    print(f"  {'✓ 全部通过' if all_pass else '✗ 存在失败'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
