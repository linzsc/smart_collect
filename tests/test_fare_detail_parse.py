"""详细计价页 UI 树解析 — 离线测试
================================================================

数据：tests/fixtures/fare_detail_weekday_ui_tree.json / fare_detail_weekend_ui_tree.json
     （2026-08-05 真机采集，含 WebView 坐标坍塌段）

用例：
  - parse : 起步价/里程费字段配对、坍塌跳过、预约用车截断
  - merge : 跨滚动拼接去重
  - write : result/{tab}/{supplier}/fare_detail.json
  - handler : fare_detail_ui 端到端（dump→解析→下滑→拼接→写 JSON）

用法:
  .venv/bin/python tests/test_fare_detail_parse.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collector.platform.gaode.fare_detail_parse import (
    FareDetail,
    fare_detail_to_dict,
    merge_fare_detail,
    parse_fare_detail_nodes,
    write_fare_detail_json,
)

_WEEKDAY = _THIS_DIR / "fixtures" / "fare_detail_weekday_ui_tree.json"
_WEEKEND = _THIS_DIR / "fixtures" / "fare_detail_weekend_ui_tree.json"


def _fixture(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["nodes"]


def _mk_node(text, x, y):
    return {"text": text, "content_desc": "", "class": "", "resource_id": "",
            "clickable": False, "checked": "false",
            "left": x, "top": y, "right": x + 60, "bottom": y + 40,
            "center": [x, y]}


# ---------------------------------------------------------------------------
# 1) 解析
# ---------------------------------------------------------------------------

def test_parse_weekday_fields():
    fd = parse_fare_detail_nodes(_fixture(_WEEKDAY), supplier="火箭出行", tab="工作日")
    secs = {s.title: s for s in fd.sections}
    assert "起步价" in secs and len(secs["起步价"].rows) == 8
    assert "里程费" in secs and len(secs["里程费"].rows) == 8
    # 起步价首行配对正确
    assert secs["起步价"].rows[0].period == "普通时段(3.0公里,11分钟)"
    assert secs["起步价"].rows[0].price == "14.58元"
    assert secs["里程费"].rows[0].price == "1.40元/公里"
    # WebView 坐标坍塌段被跳过
    assert fd.collapsed_skipped > 0
    assert fd.stopped_at_yuyue is False
    assert fd.mode == "实时用车"


def test_parse_weekend_partial():
    fd = parse_fare_detail_nodes(_fixture(_WEEKEND))
    secs = {s.title: s for s in fd.sections}
    assert len(secs["起步价"].rows) == 6
    assert len(secs["里程费"].rows) == 6
    assert "时长费" in secs and len(secs["时长费"].rows) == 3
    assert fd.collapsed_skipped > 0


def test_parse_stops_at_yuyue():
    nodes = [
        _mk_node("实时用车", 599, 100),
        _mk_node("起步价", 599, 200),
        _mk_node("普通时段", 300, 300), _mk_node("14.58元", 1000, 300),
        _mk_node("预约用车", 599, 400),
        _mk_node("起步价", 599, 500),          # 预约用车之后 → 不采
        _mk_node("普通时段", 300, 600), _mk_node("99元", 1000, 600),
    ]
    fd = parse_fare_detail_nodes(nodes)
    assert fd.stopped_at_yuyue is True
    assert fd.yuyue_y == 400
    assert len(fd.sections) == 1 and fd.sections[0].title == "起步价"
    assert len(fd.sections[0].rows) == 1


# ---------------------------------------------------------------------------
# 2) 跨滚动合并去重
# ---------------------------------------------------------------------------

def test_merge_dedup():
    a = parse_fare_detail_nodes(_fixture(_WEEKDAY))
    b = parse_fare_detail_nodes(_fixture(_WEEKEND))
    target = FareDetail(supplier="火箭出行", tab="工作日")
    added1 = merge_fare_detail(target, a)
    added2 = merge_fare_detail(target, b)
    assert added1 > 0 and added2 > 0
    # 起步价按 period 去重：无重复
    qj = next(s for s in target.sections if s.title == "起步价")
    periods = [r.period for r in qj.rows]
    assert len(periods) == len(set(periods)), "起步价时段应去重"
    assert "普通时段(3.0公里,11分钟)" in periods


# ---------------------------------------------------------------------------
# 3) JSON 写入
# ---------------------------------------------------------------------------

def test_write_json_structure():
    fd = parse_fare_detail_nodes(_fixture(_WEEKDAY), supplier="火箭出行", tab="工作日")
    with tempfile.TemporaryDirectory() as tmp:
        out = write_fare_detail_json(fd, Path(tmp) / "result")
        assert out == Path(tmp) / "result" / "工作日" / "火箭出行" / "fare_detail.json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["supplier"] == "火箭出行"
        assert data["tab"] == "工作日"
        assert data["mode"] == "实时用车"
        assert "起步价" in data["sections"]
        assert data["sections"]["起步价"][0] == {"period": "普通时段(3.0公里,11分钟)", "price": "14.58元"}


# ---------------------------------------------------------------------------
# 4) handler 端到端
# ---------------------------------------------------------------------------

class FareFakeEngine:
    def __init__(self):
        self.state = {"supplier": "火箭出行", "tab": "工作日"}
        self.adb = MagicMock()
        self._logs: list[str] = []
        self.screen_size = (1200, 2670)

    def _log(self, msg: str) -> None:
        self._logs.append(msg)

    def _wait(self, seconds: float, tag: str = "") -> None:
        pass

    @property
    def _screen_size(self):
        return self.screen_size


def _tab_nodes():
    """含「工作日/休息日」tab 的节点（回滚到顶部的判定信号）。"""
    return [_mk_node("工作日", 141, 680), _mk_node("休息日", 422, 680)]


def test_handler_fare_detail_ui():
    from collector.platform.gaode.platform import handle_fare_detail_ui
    with tempfile.TemporaryDirectory() as tmp:
        engine = FareFakeEngine()
        engine.output_dir = str(Path(tmp) / "out")
        nodes = _fixture(_WEEKDAY)
        # 3 次提取 dump：有数据 → 无新增 → 无新增（连续2轮无新增停止）
        # + 1 次确认 dump：见 tab → 确认回顶
        engine.adb.dump_ui_tree.side_effect = ["<h/>"] * 4
        with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
                   side_effect=[nodes, [], [], _tab_nodes()]):
            handle_fare_detail_ui(engine, {"max_rounds": 12, "scroll_wait": 0})
        # 下滑 2 次 + 上滑 2 次（down=2 → (2+1)//2+1=2，先按次数回滚）+ 确认 0 次补滑
        assert engine.adb.slide.call_count == 4, engine.adb.slide.call_count
        out = Path(tmp) / "result" / "工作日" / "火箭出行" / "fare_detail.json"
        assert out.exists(), engine._logs
        data = json.loads(out.read_text(encoding="utf-8"))
        assert set(data["sections"]) == {"起步价", "里程费"}
        assert len(data["sections"]["起步价"]) == 8


def test_handler_yuyue_needs_bottom_margin():
    """预约用车刚露头（距底部<20%）→ 继续下滑；进入安全区 → 停止。"""
    from collector.platform.gaode.platform import handle_fare_detail_ui
    bottom = [
        _mk_node("实时用车", 599, 100),
        _mk_node("起步价", 599, 200),
        _mk_node("普通时段", 300, 300), _mk_node("14.58元", 1000, 300),
        _mk_node("预约用车", 599, 2536),              # 0.95H → 底部余量(0.1)内，继续滑
    ]
    safe = [
        _mk_node("实时用车", 599, 100),
        _mk_node("起步价", 599, 200),
        _mk_node("普通时段", 300, 300), _mk_node("14.58元", 1000, 300),
        _mk_node("里程费", 599, 400),
        _mk_node("普通时段", 300, 500), _mk_node("1.30元/公里", 1000, 500),
        _mk_node("预约用车", 599, 1300),              # 0.49H → 安全区，停
    ]
    with tempfile.TemporaryDirectory() as tmp:
        engine = FareFakeEngine()
        engine.output_dir = str(Path(tmp) / "out")
        engine.adb.dump_ui_tree.side_effect = ["<h/>"] * 3
        with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
                   side_effect=[bottom, safe, _tab_nodes()]):
            handle_fare_detail_ui(engine, {"max_rounds": 12, "scroll_wait": 0})
        # 下滑 1 次（第1轮预约用车在底部→滑；第2轮安全→停）+ 按次数上滑 2 次 = 3
        assert engine.adb.slide.call_count == 3, engine.adb.slide.call_count
        out = Path(tmp) / "result" / "工作日" / "火箭出行" / "fare_detail.json"
        data = json.loads(out.read_text(encoding="utf-8"))
        assert set(data["sections"]) == {"起步价", "里程费"}
        assert data["stopped_at_yuyue"] is True


def test_handler_up_scroll_adaptive():
    """回滚顶部：第一次 dump 无 tab → 上滑一次；再 dump 见 tab → 停。"""
    from collector.platform.gaode.platform import handle_fare_detail_ui
    nodes = _fixture(_WEEKDAY)
    with tempfile.TemporaryDirectory() as tmp:
        engine = FareFakeEngine()
        engine.output_dir = str(Path(tmp) / "out")
        # 提取 3 次 + 确认回滚 2 次（第1次无tab→补滑，第2次见tab→停）
        engine.adb.dump_ui_tree.side_effect = ["<h/>"] * 5
        with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
                   side_effect=[nodes, [], [], [], _tab_nodes()]):
            handle_fare_detail_ui(engine, {"max_rounds": 12, "scroll_wait": 0})
        # 下滑 2 次 + 按次数上滑 2 次 + 确认补滑 1 次 = 5
        assert engine.adb.slide.call_count == 5, engine.adb.slide.call_count


def test_handler_fare_detail_ui_dump_fail_writes_empty():
    from collector.platform.gaode.platform import handle_fare_detail_ui
    with tempfile.TemporaryDirectory() as tmp:
        engine = FareFakeEngine()
        engine.output_dir = str(Path(tmp) / "out")
        engine.adb.dump_ui_tree.return_value = None      # dump 全失败
        handle_fare_detail_ui(engine, {"max_rounds": 3})
        out = Path(tmp) / "result" / "工作日" / "火箭出行" / "fare_detail.json"
        assert out.exists()
        assert json.loads(out.read_text(encoding="utf-8"))["sections"] == {}




def test_handler_empty_first_dump_retries():
    """首轮页面未渲染（无文字节点）→ 等待重试，不滑动；第二轮才有数据。"""
    from collector.platform.gaode.platform import handle_fare_detail_ui
    nodes = _fixture(_WEEKDAY)
    with tempfile.TemporaryDirectory() as tmp:
        engine = FareFakeEngine()
        engine.output_dir = str(Path(tmp) / "out")
        # 空 dump → 数据 → 无新增 → 无新增 → 确认回顶(tab)
        engine.adb.dump_ui_tree.side_effect = ["<h/>"] * 5
        with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
                   side_effect=[[], nodes, [], [], _tab_nodes()]):
            handle_fare_detail_ui(engine, {"max_rounds": 12, "scroll_wait": 0, "initial_wait": 0})
        # 空 dump 不滑动；数据轮滑 1 次；无新增轮滑 1 次；streak2 停 → 下滑 2 次 + 上滑 2 次
        assert engine.adb.slide.call_count == 4, engine.adb.slide.call_count
        out = Path(tmp) / "result" / "工作日" / "火箭出行" / "fare_detail.json"
        data = json.loads(out.read_text(encoding="utf-8"))
        assert set(data["sections"]) == {"起步价", "里程费"}

def test_parse_cross_city_nested():
    """跨城费：跨城费 → 城市 → (里程段, 价格) 三级结构。"""
    nodes = [
        _mk_node("实时用车", 599, 100),
        _mk_node("跨城费", 599, 200),
        _mk_node("北京市 至 天津市", 233, 300),
        _mk_node("40.0 - 60.0km", 209, 400), _mk_node("10.00元", 1067, 400),
        _mk_node("60.0 - 85.0km", 209, 500), _mk_node("20.00元", 1067, 500),
        _mk_node("北京市 至 保定市", 233, 600),
        _mk_node("25.0 - 35.0km", 207, 700), _mk_node("5.00元", 1067, 700),
    ]
    fd = parse_fare_detail_nodes(nodes)
    d = fare_detail_to_dict(fd)
    cc = d["sections"]["跨城费"]
    assert cc == {
        "北京市 至 天津市": [
            {"period": "40.0 - 60.0km", "price": "10.00元"},
            {"period": "60.0 - 85.0km", "price": "20.00元"},
        ],
        "北京市 至 保定市": [{"period": "25.0 - 35.0km", "price": "5.00元"}],
    }, cc


# ---------------------------------------------------------------------------
# 脚本式 runner
# ---------------------------------------------------------------------------

def main() -> int:
    import inspect
    funcs = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and inspect.isfunction(fn)]
    all_pass = True
    print(f"运行 {len(funcs)} 个用例：")
    for name, fn in funcs:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as e:
            import traceback
            print(f"  [FAIL] {name}: {e}")
            traceback.print_exc()
            all_pass = False
    print("=" * 50)
    print(f"  {'✓ 全部通过' if all_pass else '✗ 存在失败'}")
    print("=" * 50)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
