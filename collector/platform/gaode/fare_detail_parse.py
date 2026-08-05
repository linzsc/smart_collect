"""
详细计价页 UI 树解析（替代 OCR 识别，OCR 仅兜底）
================================================================

从 uiautomator dump 的节点中提取详细计价规则，产出结构化字段。

规则（全部确定性、零 LLM 零 OCR）：
  - 按 y 分组建行（容差 20px）；某 y 组节点过多（>5）＝ 坐标坍塌（WebView 未稳定）
    → 跳过该组（不采，等滚动后重 dump 获得正确坐标）；
  - 段标题：居中（x≈599）且匹配已知费用类别 → 开启新段；
    「实时用车」= 计价模式；「预约用车」= 停止（只取其上，没有则全部）；
  - 数据行：同行 2 节点 且 左 x<500 / 右 x>800 → (时段, 价格) 配对；
  - 跨滚动合并：按 (段, 时段) 去重，供"每滚一屏 dump 一次"拼接。

输出 JSON（每运力商每标签一个）：
  {supplier, tab, mode, stopped_at_yuyue, sections: {段: [{period, price}]}}
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 已知费用类别段标题（居中 x≈599）
_SECTION_TITLES = {
    "起步价", "里程费", "时长费", "远途费", "跨城费",
    "等待费", "预约等待费", "临时加价",
}
_MODE_TITLES = {"实时用车", "预约用车"}

# 坐标坍塌判定：同一 y 组节点数超过该值即视为坐标不可靠（WebView 未稳定）
_COLLAPSE_NODE_LIMIT = 5
# 有效 label|value 配对的 x 区间
_LEFT_X_MAX = 500
_RIGHT_X_MIN = 800
# 段标题 x 区间（居中）
_HEADER_X_MIN = 450
_HEADER_X_MAX = 750

# 价格正则（用于辅助识别 value，可选）
_PRICE_RE = __import__("re").compile(r"\d+(\.\d+)?")


@dataclass
class FareRow:
    period: str
    price: str
    city: str = ""      # 跨城费的城市分组（如「北京市 至 天津市」）


@dataclass
class FareSection:
    title: str
    rows: list[FareRow] = field(default_factory=list)


@dataclass
class FareDetail:
    supplier: str = ""
    tab: str = ""
    mode: str = "实时用车"
    sections: list[FareSection] = field(default_factory=list)
    stopped_at_yuyue: bool = False
    yuyue_y: int | None = None   # 「预约用车」标题 y（用于"距底部余量"停止判定）
    collapsed_skipped: int = 0


def parse_fare_detail_nodes(nodes: list[dict], supplier: str = "", tab: str = "") -> FareDetail:
    """解析单次 dump 的节点 → FareDetail（坍塌组跳过，预约用车截断）。"""
    text_nodes = [n for n in nodes if n.get("text")]

    # 按 y 分组（容差 20px）
    groups: dict[int, list[dict]] = defaultdict(list)
    for n in text_nodes:
        groups[round(n["center"][1] / 20)].append(n)

    fd = FareDetail(supplier=supplier, tab=tab)
    current: FareSection | None = None
    current_city = ""

    for key in sorted(groups):
        ns = sorted(groups[key], key=lambda n: n["center"][0])
        if len(ns) > _COLLAPSE_NODE_LIMIT:
            fd.collapsed_skipped += len(ns)   # 坐标坍塌，跳过
            continue

        # ── 段标题（居中单节点）──
        if len(ns) == 1:
            x = ns[0]["center"][0]
            t = ns[0]["text"]
            if _HEADER_X_MIN <= x <= _HEADER_X_MAX and t in _SECTION_TITLES:
                current = FareSection(title=t)
                current_city = ""
                fd.sections.append(current)
                continue
            if _HEADER_X_MIN <= x <= _HEADER_X_MAX and t == "实时用车":
                fd.mode = "实时用车"
                continue
            if _HEADER_X_MIN <= x <= _HEADER_X_MAX and t == "预约用车":
                fd.stopped_at_yuyue = True    # 只取预约用车之上
                fd.yuyue_y = ns[0]["center"][1]
                break

        # ── 跨城费城市行：单节点、含「至」（如「北京市 至 天津市」）──
        if (current is not None and current.title == "跨城费"
                and len(ns) == 1 and "至" in ns[0]["text"]):
            current_city = ns[0]["text"].strip()
            continue

        # ── 数据行：左标签 | 右价格（跨城费行带 city）──
        if current is not None and len(ns) == 2:
            left, right = ns[0], ns[1]
            if left["center"][0] < _LEFT_X_MAX and right["center"][0] > _RIGHT_X_MIN:
                period = left["text"].strip()
                price = right["text"].strip()
                if period and price:
                    current.rows.append(FareRow(
                        period=period, price=price,
                        city=current_city if current.title == "跨城费" else "",
                    ))

    return fd


def merge_fare_detail(target: FareDetail, new: FareDetail) -> int:
    """把 new 并入 target（按 段+时段 去重），返回新增行数。"""
    added = 0
    for sec in new.sections:
        t = next((s for s in target.sections if s.title == sec.title), None)
        if t is None:
            target.sections.append(sec)
            added += len(sec.rows)
            continue
        for row in sec.rows:
            if not any(r.period == row.period and r.city == row.city for r in t.rows):
                t.rows.append(row)
                added += 1
    if new.stopped_at_yuyue:
        target.stopped_at_yuyue = True
        if new.yuyue_y is not None and (target.yuyue_y is None or new.yuyue_y < target.yuyue_y):
            target.yuyue_y = new.yuyue_y
    return added


def fare_detail_to_dict(fd: FareDetail) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    for s in fd.sections:
        if s.title == "跨城费":
            cities: dict[str, list[dict[str, str]]] = {}
            for r in s.rows:
                cities.setdefault(r.city, []).append({"period": r.period, "price": r.price})
            sections[s.title] = cities
        else:
            sections[s.title] = [{"period": r.period, "price": r.price} for r in s.rows]
    return {
        "supplier": fd.supplier,
        "tab": fd.tab,
        "mode": fd.mode,
        "stopped_at_yuyue": fd.stopped_at_yuyue,
        "sections": sections,
    }


def write_fare_detail_json(fd: FareDetail, result_dir: str | Path, logger=None) -> Path:
    """按 result/ 约定写入：result/{tab}/{supplier}/fare_detail.json。"""
    out = Path(result_dir) / fd.tab / fd.supplier / "fare_detail.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(fare_detail_to_dict(fd), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if logger:
        logger(f"  [计价UI] 已写入 {out}")
    return out
