"""
冒泡页（打车页）UI 树解析器：替代 S2 VLM 识别
================================================================

纯确定性：输入 UI 树节点列表，输出结构化结果，零 LLM 零 OCR。

数据来源：uiautomator dump（tools/ui_tree_collect.py 采集，output/ui_tree_pages.jsonl）。
节点格式与 tools/ui_tree_collect.py 的 parse_nodes 一致：
  {text, content_desc, class, resource_id, clickable, checked,
   left, top, right, bottom, center:[x, y]}

能力：
  - extract_economy_suppliers : 经济型运力商识别（y 分段 + 名称过滤）
  - locate_row_elements       : 定位运力商行的「?」问号 / 勾选框 / 价格
  - parse_supplier_desc       : 解析行 content-desc（车型:XX 已选/未选 预估 XX 元）
  - nodes_from_xml            : uiautomator XML → 节点列表
"""
from __future__ import annotations

import html as _html
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from collector.platform.gaode.supplier_parse import is_skipped_supplier

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 平台产品/类别行（非经济型运力商，名称过滤用；"经济型"是分段锚点行）
# 左侧栏类别名（用于 rail 列聚类；过滤走 supplier_parse.is_skipped_supplier）
_RAIL_NAMES = {
    "推荐", "拼车", "经济", "舒适", "品质", "特快车", "出租",
    "优享", "优享型", "专车", "六座", "豪华",
}

# 列表区类别标题
_ECON_HEADER_RE = re.compile(r"^经济型·?(\d*)$")
_END_HEADER_RES = [
    re.compile(p) for p in (
        r"^特快车$", r"^出租车$", r"^出租$", r"^优享(型)?$",
        r"^专车$", r"^六座$", r"^豪华$",
    )
]

_ROW_DESC_RE = re.compile(r"^车型:(.+?)(?:\s+(已选|未选))?(?:\s+(.*))?$")

# 行内价格文本（同行、行中心右侧）：预估 / 数字 / 元
_PRICE_TEXT_RE = re.compile(r"预估|^\d[\d.]*$|元")

# 左侧栏 vs 列表区：x 聚类桶宽（px）
_RAIL_BUCKET = 50
_RAIL_TOL = 30

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SupplierRow:
    name: str
    selected: bool | None
    detail: str
    center: tuple[int, int]
    node: dict


@dataclass
class EconomyResult:
    suppliers: list[SupplierRow]
    economy_ended: bool
    total_count: int | None        # 经济型·N 的 N（App 报的经济型总数）
    header_missing: bool = False   # 当前屏看不到「经济型·N」标题


@dataclass
class RowElements:
    row: dict
    price: dict | None
    q_button: dict | None          # 「?」问号（可点击、行中心~价格之间）
    checkbox: dict | None          # 勾选框（可点击、价格右侧最右）


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _text(n: dict) -> str:
    return n.get("text") or ""


def _desc(n: dict) -> str:
    return n.get("content_desc") or ""


def _is_clickable(n: dict) -> bool:
    return bool(n.get("clickable"))


def _center(n: dict) -> list[int] | None:
    c = n.get("center")
    if c:
        return c
    if "left" in n and "right" in n:
        return [(n["left"] + n["right"]) // 2, (n["top"] + n["bottom"]) // 2]
    return None


# ---------------------------------------------------------------------------
# XML → 节点
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BASE64_RE = re.compile(r"(?:data:)?(?:image/[^;]+;)?(?:[\w/+-]+;)?base64,[A-Za-z0-9+/=]*")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_IGNORE_TEXT = {"", " ", "　", "None", "null"}


def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    t = _html.unescape(raw)
    t = _TAG_RE.sub(" ", t)
    t = _BASE64_RE.sub(" ", t)
    t = _MULTI_SPACE_RE.sub(" ", t)
    return t.strip()


def nodes_from_xml(xml_text: str) -> list[dict]:
    """uiautomator XML → 节点列表（清洗富文本/base64，带 bounds/center）。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _nodes_from_xml_regex(xml_text)

    nodes = []
    for el in root.iter("node"):
        text = _clean_text(el.get("text", ""))
        desc = _clean_text(el.get("content-desc", ""))
        b = _parse_bounds(el.get("bounds", ""))
        if b is None:
            continue
        clickable = el.get("clickable", "false") == "true"
        if text in _IGNORE_TEXT and desc in _IGNORE_TEXT and not clickable:
            continue
        nodes.append({
            "text": text, "content_desc": desc,
            "class": el.get("class", ""), "resource_id": el.get("resource-id", ""),
            "clickable": clickable, "checked": el.get("checked"),
            **b,
        })
    return nodes


def _nodes_from_xml_regex(xml_text: str) -> list[dict]:
    nodes = []
    for m in re.finditer(r"<node\b[^>]*>", xml_text):
        attrs = m.group(0)
        get = lambda k: (re.search(rf'{k}="([^"]*)"', attrs) or [None, ""])[1]
        text = _clean_text(get("text"))
        desc = _clean_text(get("content-desc"))
        b = _parse_bounds(get("bounds"))
        if b is None:
            continue
        clickable = get("clickable") == "true"
        if text in _IGNORE_TEXT and desc in _IGNORE_TEXT and not clickable:
            continue
        nodes.append({
            "text": text, "content_desc": desc,
            "class": get("class"), "resource_id": get("resource-id"),
            "clickable": clickable, "checked": get("checked"),
            **b,
        })
    return nodes


def _parse_bounds(bounds: str) -> dict | None:
    m = _BOUNDS_RE.search(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return {
        "left": x1, "top": y1, "right": x2, "bottom": y2,
        "center": [(x1 + x2) // 2, (y1 + y2) // 2],
    }


# ---------------------------------------------------------------------------
# 行 desc 解析
# ---------------------------------------------------------------------------

def parse_supplier_desc(desc: str) -> dict:
    """解析「车型:星徽出行 已选 预估 12 元」→ {name, selected, detail}。"""
    m = _ROW_DESC_RE.match(desc.strip())
    if not m:
        return {"name": None, "selected": None, "detail": desc}
    name = m.group(1).strip()
    sel = m.group(2)
    detail = (m.group(3) or "").strip()
    return {"name": name, "selected": (sel == "已选"), "detail": detail}


# ---------------------------------------------------------------------------
# 类别标题（左侧栏 vs 列表区）
# ---------------------------------------------------------------------------

def _category_text_nodes(nodes: list[dict]) -> list[dict]:
    out = []
    for n in nodes:
        t = _text(n)
        if not t:
            continue
        if t in _RAIL_NAMES or _ECON_HEADER_RE.match(t) or any(r.match(t) for r in _END_HEADER_RES):
            out.append(n)
    return out


def _rail_column(cat_nodes: list[dict]) -> int | None:
    """左侧栏 x 列：类别名文字 x 聚类的众数列（平局取最左）。"""
    xs = [_center(n)[0] for n in cat_nodes if _center(n)]
    if not xs:
        return None
    buckets = Counter(round(x / _RAIL_BUCKET) * _RAIL_BUCKET for x in xs)
    max_cnt = max(buckets.values())
    return sorted(b for b, c in buckets.items() if c == max_cnt)[0]


def find_list_headers(nodes: list[dict]) -> list[dict]:
    """列表区类别标题（排除左侧栏）：经济型·N / 特快车 / 出租 / 优享 / 专车 / 六座 / 豪华。"""
    cat_nodes = _category_text_nodes(nodes)
    rail_x = _rail_column(cat_nodes)
    headers = []
    for n in cat_nodes:
        c = _center(n)
        if c is None:
            continue
        if rail_x is not None and abs(c[0] - rail_x) <= _RAIL_TOL:
            continue  # 左侧栏
        headers.append(n)
    return headers


# ---------------------------------------------------------------------------
# 经济型运力商识别
# ---------------------------------------------------------------------------

def _supplier_row_nodes(nodes: list[dict]) -> list[dict]:
    return [n for n in nodes if _is_clickable(n) and _desc(n).startswith("车型:")]


def extract_economy_suppliers(nodes: list[dict]) -> EconomyResult:
    """识别当前屏经济型运力商。

    规则：
      1. 行 = clickable 且 desc 以「车型:」开头；
      2. 列表区「经济型·N」标题 y = 经济型分段起点；
      3. 列表区 特快车/出租/优享/专车/六座 标题 y = 经济型分段终点（economy_ended）；
      4. 行 y ∈ [起点, 终点) 且名称非平台产品 → 经济型运力商。
    """
    rows = _supplier_row_nodes(nodes)
    headers = find_list_headers(nodes)

    econ_header = next((h for h in headers if _ECON_HEADER_RE.match(_text(h))), None)
    if econ_header is None:
        ended = any(any(r.match(_text(h)) for r in _END_HEADER_RES) for h in headers)
        return EconomyResult(suppliers=[], economy_ended=ended,
                             total_count=None, header_missing=True)

    econ_y = _center(econ_header)[1]
    end_headers = [h for h in headers
                   if _center(h)[1] > econ_y and any(r.match(_text(h)) for r in _END_HEADER_RES)]
    end_y = min((_center(h)[1] for h in end_headers), default=None)

    m = _ECON_HEADER_RE.match(_text(econ_header))
    total = int(m.group(1)) if m and m.group(1) else None

    suppliers: list[SupplierRow] = []
    for n in rows:
        c = _center(n)
        if c is None:
            continue
        y = c[1]
        if y < econ_y:
            continue
        if end_y is not None and y >= end_y:
            continue
        info = parse_supplier_desc(_desc(n))
        if not info["name"] or is_skipped_supplier(info["name"]):
            continue
        suppliers.append(SupplierRow(
            name=info["name"], selected=info["selected"],
            detail=info["detail"], center=tuple(c), node=n,
        ))
    return EconomyResult(suppliers=suppliers, economy_ended=bool(end_headers),
                         total_count=total)


# ---------------------------------------------------------------------------
# 行内元素定位（? / 勾选框 / 价格）
# ---------------------------------------------------------------------------

def locate_row_elements(nodes: list[dict], row_node: dict) -> RowElements:
    """定位运力商行内的「?」问号 / 勾选框 / 价格（全动态，不写死坐标）。

    - 价格：行 y 带内、行中心右侧、像价格的文本（预估/数字/元）中最左一个；
    - 「?」：行 y 带内、x ∈ (行中心, 价格左) 之间、最靠价格的可点击节点；
    - 勾选框：行 y 带内、x > 价格左、最右的可点击节点。
    """
    ry1, ry2 = row_node.get("top"), row_node.get("bottom")
    rcx = _center(row_node)[0]
    band = [n for n in nodes
            if _center(n) and ry1 <= _center(n)[1] <= ry2]

    # 价格文本：同行、x > 行中心、像价格，取最左
    price = None
    for n in sorted(band, key=lambda n: _center(n)[0]):
        t = _text(n)
        c = _center(n)
        if c[0] > rcx and t and _PRICE_TEXT_RE.search(t):
            price = n
            break
    price_left_x = _center(price)[0] if price else None

    # 「?」：行中心 ~ 价格左 之间的可点击节点，最靠价格
    q_button = None
    if price_left_x is not None:
        cands = [n for n in band
                 if _is_clickable(n) and rcx < _center(n)[0] < price_left_x]
        if cands:
            q_button = max(cands, key=lambda n: _center(n)[0])

    # 勾选框：价格左右侧最右可点击节点
    checkbox = None
    right_cands = [n for n in band
                   if _is_clickable(n)
                   and (price_left_x is None or _center(n)[0] > price_left_x)]
    if right_cands:
        checkbox = max(right_cands, key=lambda n: _center(n)[0])

    return RowElements(row=row_node, price=price, q_button=q_button, checkbox=checkbox)



def is_safe_center(y: int, screen_h: int, margin: float = 0.2) -> bool:
    """运力商行中心 y 是否在安全带内（上下各留 margin 余量，避免点到贴边行）。

    y < 0（无坐标，如 VLM 兜底行）视为安全（不约束）。
    """
    if y < 0 or screen_h <= 0:
        return True
    return margin * screen_h <= y <= (1 - margin) * screen_h

# ---------------------------------------------------------------------------
# 全选经济 / 左侧栏 定位
# ---------------------------------------------------------------------------

def locate_select_all(nodes: list[dict], label: str = "全选经济") -> dict | None:
    """定位「全选经济」文字及其右侧勾选框（clickable 节点）。

    返回 {text_node, checkbox}；找不到返回 None。
    """
    text_node = next((n for n in nodes if _text(n) == label), None)
    if text_node is None:
        return None
    tc = _center(text_node)
    if tc is None:
        return None
    # 同行 y 带内、x > 文字、可点击；取紧挨文字右侧的候选（勾选框）
    cands = [n for n in nodes
             if _is_clickable(n) and _center(n)
             and abs(_center(n)[1] - tc[1]) <= 40
             and _center(n)[0] > tc[0] + 10]
    if not cands:
        return None
    checkbox = min(cands, key=lambda n: _center(n)[0])
    return {"text_node": text_node, "checkbox": checkbox}


def locate_rail_category(nodes: list[dict], name: str = "经济") -> dict | None:
    """定位左侧栏类别文字（如「经济」）。"""
    cat_nodes = _category_text_nodes(nodes)
    rail_x = _rail_column(cat_nodes)
    if rail_x is None:
        return None
    for n in nodes:
        c = _center(n)
        if c is None:
            continue
        if abs(c[0] - rail_x) <= _RAIL_TOL and _text(n) == name:
            return n
    return None


def locate_supplier_row(nodes: list[dict], name: str) -> dict | None:
    """按运力商名找行节点（clickable 且 desc 车型:name）。"""
    for n in nodes:
        if not _is_clickable(n):
            continue
        d = _desc(n)
        if d.startswith("车型:") and parse_supplier_desc(d)["name"] == name:
            return n
    return None


def locate_q_button(nodes: list[dict], row_node: dict) -> dict | None:
    """定位行内「?」问号（相对行几何）。"""
    return locate_row_elements(nodes, row_node).q_button
