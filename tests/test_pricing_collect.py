"""
计价采集测试（WS-1 P3 重构版，无硬编码 FSM）
============================================================================

RidePricingFSM 已删除，计价采集由 YAML 子流程表达：
  subflows/pricing_collect_gaode.yaml + subflows/detail_capture_gaode.yaml

覆盖：
  - Suite 1: S2 响应解析（supplier_parse 单一实现）+ RES-01 结果整理
  - Suite 2: v2 端到端（导航 + 计价子流程，全 mock）
  - Suite 3: 平台注册表 / 新平台零侵入
  - Suite 4: debug/collect 输出模式 + 耗时统计
  - Suite 5: 真实 VLM 素材验证（可选）
  - Suite 6: 真实设备 v2 全流程（可选）

用法:
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
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collector.infrastructure.device.adb_utils import MockAdbTools


class _MockAdbFixed(MockAdbTools):
    """MockAdbTools + 固定屏幕尺寸（避免 1x1 占位截图覆盖 image_info）。"""

    @property
    def screen_size(self):
        return (1080, 2400)


def _hit(x=100, y=200):
    return {"element": "x", "bbox": [x - 50, y - 50, x + 50, y + 50], "center": [x, y],
            "conf": 0.9, "found": True, "selected": None, "reason": None, "raw_response": ""}


def _miss():
    return {"element": "x", "bbox": None, "center": None, "conf": 0.0,
            "found": False, "selected": None, "reason": "not found", "raw_response": ""}


# ======================================================================
# Suite 1: 纯逻辑测试
# ======================================================================

def test_s2_parse_json_array():
    """S2: VLM 返回标准 JSON 数组（代码块剥离；关键词过滤后保留目标运力商）"""
    from collector.platform.gaode.supplier_parse import parse_suppliers_response

    suppliers, ended = parse_suppliers_response('```json\n["曹操出行", "阳光出行"]\n```')
    assert suppliers == ["曹操出行", "阳光出行"], suppliers
    assert ended is False
    return "PASS ✓"


def test_s2_parse_excludes_taxi_and_youxiang():
    """S2/CAP-08/09: 排除快车/拼车/出租车/的士/优享等非目标运力商"""
    from collector.platform.gaode.supplier_parse import parse_suppliers_response

    suppliers, _ = parse_suppliers_response(
        '["曹操出行", "快车", "出租车", "北京的士", "北京新出租", "特惠快车", "优享", "拼车", "阳光出行"]'
    )
    assert "曹操出行" in suppliers
    assert "阳光出行" in suppliers
    for bad in ("快车", "特惠快车", "拼车", "出租车", "北京的士", "北京新出租", "优享"):
        assert bad not in suppliers, bad
    return "PASS ✓"


def test_s2_parse_line_by_line_fallback():
    """S2: JSON 解析失败时回退到逐行提取"""
    from collector.platform.gaode.supplier_parse import parse_suppliers_response

    suppliers, ended = parse_suppliers_response("1. 曹操出行\n2. 阳光出行")
    assert len(suppliers) == 2
    assert "曹操出行" in suppliers and "阳光出行" in suppliers
    assert ended is False
    return "PASS ✓"


def test_s2_parse_empty():
    """S2: VLM 返回空数组"""
    from collector.platform.gaode.supplier_parse import parse_suppliers_response

    suppliers, ended = parse_suppliers_response("[]")
    assert suppliers == []
    assert ended is False
    return "PASS ✓"


def test_s2_parse_cap09_dict_format():
    """CAP-09: 新 dict 格式（suppliers + economy_ended）+ 关键词过滤"""
    from collector.platform.gaode.supplier_parse import parse_suppliers_response

    suppliers, ended = parse_suppliers_response(
        '{"suppliers": ["曹操出行", "快车", "阳光出行"], "economy_ended": true}'
    )
    assert suppliers == ["曹操出行", "阳光出行"], suppliers
    assert ended is True

    # 旧数组格式 → economy_ended=False，全被过滤
    suppliers2, ended2 = parse_suppliers_response('["快车", "特惠快车", "拼车"]')
    assert suppliers2 == [] and ended2 is False

    # 空响应/非法 → ([], False)
    suppliers3, ended3 = parse_suppliers_response("")
    assert suppliers3 == [] and ended3 is False
    return "PASS ✓"


def test_extract_center_from_bbox_and_center():
    """GroundingResult center 语义：bbox+center 同时存在（结构化已归一化）"""
    from collector.domain.vision.models import GroundingResult
    r = GroundingResult(element="x", found=True, bbox=[100, 200, 300, 400],
                        center=(200, 300), confidence=0.9)
    assert r.has_geometry is True
    assert r.center == (200, 300)
    return "PASS ✓"


def test_extract_center_only_center():
    """GroundingResult center-only（bbox 缺失 → has_geometry=False，center 保留）"""
    from collector.domain.vision.models import GroundingResult
    r = GroundingResult(element="x", found=True, center=(500, 600))
    assert r.center == (500, 600)
    assert r.has_geometry is False
    return "PASS ✓"


def test_extract_center_none():
    """GroundingResult：全零几何归一化为 None（适配器语义，无盲点击）"""
    from collector.infrastructure.vision.adapters import grounding_result_from_dict
    r = grounding_result_from_dict({"element": "x", "bbox": [0, 0, 0, 0], "center": [0, 0]})
    assert r.center is None
    assert r.bbox is None
    return "PASS ✓"


def test_screenshot_organizer():
    """RES-01: 结果整理 — 筛选必要截图并按 工作日/休息日 × 运力商 聚合到 result/."""
    import tempfile

    from collector.platform.gaode.screenshot_organizer import collect_necessary_screenshots

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "output"
        res = Path(tmp) / "result"
        shots = out / "screenshots"
        shots.mkdir(parents=True)

        # 打车页（全选经济后）
        (shots / "p04_select_all_after.jpg").write_bytes(b"ride")
        # 各标签 × 各运力商 scroll_0..3
        cases = [
            ("p12", "工作日", "飞嘀打车", 6),   # 数量不固定：6 帧
            ("p22", "休息日", "飞嘀打车", 4),
            ("p34", "工作日", "旗妙出行", 4),
            ("p44", "休息日", "旗妙出行", 4),
        ]
        for prefix, tab, supplier, count in cases:
            base = int(prefix[1:])
            for i in range(count):
                (shots / f"p{base + i}_{tab}_scroll_{i}_{supplier}.jpg").write_bytes(b"s")
        # 干扰文件：不应被复制
        for name in (
            "p16_工作日_check_3_飞嘀打车.jpg",
            "p20_工作日_check_6_飞嘀打车.jpg",
            "p26_02_休息日_bottom_飞嘀打车.jpg",
            "p11_detail_before_工作日_飞嘀打车.jpg",
            "p05_after_select_all.jpg",
        ):
            (shots / name).write_bytes(b"x")

        summary = collect_necessary_screenshots(out, res)

        groups = summary["groups"]
        expected = {
            ("工作日", "飞嘀打车"): 6, ("工作日", "旗妙出行"): 4,
            ("休息日", "飞嘀打车"): 4, ("休息日", "旗妙出行"): 4,
        }
        assert set(groups) == {"工作日", "休息日"}, groups
        for tab in ("工作日", "休息日"):
            assert set(groups[tab]) == {"飞嘀打车", "旗妙出行"}, groups[tab]
            for supplier in ("飞嘀打车", "旗妙出行"):
                files = sorted(groups[tab][supplier])
                want = expected[(tab, supplier)]
                assert len(files) == want, f"{tab}/{supplier}: 应 {want} 张, 实际 {len(files)}"
                assert files[0].endswith(f"_scroll_0_{supplier}.jpg"), files

        # 目录结构：打车页入 <标签>/冒泡页/（每个大文件夹各 1 次，共 2 次）
        for tab in ("工作日", "休息日"):
            bubble = res / tab / "冒泡页"
            assert (bubble / "p04_select_all_after.jpg").exists(), f"{bubble} 缺打车页"
            for supplier in ("飞嘀打车", "旗妙出行"):
                folder = res / tab / supplier
                assert not (folder / "p04_select_all_after.jpg").exists(), \
                    f"{folder} 不应包含打车页"
                scrolls = list(folder.glob(f"*_{tab}_scroll_*_{supplier}.jpg"))
                assert len(scrolls) == expected[(tab, supplier)], f"{folder}: 滚动帧数量不符"

        # 干扰文件未复制
        assert not (res / "工作日" / "飞嘀打车" / "p16_工作日_check_3_飞嘀打车.jpg").exists()
        assert not (res / "工作日" / "飞嘀打车" / "p05_after_select_all.jpg").exists()

        # 打车页 1 张 × 2 大文件夹 + 滚动 (6+4+4+4) 张 = 20
        assert summary["copied"] == 20, f"copied={summary['copied']}"
    return "PASS ✓"


# ======================================================================
# Suite 1b: 循环终止条件（ARCH-17）
# ======================================================================

def test_pricing_loop_done_termination():
    """终止 = 满 10 个 或（特殊词出现且其上采完）或（下滑达 3 次且仍无未采集）。"""
    from unittest.mock import MagicMock as _MM

    from collector.platform.gaode.supplier_list import handle_pricing_loop_done

    engine = _MM()
    engine.profile_cfg = {"collection": {"max_suppliers": 10}}
    engine._log = lambda *a, **k: None

    def _state(processed, round_had, economy_ended, scroll_count=0):
        engine.state = {"_processed": set(processed), "round_had_suppliers": round_had,
                        "economy_ended": economy_ended, "scroll_count": scroll_count}

    # 有未采集供应商 → 继续
    _state({"A"}, round_had=True, economy_ended=False)
    assert handle_pricing_loop_done(engine, {}) is False

    # 采满 10 个 → 停止
    _state({f"s{i}" for i in range(10)}, round_had=True, economy_ended=False)
    assert handle_pricing_loop_done(engine, {}) is True

    # 特殊词出现且其上采完（空列表）→ 停止（不满 10 也算完成）
    _state({"A"}, round_had=False, economy_ended=True)
    assert handle_pricing_loop_done(engine, {}) is True

    # 特殊词出现但仍有未采集（其上还没采完）→ 继续
    _state({"A"}, round_had=True, economy_ended=True)
    assert handle_pricing_loop_done(engine, {}) is False

    # 未出现特殊词且空列表、下滑 < 3 → 继续（下滑找更多）
    _state({"A"}, round_had=False, economy_ended=False, scroll_count=2)
    assert handle_pricing_loop_done(engine, {}) is False

    # 未出现特殊词且空列表、下滑已达 3 次 → 兜底停止
    _state({"A"}, round_had=False, economy_ended=False, scroll_count=3)
    assert handle_pricing_loop_done(engine, {}) is True

    # 下滑达 3 次但有未采集 → 继续（兜底仅针对空列表）
    _state({"A"}, round_had=True, economy_ended=False, scroll_count=3)
    assert handle_pricing_loop_done(engine, {}) is False
    return "PASS ✓"


# ======================================================================
# Suite 1c: S2 下滑确认（ARCH-21）
# ======================================================================

def test_s2_handler_identify_no_scroll():
    """s2_list_suppliers（CAP-11）：只识别不内部下滑（下滑由 s4_scroll 步骤负责）；写 state。"""
    from unittest.mock import PropertyMock as _PM

    from collector.domain.vision import VisualQueryResult
    from collector.platform.gaode.supplier_list import handle_s2_list_suppliers

    def _mk(query_responses):
        engine = MagicMock()
        type(engine).debug_mode = _PM(return_value=False)
        engine.state = {}
        engine.stats = {"vlm_calls": 0, "vlm_failures": 0}
        engine._log = lambda *a, **k: None
        engine._screenshot = lambda *a, **k: "/fake/s2.jpg"
        engine.ctx.vision.query_text.side_effect = [
            VisualQueryResult(raw_response=r) for r in query_responses
        ]
        return engine

    # 场景1：有未采集供应商 → 不滑动，写 state
    e1 = _mk(['{"suppliers": ["曹操出行", "阳光出行"], "economy_ended": false}'])
    handle_s2_list_suppliers(e1, {"id": "s2"})
    assert e1.adb.slide.call_count == 0, "S2 不应内部下滑（由 s4_scroll 负责）"
    assert e1.state["suppliers"] == ["曹操出行", "阳光出行"], e1.state
    assert e1.state["round_had_suppliers"] is True
    assert e1.state["economy_ended"] is False

    # 场景2：已采集过的被过滤 → new 为空，economy_ended 透传
    e2 = _mk(['{"suppliers": ["曹操出行"], "economy_ended": true}'])
    e2.state["_processed"] = {"曹操出行"}
    handle_s2_list_suppliers(e2, {"id": "s2"})
    assert e2.state["suppliers"] == []
    assert e2.state["round_had_suppliers"] is False
    assert e2.state["economy_ended"] is True
    assert e2.adb.slide.call_count == 0
    return "PASS ✓"


# ======================================================================
# Suite 2: v2 端到端（导航 + 计价子流程，全 mock）
# ======================================================================

def test_v2_flow_end_to_end():
    """运行真实 v2_gaode.yaml + 计价/详情子流程（全 mock）。

    验证：导航 → subflow(select_all → verify → loop(每轮重新识别供应商 + 检查全选经济
    → 采集第一个新供应商) → organize) 全链路；不重复采集；标签坐标缓存（PERF-03）；
    截图命名与 result 聚合兼容。
    """
    import tempfile as _tf

    from collector.platform.gaode.platform import build_platform

    v2_path = _PROJECT_ROOT / "collector/platform/gaode/flows/v2_gaode.yaml"
    assert v2_path.exists(), v2_path

    def _ground_side_effect(image_path, element_desc, **kwargs):
        d = str(element_desc)
        if "查看详细计价规则" in d:
            return _hit(540, 1800)
        if "经济" in d and "导航" in d:   # jump_economy：左侧导航「经济」
            return _hit(180, 400)
        if "问号" in d or "'?'" in d:
            return _hit(800, 1200)
        if "工作日" in d or "休息日" in d:
            return _hit(400, 150)
        if "候选" in d:
            return _hit(540, 800)
        if "你要去哪儿" in d or "输入目的地" in d:
            return _hit(540, 500)
        if "上车点" in d:
            return _hit(540, 400)
        if "搜索" in d:
            return _hit(950, 200)
        if "打车" in d:
            return _hit(540, 2100)
        return _miss()

    def _query_side_effect(image_path, prompt):
        p = str(prompt)
        if "预约用车" in p:
            return {"raw_response": "NO", "success": True}
        return {"raw_response": '{"suppliers": ["曹操出行", "阳光出行"], "economy_ended": true}',
                "success": True}

    platform = build_platform()
    profile_cfg = platform.load_profile()

    with _tf.TemporaryDirectory() as tmp:
        adb = _MockAdbFixed()
        grounder = MagicMock()
        grounder.ground.side_effect = _ground_side_effect
        grounder.query_text.side_effect = _query_side_effect

        with patch("collector.platform.gaode.select_all.ensure_all_selected",
                   return_value=MagicMock()) as mock_ensure:
            engine = None
            from collector.workflows.flow_engine import FlowEngine
            engine = FlowEngine(
                adb=adb, grounder=grounder, flow_path=str(v2_path),
                vars_={"Address": "北京西站", "Pickup": "西北旺万象汇"},
                output_dir=str(Path(tmp) / "out"),
                verbose=False, profile_cfg=profile_cfg,
                platform_step_handlers=platform.step_handlers,
                mode="debug",
            )
            with patch("time.sleep"):   # 跳过真实等待，wait_seconds 仍累计
                engine.run()

        # 1. 计价子流程执行：循环终止（economy_ended=true）且两个供应商都被处理
        assert engine.state["_processed"] == {"曹操出行", "阳光出行"}, engine.state["_processed"]
        assert engine.state["economy_ended"] is True
        # 2. 全选状态被 verify 校验
        assert engine.state["select_all_done"] is True
        # 3. ground 描述渲染了供应商
        descs = [c.args[1] for c in grounder.ground.call_args_list]
        assert any("曹操出行" in d for d in descs), "缺少供应商模板渲染"
        assert any("阳光出行" in d for d in descs), "缺少供应商模板渲染"
        # 4. 标签坐标缓存（PERF-03）：首次 ground 后缓存，后续直接复用
        assert engine.state.get("tab_工作日") == [400, 150], engine.state
        assert engine.state.get("tab_休息日") == [400, 150], engine.state
        tab_grounds = [d for d in descs if "标签页" in d]
        assert len(tab_grounds) == 2, f"标签应仅首次 ground（2 次），实际 {len(tab_grounds)}"
        # 5. 截图命名与 result 聚合兼容
        shots = sorted(p.name for p in (Path(tmp) / "out" / "screenshots").glob("*.jpg"))
        assert any("_工作日_scroll_0_曹操出行.jpg" in n for n in shots), shots
        assert any("_休息日_scroll_0_阳光出行.jpg" in n for n in shots), shots
        # 6. VLM 统计
        assert engine.stats["vlm_calls"] > 0, engine.stats["vlm_calls"]
        # 7. 每次回到打车页都重新识别供应商（S2 至少 3 轮）+ 检查全选经济（至少 3 次）
        s2_calls = [c for c in grounder.query_text.call_args_list
                    if "经济型" in str(c.args[1])]
        assert len(s2_calls) >= 3, f"每轮应重新识别供应商, 实际 {len(s2_calls)} 次"
        assert mock_ensure.call_count >= 3, \
            f"每次回打车页应检查全选经济, 实际 {mock_ensure.call_count} 次"
        # 8. 不重复采集：每个供应商恰好被采集一次（_processed 无重复）
        assert engine.state["_processed"] == {"曹操出行", "阳光出行"}, engine.state["_processed"]
        engine.cleanup()
    return "PASS ✓"


# ======================================================================
# Suite 3: 平台注册表 / 新平台零侵入
# ======================================================================

def test_platform_registry():
    """注册表：gaode 可解析、flow 约定、未知平台报错."""
    from collector.platform.registry import available_platforms, get_platform

    assert "gaode" in available_platforms(), "注册表应包含 gaode"

    gaode = get_platform("gaode")
    assert gaode.flows_dir.name == "flows"
    assert gaode.profile_path.name == "gaode.json"
    assert gaode.default_flow == "v1"
    # 计价控制流已 YAML 化：pricing_collect 不再作为平台步骤（由 subflow 表达）
    assert "pricing_collect" not in gaode.step_handlers
    assert "select_all" in gaode.step_handlers
    assert gaode.resolve_flow("v2").name == "v2_gaode.yaml", "flow 解析约定 <flow>_<platform>.yaml"
    assert "v2" in gaode.list_flow_names(), "list_flow_names 应列出 v1/v2/v3"

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
# Suite 4: debug/collect 输出模式 + 耗时统计
# ======================================================================

def test_collect_mode_engine_no_output():
    """collect 模式：导航阶段截图写入临时目录，output 无截图、无标记图."""
    import tempfile

    from collector.workflows.flow_engine import FlowEngine

    mock_adb = _MockAdbFixed()
    mock_grounder = MagicMock()
    mock_grounder.ground.return_value = _miss()

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
            assert not (out_dir / "screenshots").exists() \
                or list((out_dir / "screenshots").glob("*.jpg")) == [], "collect 模式导航阶段不应保存截图"
            assert not (out_dir / "annotations").exists(), "collect 模式不应输出标记图"
            assert engine.scratch_dir is not None
            assert len(list(engine.scratch_dir.glob("*.jpg"))) >= 1, "VLM 临时截图应写入临时目录"
        finally:
            engine.cleanup()
    return "PASS ✓"


def test_timing_stats_recorded():
    """耗时统计：等待/API/总耗时被记录并汇总."""
    import tempfile

    from collector.workflows.flow_engine import FlowEngine

    mock_adb = _MockAdbFixed()
    mock_grounder = MagicMock()
    mock_grounder.ground.return_value = _miss()

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


# ======================================================================
# Suite 5: 真实 VLM 素材验证 (不需要设备)
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
# Suite 6: 真实设备 + 真实 VLM — v2 全流程 (需要设备)
# ======================================================================

def real_device_test(
    adb_path: str,
    api_key: str,
    base_url: str,
    output_dir: str,
    device: str | None = None,
) -> None:
    """在真实设备上执行一次完整的 v2 计价采集流程（YAML 子流程版）。"""
    from collector.infrastructure.device.adb_utils import AdbTools
    from collector.infrastructure.vision.vlm_grounder import VLMGrounder
    from collector.platform.gaode.platform import build_platform
    from collector.workflows.flow_engine import FlowEngine

    print("\n" + "=" * 60)
    print("  Real Device — v2 全流程（YAML 子流程版）")
    print("=" * 60)

    adb = AdbTools(adb_path, device=device)
    grounder = VLMGrounder(
        api_key=api_key, base_url=base_url,
        model="qwen3-vl-plus",
        image_max_pixels=400000,
    )

    # 检查连接
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    test_shot = str(Path(output_dir) / "_test_connection.png")
    if not adb.get_screenshot(test_shot):
        print("❌ 无法连接设备")
        return

    print(f"  ✓ 设备已连接: {adb.screen_size}")

    platform = build_platform()
    v2_path = platform.resolve_flow("v2")

    engine = FlowEngine(
        adb=adb,
        grounder=grounder,
        flow_path=str(v2_path),
        vars_={"Address": "北京西站", "Pickup": "我的位置"},
        output_dir=output_dir,
        verbose=True,
        profile_cfg=platform.load_profile(),
        platform_step_handlers=platform.step_handlers,
    )

    t0 = time.time()
    try:
        engine.run()
        elapsed = time.time() - t0
        print(f"\n  ✓ 完成, 耗时 {elapsed:.1f}s")
        print(f"  VLM: {engine.stats['vlm_calls']} 次调用, "
              f"失败: {engine.stats['vlm_failures']}")
    except KeyboardInterrupt:
        print("\n  ⚠ 用户中断")
    except Exception as e:
        print(f"\n  ❌ 失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        engine.cleanup()


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="计价采集测试（YAML 子流程版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--real-vlm", action="store_true",
                        help="用真实 VLM API 验证素材 grounding")
    parser.add_argument("--real-device", action="store_true",
                        help="在真实设备上执行 v2 全流程")
    parser.add_argument("--adb-path", help="ADB 路径 (--real-device 需要)")
    parser.add_argument("--device", help="设备序列号")
    parser.add_argument("--vlm-api-key", help="API Key")
    parser.add_argument("--vlm-base-url", help="Base URL")
    parser.add_argument("--output-dir", default="./output",
                        help="截图输出目录 (默认: ./output)")
    args = parser.parse_args()

    print("=" * 60)
    print("  计价采集测试（YAML 子流程版）")
    print("=" * 60)

    all_pass = True

    # ── Suite 1: 纯逻辑测试 ──
    print("\n── Suite 1: 解析/整理逻辑 ──")
    suite1 = [
        ("S2 JSON 数组解析",          test_s2_parse_json_array),
        ("循环终止条件(满10/经济型采完)", test_pricing_loop_done_termination),
        ("S2 识别不内部下滑", test_s2_handler_identify_no_scroll),
        ("S2 排除出租车/优享",         test_s2_parse_excludes_taxi_and_youxiang),
        ("S2 逐行回退解析",            test_s2_parse_line_by_line_fallback),
        ("S2 空数组",                 test_s2_parse_empty),
        ("CAP-09 S2 dict 格式解析",   test_s2_parse_cap09_dict_format),
        ("GroundingResult bbox+center", test_extract_center_from_bbox_and_center),
        ("GroundingResult only center", test_extract_center_only_center),
        ("GroundingResult 全零归一化", test_extract_center_none),
        ("RES-01 结果整理聚合",      test_screenshot_organizer),
    ]
    for label, fn in suite1:
        try:
            r = fn()
            if isinstance(r, tuple):
                status, *extra = r
            else:
                status, extra = r, []
            print(f"  [{status}] {label}")
            if extra:
                print(f"         → {extra[0]}")
        except Exception as e:
            print(f"  [FAIL ✗] {label}: {e}")
            all_pass = False

    # ── Suite 2: v2 端到端 ──
    print("\n── Suite 2: v2 端到端（导航 + 计价子流程 Mock） ──")
    try:
        print(f"  [{test_v2_flow_end_to_end()}] v2 计价采集全流程")
    except Exception as e:
        print(f"  [FAIL ✗] v2 端到端: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False

    # ── Suite 3: 平台注册表 / 零侵入 ──
    print("\n── Suite 3: 平台注册表 / 新平台零侵入 ──")
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

    # ── Suite 4: debug/collect 输出模式 ──
    print("\n── Suite 4: debug/collect 输出模式 + 耗时 ──")
    for label, fn in [
        ("collect 引擎零输出", test_collect_mode_engine_no_output),
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

    # ── Suite 5: 真实 VLM (可选) ──
    if args.real_vlm:
        if not args.vlm_api_key or not args.vlm_base_url:
            print("\n❌ --real-vlm 需要 --vlm-api-key 和 --vlm-base-url")
            sys.exit(1)
        real_vlm_tests(args.vlm_api_key, args.vlm_base_url)

    # ── Suite 6: 真实设备 (可选) ──
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
