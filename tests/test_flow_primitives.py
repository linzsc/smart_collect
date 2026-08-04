"""
FlowEngine 流程原语测试（WS-1 P2）
============================================================================

验证新增流程原语：
  - extract_list（内置 JSON 解析 / 平台 handler）
  - for_each（state 遍历 + 模板渲染 + max）
  - loop_until（until_state / until_var / until_handler / max_rounds）
  - subflow（内联执行子流程 YAML）
  - verify（expect_state / handler）+ back
  - scroll_until_visible 增强（frame_suffix / stop_on_stable / scroll_back_to_top）
  - v2_gaode.yaml 端到端见 tests/test_pricing_collect.py（v2 计价子流程）

用法:
  .venv/bin/python tests/test_flow_primitives.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collector.infrastructure.device.adb_utils import MockAdbTools
from collector.workflows.flow_engine import FlowEngine, StepFailed


class _MockAdbFixed(MockAdbTools):
    """MockAdbTools + 固定屏幕尺寸（避免 1x1 占位截图覆盖 image_info）。"""

    @property
    def screen_size(self):
        return (1080, 2400)


# ======================================================================
# Helpers
# ======================================================================

def _make_engine(yaml_text: str, *, vars_=None, platform_step_handlers=None,
                 profile_cfg=None, mode="debug", output_dir=None) -> FlowEngine:
    mock_adb = MagicMock()
    type(mock_adb).screen_size = PropertyMock(return_value=(1080, 2400))
    mock_grounder = MagicMock()
    mock_grounder.ground.return_value = {
        "element": "x", "bbox": [100, 100, 200, 200], "center": [150, 150],
        "conf": 0.9, "found": True, "selected": None, "reason": None, "raw_response": "",
    }
    mock_grounder.query_text.return_value = {"raw_response": "NO", "success": True}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        tmp = f.name
    engine = FlowEngine(
        adb=mock_adb, grounder=mock_grounder, flow_path=tmp,
        vars_=vars_ or {}, output_dir=output_dir or "/tmp/primitives_out",
        verbose=False, profile_cfg=profile_cfg or {},
        platform_step_handlers=platform_step_handlers or {},
        mode=mode,
    )
    engine._tmp_flow = tmp
    return engine


def _cleanup(engine) -> None:
    Path(engine._tmp_flow).unlink(missing_ok=True)


def _hit(x=100, y=200):
    return {"element": "x", "bbox": [x - 50, y - 50, x + 50, y + 50], "center": [x, y],
            "conf": 0.9, "found": True, "selected": None, "reason": None, "raw_response": ""}


def _miss():
    return {"element": "x", "bbox": None, "center": None, "conf": 0.0,
            "found": False, "selected": None, "reason": "not found", "raw_response": ""}


# ======================================================================
# Suite 1: _render 状态模板
# ======================================================================

def test_render_state_and_vars():
    e = _make_engine("name: t\nsteps: []")
    try:
        e.state["supplier"] = "曹操出行"
        e.vars["Address"] = "北京西站"
        assert e._render("找「{{.S.supplier}}」") == "找「曹操出行」"
        assert e._render("目的地 {{.Address}}") == "目的地 北京西站"
        assert e._render("无模板") == "无模板"
        assert e._render("{{.S.missing}}") == "{{.S.missing}}"   # 缺失保持原样
    finally:
        _cleanup(e)
    return "PASS ✓"


# ======================================================================
# Suite 2: extract_list
# ======================================================================

def test_extract_list_json_dict_builtin():
    yaml_text = """
name: t
steps:
  - id: "ext"
    type: "extract_list"
    var: "suppliers"
    meta_var: "ended"
    prompt: "识别供应商"
    parse: "json_dict"
    items_key: "suppliers"
    meta_key: "economy_ended"
    skip_keywords: ["快车"]
"""
    e = _make_engine(yaml_text)
    try:
        e.grounder.query_text.return_value = {
            "raw_response": '{"suppliers": ["曹操出行", "快车", "阳光出行"], "economy_ended": true}',
            "success": True,
        }
        e.run()
        assert e.state["suppliers"] == ["曹操出行", "阳光出行"], e.state["suppliers"]
        assert e.state["ended"] is True
        assert e.stats["vlm_calls"] == 1
    finally:
        _cleanup(e)
    return "PASS ✓"


def test_extract_list_json_array_builtin():
    yaml_text = """
name: t
steps:
  - id: "ext"
    type: "extract_list"
    var: "items"
    prompt: "列出"
    parse: "json_array"
"""
    e = _make_engine(yaml_text)
    try:
        e.grounder.query_text.return_value = {"raw_response": '```json\n["A", "B"]\n```', "success": True}
        e.run()
        assert e.state["items"] == ["A", "B"]
    finally:
        _cleanup(e)
    return "PASS ✓"


def test_extract_list_handler_dispatch():
    yaml_text = """
name: t
steps:
  - id: "ext"
    type: "extract_list"
    handler: "fake_extract"
"""
    def fake_extract(engine, step):
        engine.state["suppliers"] = ["A", "B"]
        engine.state["economy_ended"] = False
    e = _make_engine(yaml_text, platform_step_handlers={"fake_extract": fake_extract})
    try:
        e.run()
        assert e.state["suppliers"] == ["A", "B"]
    finally:
        _cleanup(e)
    return "PASS ✓"


def test_extract_list_unknown_handler_raises():
    yaml_text = """
name: t
steps:
  - id: "ext"
    type: "extract_list"
    handler: "nope"
"""
    e = _make_engine(yaml_text)
    try:
        try:
            e.run()
        except StepFailed:
            return "PASS ✓"
        raise AssertionError("应抛 StepFailed")
    finally:
        _cleanup(e)


# ======================================================================
# Suite 3: for_each
# ======================================================================

def test_for_each_iterates_state_with_render():
    yaml_text = """
name: t
steps:
  - id: "each"
    type: "for_each"
    items: "suppliers"
    item: "supplier"
    index: "idx"
    steps:
      - id: "tap_{{.S.supplier}}"
        type: "ground_click"
        optional: true
        wait_after: 0
        ground:
          element_desc: "找「{{.S.supplier}}」问号"
"""
    e = _make_engine(yaml_text)
    try:
        e.state["suppliers"] = ["曹操出行", "阳光出行"]
        e.run()
        descs = [c.args[1] for c in e.grounder.ground.call_args_list]
        assert any("曹操出行" in d for d in descs), descs
        assert any("阳光出行" in d for d in descs), descs
        assert e.state["supplier"] == "阳光出行"   # item_var=supplier
        assert e.state["idx"] == 1
    finally:
        _cleanup(e)
    return "PASS ✓"


def test_for_each_literal_items_and_max():
    yaml_text = """
name: t
steps:
  - id: "each"
    type: "for_each"
    items: ["工作日", "休息日", "周末"]
    item: "tab"
    max: 2
    steps:
      - id: "click_{{.S.tab}}"
        type: "ground_click"
        optional: true
        wait_after: 0
        ground:
          element_desc: "点「{{.S.tab}}」"
"""
    e = _make_engine(yaml_text)
    try:
        e.run()
        assert e.grounder.ground.call_count == 2, e.grounder.ground.call_count
        assert e.state["tab"] == "休息日"
    finally:
        _cleanup(e)
    return "PASS ✓"


# ======================================================================
# Suite 4: loop_until
# ======================================================================

def test_loop_until_until_state():
    yaml_text = """
name: t
steps:
  - id: "loop"
    type: "loop_until"
    until_state: {done: true}
    max_rounds: 5
    steps:
      - id: "mark"
        type: "screenshot"
"""
    e = _make_engine(yaml_text)
    try:
        e.state["done"] = True
        e.run()
        # 第一轮执行后立即满足 → 只执行 1 轮（含 mark 截图）
        assert e.state["done"] is True
    finally:
        _cleanup(e)
    return "PASS ✓"


def test_loop_until_max_rounds():
    yaml_text = """
name: t
steps:
  - id: "loop"
    type: "loop_until"
    until_var: "done"
    max_rounds: 3
    steps:
      - id: "mark"
        type: "screenshot"
"""
    e = _make_engine(yaml_text)
    try:
        e.run()   # done 永不满足 → 3 轮后停（含 loop 容器步骤 = 1 + 3 轮）
        assert e.stats["steps_executed"] == 4, e.stats["steps_executed"]
    finally:
        _cleanup(e)
    return "PASS ✓"


def test_loop_until_until_handler():
    yaml_text = """
name: t
steps:
  - id: "loop"
    type: "loop_until"
    until_handler: "fake_done"
    max_rounds: 10
    steps:
      - id: "mark"
        type: "screenshot"
"""
    counter = {"n": 0}
    def fake_done(engine, step) -> bool:
        counter["n"] += 1
        return counter["n"] >= 2
    e = _make_engine(yaml_text, platform_step_handlers={"fake_done": fake_done})
    try:
        e.run()
        assert counter["n"] == 2, counter
        assert e.stats["steps_executed"] == 3, e.stats["steps_executed"]
    finally:
        _cleanup(e)
    return "PASS ✓"


# ======================================================================
# Suite 5: subflow
# ======================================================================

def test_subflow_runs_inline():
    with tempfile.TemporaryDirectory() as tmp:
        sub = Path(tmp) / "sub.yaml"
        sub.write_text(
            "name: sub\nsteps:\n"
            '  - id: "sub_mark"\n    type: "screenshot"\n',
            encoding="utf-8",
        )
        yaml_text = f"""
name: t
steps:
  - id: "main"
    type: "subflow"
    file: "{sub}"
"""
        e = _make_engine(yaml_text)
        try:
            e.run()
            assert e.stats["steps_executed"] == 2, e.stats["steps_executed"]
            # 截图编号在主流程上延续（同一 _shot_seq）
            assert e._shot_seq >= 1
        finally:
            _cleanup(e)
    return "PASS ✓"


def test_subflow_missing_file_raises():
    yaml_text = """
name: t
steps:
  - id: "bad"
    type: "subflow"
    file: "not_exist.yaml"
"""
    e = _make_engine(yaml_text)
    try:
        try:
            e.run()
        except StepFailed:
            return "PASS ✓"
        raise AssertionError("应抛 StepFailed")
    finally:
        _cleanup(e)


# ======================================================================
# Suite 6: verify / back
# ======================================================================

def test_verify_expect_state_pass_and_fail():
    yaml_text = """
name: t
steps:
  - id: "v"
    type: "verify"
    expect_state: {select_all_done: true}
"""
    e = _make_engine(yaml_text)
    try:
        e.state["select_all_done"] = True
        e.run()   # 通过
        e.state["select_all_done"] = False
        try:
            e.run()
        except StepFailed:
            return "PASS ✓"
        raise AssertionError("应抛 StepFailed")
    finally:
        _cleanup(e)


def test_verify_handler():
    yaml_text = """
name: t
steps:
  - id: "v"
    type: "verify"
    handler: "fake_verify"
"""
    def fake_verify(engine, step):
        return engine.state.get("ok", False)
    e = _make_engine(yaml_text, platform_step_handlers={"fake_verify": fake_verify})
    try:
        e.state["ok"] = True
        e.run()   # 通过
        e.state["ok"] = False
        try:
            e.run()
        except StepFailed:
            return "PASS ✓"
        raise AssertionError("应抛 StepFailed")
    finally:
        _cleanup(e)


def test_back_step():
    yaml_text = """
name: t
steps:
  - id: "b"
    type: "back"
    wait_after: 0
"""
    e = _make_engine(yaml_text)
    try:
        e.run()
        e.adb.back.assert_called_once()
    finally:
        _cleanup(e)
    return "PASS ✓"


# ======================================================================
# Suite 7: scroll_until_visible 增强
# ======================================================================

def test_scroll_until_visible_frame_suffix_and_stable():
    import tempfile as _tf

    yaml_text = """
name: t
steps:
  - id: "scroll_工作日"
    type: "scroll_until_visible"
    target_text: "预约用车"
    max_swipes: 12
    frame_suffix: "{{.S.supplier}}"
    stop_on_stable: true
    stable_threshold: 0.01
    wait_after_slide: 0
"""
    with _tf.TemporaryDirectory() as tmp:
        adb = _MockAdbFixed()
        grounder = MagicMock()
        grounder.query_text.return_value = {"raw_response": "NO", "success": True}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_text)
            tmp_flow = f.name
        try:
            e = FlowEngine(adb=adb, grounder=grounder, flow_path=tmp_flow,
                           output_dir=str(Path(tmp) / "out"), verbose=False)
            e.state["supplier"] = "曹操出行"
            e.run()
            shots = sorted(p.name for p in (Path(tmp) / "out" / "screenshots").glob("*.jpg"))
            # 标记始终未出现，页面无变化 → i=1 即停：scroll_0 / scroll_1
            assert any("_工作日_scroll_0_曹操出行.jpg" in n for n in shots), shots
            assert any("_工作日_scroll_1_曹操出行.jpg" in n for n in shots), shots
            assert e.stats["vlm_calls"] == 2, e.stats["vlm_calls"]
        finally:
            Path(tmp_flow).unlink(missing_ok=True)
    return "PASS ✓"


def test_scroll_until_visible_back_to_top():
    import tempfile as _tf

    yaml_text = """
name: t
steps:
  - id: "scroll_x"
    type: "scroll_until_visible"
    target_text: "预约用车"
    max_swipes: 3
    scroll_back_to_top: true
"""
    with _tf.TemporaryDirectory() as tmp:
        adb = _MockAdbFixed()
        grounder = MagicMock()
        grounder.query_text.return_value = {"raw_response": "YES", "success": True}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_text)
            tmp_flow = f.name
        try:
            e = FlowEngine(adb=adb, grounder=grounder, flow_path=tmp_flow,
                           output_dir=str(Path(tmp) / "out"), verbose=False)
            e.run()
            # 第 1 次检测即 YES → 不滑动，仅回顶 3 次上滑
            swipes = [a for a in adb.action_log
                      if a.get("type") == "adb_cmd" and "input swipe" in str(a.get("args", ""))]
            assert len(swipes) == 3, swipes
        finally:
            Path(tmp_flow).unlink(missing_ok=True)
    return "PASS ✓"


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    print("=" * 60)
    print("  Flow Primitives Tests (WS-1 P2)")
    print("=" * 60)

    suites = [
        ("Suite 1: _render", [
            ("state/vars 模板", test_render_state_and_vars),
        ]),
        ("Suite 2: extract_list", [
            ("json_dict + meta + 过滤", test_extract_list_json_dict_builtin),
            ("json_array", test_extract_list_json_array_builtin),
            ("handler 分发", test_extract_list_handler_dispatch),
            ("未知 handler 抛错", test_extract_list_unknown_handler_raises),
        ]),
        ("Suite 3: for_each", [
            ("state 遍历 + 渲染", test_for_each_iterates_state_with_render),
            ("字面列表 + max", test_for_each_literal_items_and_max),
        ]),
        ("Suite 4: loop_until", [
            ("until_state", test_loop_until_until_state),
            ("max_rounds", test_loop_until_max_rounds),
            ("until_handler", test_loop_until_until_handler),
        ]),
        ("Suite 5: subflow", [
            ("内联执行", test_subflow_runs_inline),
            ("文件缺失抛错", test_subflow_missing_file_raises),
        ]),
        ("Suite 6: verify/back", [
            ("expect_state 通过/失败", test_verify_expect_state_pass_and_fail),
            ("handler", test_verify_handler),
            ("back 步骤", test_back_step),
        ]),
        ("Suite 7: scroll_until_visible 增强", [
            ("frame_suffix + stop_on_stable", test_scroll_until_visible_frame_suffix_and_stable),
            ("scroll_back_to_top", test_scroll_until_visible_back_to_top),
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

    print(f"\n  通过 {total - failed}/{total}")
    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
