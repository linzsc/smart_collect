"""冒泡页 UI 树解析器 — 离线测试
================================================================

数据：tests/fixtures/bubble_ui_tree.json（2026-08-05 真机采集，output/ui_tree_pages.jsonl 的 bubble 页）

用例：
  - parse_supplier_desc : 行 desc 解析
  - find_list_headers   : 列表区标题识别（排除左侧栏）
  - extract_economy_suppliers : 经济型运力商识别（y 分段 + 名称过滤 + 总数）
  - locate_row_elements : 「?」问号 / 勾选框 / 价格 定位

用法:
  .venv/bin/python -m pytest tests/test_ui_tree_supplier.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collector.platform.gaode.ui_tree_supplier import (
    extract_economy_suppliers,
    find_list_headers,
    locate_q_button,
    locate_rail_category,
    locate_row_elements,
    locate_select_all,
    locate_supplier_row,
    parse_supplier_desc,
)

_FIXTURE = _THIS_DIR / "fixtures" / "bubble_ui_tree.json"


def _bubble_nodes() -> list[dict]:
    with open(_FIXTURE, encoding="utf-8") as f:
        return json.load(f)["nodes"]


# ---------------------------------------------------------------------------
# 1) 行 desc 解析
# ---------------------------------------------------------------------------

def test_parse_desc_full():
    r = parse_supplier_desc("车型:星徽出行 已选 预估 12 元")
    assert r["name"] == "星徽出行"
    assert r["selected"] is True
    assert r["detail"] == "预估 12 元"


def test_parse_desc_unselected():
    r = parse_supplier_desc("车型:极速拼车 未选 拼成最高 12.5 元")
    assert r["name"] == "极速拼车"
    assert r["selected"] is False
    assert r["detail"] == "拼成最高 12.5 元"


def test_parse_desc_no_price():
    r = parse_supplier_desc("车型:经济型 已选")
    assert r["name"] == "经济型"
    assert r["selected"] is True
    assert r["detail"] == ""


def test_parse_desc_invalid():
    r = parse_supplier_desc("随便一段文字")
    assert r["name"] is None


# ---------------------------------------------------------------------------
# 2) 列表区标题（排除左侧栏）
# ---------------------------------------------------------------------------

def test_find_list_headers_excludes_rail():
    nodes = _bubble_nodes()
    headers = find_list_headers(nodes)
    texts = [n["text"] for n in headers]
    # 左侧栏（x≈91）的 特快车/出租/优享/专车/六座 不应进列表区标题
    assert "经济型·14" in texts
    assert "特快车" not in texts
    assert "出租" not in texts


# ---------------------------------------------------------------------------
# 3) 经济型运力商识别
# ---------------------------------------------------------------------------

def test_extract_economy_suppliers():
    res = extract_economy_suppliers(_bubble_nodes())
    names = [s.name for s in res.suppliers]
    # 本屏经济型运力商（y 在 经济型·14 标题下方）
    assert names == ["星徽出行", "火箭出行", "AA出行", "首汽约车", "飞嘀打车"], names
    assert res.total_count == 14          # 经济型·14
    assert res.economy_ended is False     # 本屏列表区未出现 特快车/出租 等标题
    assert res.header_missing is False
    # 拼车类（特惠快车/极速拼车）被 y 分段排除
    assert "特惠快车" not in names
    assert "极速拼车" not in names


def test_extract_economy_suppliers_selected_flags():
    res = extract_economy_suppliers(_bubble_nodes())
    by_name = {s.name: s for s in res.suppliers}
    assert by_name["星徽出行"].selected is True
    assert by_name["AA出行"].selected is True


def test_header_missing_returns_empty():
    nodes = [n for n in _bubble_nodes() if n["text"] != "经济型·14"]
    res = extract_economy_suppliers(nodes)
    assert res.suppliers == []
    assert res.header_missing is True


# ---------------------------------------------------------------------------
# 4) 行内元素定位（? / 勾选框 / 价格）
# ---------------------------------------------------------------------------

def _find_row(nodes, name):
    return next(n for n in nodes
                if n.get("clickable") and name in (n.get("content_desc") or ""))


def test_locate_row_elements_star():
    nodes = _bubble_nodes()
    row = _find_row(nodes, "车型:星徽出行")
    r = locate_row_elements(nodes, row)
    # 「?」：行中心(~691) 与「预估」(x=941) 之间、最靠价格的可点击节点
    assert r.q_button is not None
    assert r.q_button["center"][0] == 864
    assert abs(r.q_button["center"][1] - 2051) <= 30
    # 勾选框：价格右侧最右可点击节点（x≈1141）
    assert r.checkbox is not None
    assert r.checkbox["center"][0] >= 1100
    assert abs(r.checkbox["center"][1] - 2051) <= 30
    # 价格文本：预估 @ x=941
    assert r.price is not None
    assert r.price["text"] == "预估"
    assert r.price["center"][0] == 941


def test_locate_row_elements_aa():
    nodes = _bubble_nodes()
    row = _find_row(nodes, "车型:AA出行")
    r = locate_row_elements(nodes, row)
    assert r.q_button is not None
    assert r.checkbox is not None
    assert r.price is not None
    # 问号在 行中心 与 价格 之间
    assert r.row["center"][0] < r.q_button["center"][0] < r.price["center"][0]
    # 勾选框在价格右侧
    assert r.checkbox["center"][0] > r.price["center"][0]

# ---------------------------------------------------------------------------
# 5) handle_s2_list_suppliers 集成（UI 树优先 + VLM 兜底）
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch  # noqa: E402


class FakeEngine:
    """最小 engine 桩：只覆盖 handle_s2_list_suppliers 用到的能力。"""

    def __init__(self):
        self.state: dict = {"_processed": set()}
        self.stats: dict = {"vlm_calls": 0, "ui_tree_dumps": 0}
        self.adb = MagicMock()
        self.ctx = MagicMock()
        self.timing: dict = {}
        self.profile_cfg: dict = {}
        self.verbose: bool = False
        self.grounder = MagicMock()
        self._logs: list[str] = []
        self.screen_h: int = 2670            # 真机分辨率高度

    def _log(self, msg: str) -> None:
        self._logs.append(msg)

    def _screenshot(self, name: str) -> str:
        return "mock_screenshot.png"

    def _wait(self, seconds: float, tag: str = "") -> None:
        pass

    @property
    def _screen_size(self):
        return (1200, self.screen_h)


def _handle_s2():
    from collector.platform.gaode.supplier_list import handle_s2_list_suppliers
    return handle_s2_list_suppliers


def test_handle_s2_ui_tree_path():
    handle = _handle_s2()
    engine = FakeEngine()                       # screen_h=2670 → 安全带 [534, 2136]
    engine.adb.dump_ui_tree.return_value = "<hierarchy/>"
    nodes = _bubble_nodes()
    with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml", return_value=nodes):
        handle(engine, {"id": "s2_list"})
    # 余量过滤：星徽出行(y=2051)可采；其余在底部 20%（y>2136）→ 边缘行
    assert engine.state["suppliers"] == ["星徽出行"]
    assert engine.state["_edge_suppliers"] == ["火箭出行", "AA出行", "首汽约车", "飞嘀打车"]
    assert engine.state["round_had_suppliers"] is True
    assert engine.state["economy_ended"] is False
    assert engine.state["_economy_total"] == 14
    assert engine.stats["vlm_calls"] == 0          # 零 LLM
    assert engine.stats["ui_tree_dumps"] == 1
    engine.ctx.vision.query_text.assert_not_called()  # 未走 VLM


def test_handle_s2_ui_tree_no_margin_when_screen_large():
    handle = _handle_s2()
    engine = FakeEngine()
    engine.screen_h = 10000                       # 安全带 [2000,8000] → 全部可采
    engine.adb.dump_ui_tree.return_value = "<hierarchy/>"
    nodes = _bubble_nodes()
    with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml", return_value=nodes):
        handle(engine, {"id": "s2_list"})
    assert engine.state["suppliers"] == ["星徽出行", "火箭出行", "AA出行", "首汽约车", "飞嘀打车"]
    assert engine.state["_edge_suppliers"] == []


def test_handle_s2_vlm_fallback_on_dump_fail():
    handle = _handle_s2()
    engine = FakeEngine()
    engine.adb.dump_ui_tree.return_value = None      # dump 失败
    resp = MagicMock()
    resp.raw_response = '{"suppliers": ["测试车", "北京的士"], "economy_ended": false}'
    engine.ctx.vision.query_text.return_value = resp
    handle(engine, {"id": "s2_list"})
    assert engine.state["suppliers"] == ["测试车"]   # 的士 被 _SKIP_KEYWORDS 过滤
    assert engine.stats["vlm_calls"] == 1


def test_handle_s2_vlm_fallback_on_header_missing():
    handle = _handle_s2()
    engine = FakeEngine()
    engine.adb.dump_ui_tree.return_value = "<hierarchy/>"
    nodes_no_header = [n for n in _bubble_nodes() if n["text"] != "经济型·14"]
    resp = MagicMock()
    resp.raw_response = '{"suppliers": ["星徽出行"], "economy_ended": false}'
    engine.ctx.vision.query_text.return_value = resp
    with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
               return_value=nodes_no_header):
        handle(engine, {"id": "s2_list"})
    assert engine.stats["vlm_calls"] == 1           # 标题缺失 → 回退 VLM
    assert engine.state["suppliers"] == ["星徽出行"]



# ---------------------------------------------------------------------------
# 6) 全选经济 / 左侧栏 / 问号定位 + 出租的士过滤
# ---------------------------------------------------------------------------

def test_locate_select_all():
    nodes = _bubble_nodes()
    sel = locate_select_all(nodes, "全选经济")
    assert sel is not None
    assert sel["text_node"]["text"] == "全选经济"
    assert sel["checkbox"]["center"][0] == 1047   # 紧挨文字右侧 clickable
    assert sel["checkbox"]["center"][1] == 1914


def test_locate_rail_category():
    nodes = _bubble_nodes()
    node = locate_rail_category(nodes, "经济")
    assert node is not None
    assert node["text"] == "经济"
    assert node["center"][0] < 150                # 左侧栏
    assert node["center"][1] == 1837


def test_economy_excludes_taxi_keywords():
    import copy
    nodes = _bubble_nodes()
    base = extract_economy_suppliers(nodes).suppliers
    names = [s.name for s in base]
    assert "北京的士" not in names
    # 模拟加一个「北京的士」行 → 应被过滤
    row = copy.deepcopy(base[0].node)
    row["content_desc"] = "车型:北京的士 未选 预估 20 元"
    res2 = extract_economy_suppliers(nodes + [row])
    assert "北京的士" not in [s.name for s in res2.suppliers]


# ---------------------------------------------------------------------------
# 7) platform handler 集成：ui_tree_click / select_all（UI 树优先）
# ---------------------------------------------------------------------------

def _fake_engine_with_nodes(nodes):
    engine = FakeEngine()
    engine.state["_ui_tree_nodes"] = nodes
    return engine


def test_handle_ui_tree_click_rail():
    from collector.platform.gaode.platform import handle_ui_tree_click
    engine = _fake_engine_with_nodes(_bubble_nodes())
    ok = handle_ui_tree_click(engine, {"ui_target": "rail_economy", "wait_after": 0})
    assert ok is True
    engine.adb.click.assert_called_once_with(91, 1837)


def test_handle_ui_tree_click_q():
    from collector.platform.gaode.platform import handle_ui_tree_click
    engine = _fake_engine_with_nodes(_bubble_nodes())
    engine.state["supplier"] = "星徽出行"
    ok = handle_ui_tree_click(engine, {"ui_target": "q_button", "wait_after": 0})
    assert ok is True
    args, _ = engine.adb.click.call_args
    assert args[0] == 864                         # 问号 x
    assert 2020 <= args[1] <= 2060                # 问号 y


def test_handle_ui_tree_click_no_nodes_falls_back():
    from collector.platform.gaode.platform import handle_ui_tree_click
    engine = _fake_engine_with_nodes(None)
    engine.adb.dump_ui_tree.return_value = None
    ok = handle_ui_tree_click(engine, {"ui_target": "rail_economy"})
    assert ok is False


def test_handle_select_all_ui_tree_already_checked():
    from collector.platform.gaode.platform import handle_select_all
    engine = FakeEngine()
    engine.adb.dump_ui_tree.return_value = "<hierarchy/>"
    with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
               return_value=_bubble_nodes()):
        handle_select_all(engine, {"label": "全选经济"})
    assert engine.state["select_all_done"] is True
    engine.adb.click.assert_not_called()          # 全部已选 → 不点击


def test_handle_select_all_ui_tree_clicks_when_unchecked():
    import json as _json
    from collector.platform.gaode.platform import handle_select_all
    engine = FakeEngine()
    engine.adb.dump_ui_tree.side_effect = ["<h/>", "<h/>"]   # 点击前 + 点击后 两次 dump
    nodes_checked = _bubble_nodes()
    nodes_unchecked = _json.loads(_json.dumps(nodes_checked))  # 深拷贝
    for n in nodes_unchecked:
        if n.get("content_desc", "").startswith("车型:星徽出行"):
            n["content_desc"] = "车型:星徽出行 未选 预估 12 元"
            break
    with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
               side_effect=[nodes_unchecked, nodes_checked]):
        handle_select_all(engine, {"label": "全选经济"})
    assert engine.state["select_all_done"] is True
    engine.adb.click.assert_called()              # 未全选 → 点击勾选框



# ---------------------------------------------------------------------------
# 8) 20% 余量 + q 点问号「勾选保证」
# ---------------------------------------------------------------------------

def test_is_safe_center():
    from collector.platform.gaode.ui_tree_supplier import is_safe_center
    assert is_safe_center(500, 2670) is False      # 顶部 20% 内
    assert is_safe_center(2136, 2670) is True      # 下边界（0.8H）
    assert is_safe_center(2218, 2670) is False     # 底部 20% 内
    assert is_safe_center(1200, 2670) is True      # 中间
    assert is_safe_center(-1, 2670) is True        # 无坐标不约束
    assert is_safe_center(500, 0) is True          # 屏高未知不约束


def _set_row_selected(nodes, name, selected: bool):
    import json as _json
    nodes = _json.loads(_json.dumps(nodes))
    for n in nodes:
        if (n.get("content_desc") or "").startswith(f"车型:{name}"):
            n["content_desc"] = n["content_desc"].replace("已选", "未选") if not selected else \
                n["content_desc"].replace("未选", "已选")
            break
    return nodes


def test_handle_ui_tree_click_q_already_selected():
    from collector.platform.gaode.platform import handle_ui_tree_click
    engine = _fake_engine_with_nodes(_bubble_nodes())   # 星徽出行 已选
    engine.state["supplier"] = "星徽出行"
    ok = handle_ui_tree_click(engine, {"ui_target": "q_button", "wait_after": 0})
    assert ok is True
    engine.adb.click.assert_called_once()          # 已选 → 只点问号一次
    args, _ = engine.adb.click.call_args
    assert args[0] == 864


def test_handle_ui_tree_click_q_ensure_selected():
    from collector.platform.gaode.platform import handle_ui_tree_click
    engine = FakeEngine()
    nodes_unchecked = _set_row_selected(_bubble_nodes(), "星徽出行", False)
    nodes_checked = _bubble_nodes()
    engine.state["_ui_tree_nodes"] = nodes_unchecked
    engine.state["supplier"] = "星徽出行"
    engine.adb.dump_ui_tree.side_effect = ["<h/>"]     # 点勾选框后重 dump
    with patch("collector.platform.gaode.ui_tree_supplier.nodes_from_xml",
               side_effect=[nodes_checked]):
        ok = handle_ui_tree_click(engine, {"ui_target": "q_button", "wait_after": 0})
    assert ok is True
    assert engine.adb.click.call_count == 2          # 先勾选框、再问号
    first, _ = engine.adb.click.call_args_list[0]
    second, _ = engine.adb.click.call_args_list[1]
    assert first[0] >= 1100                          # 勾选框（右侧）
    assert second[0] == 864                          # 问号


# ---------------------------------------------------------------------------
# 脚本式 runner（与 tests/ 现有约定一致；pytest 亦可直接运行）
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
