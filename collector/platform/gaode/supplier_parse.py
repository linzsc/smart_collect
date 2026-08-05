"""经济型供应商响应解析（单一实现）
============================================================================

S2「识别经济型供应商列表」的响应解析唯一实现，供 YAML extract_list
handler（supplier_list.py）等调用方共用，消除重复的脆弱 JSON 解析。
"""

from __future__ import annotations

import json
import re

# 不需要采集（CAP-08/09/11）：
#   _SKIP_EXACT  精确匹配：栏目标题 + 平台产品行（特惠快车/快车/拼车/出租车等）
#   _SKIP_KEYWORDS 子串：出租车类运力商（北京的士/北京新出租）+ UI 文案
_SKIP_EXACT = {"拼车", "特价拼车", "极速拼车", "特惠快车", "快车", "特快车",
               "出租车", "经济型", "优享", "优享型", "专车", "六座", "豪华"}
_SKIP_KEYWORDS = ("的士", "出租", "打表", "发票", "查看更多", "全选")
_SKIP_SUPPLIERS = _SKIP_EXACT  # 兼容别名


def _is_skipped(name: str) -> bool:
    """是否应排除：精确命中平台产品/栏目标题，或名称含出租车/UI 等关键词。"""
    return name in _SKIP_EXACT or any(kw in name for kw in _SKIP_KEYWORDS)




def is_skipped_supplier(name: str) -> bool:
    """是否应排除：精确命中平台产品/栏目标题，或名称含出租车/UI 等关键词。

    供 VLM 解析与 UI 树解析共用（单一过滤源）。
    """
    return _is_skipped(name)

def parse_suppliers_response(raw: str) -> tuple[list[str], bool]:
    """解析 S2 VLM 响应 → (suppliers, economy_ended)（CAP-09/11）。

    兼容两种格式：
      {"suppliers": [...], "economy_ended": true/false}   # 新格式
      ["快车", ...]                                       # 旧数组格式（economy_ended=False）
    按 _is_skipped（_SKIP_EXACT 精确 + _SKIP_KEYWORDS 子串）过滤；解析失败返回 ([], False)。
    """
    suppliers: list[str] = []
    economy_ended = False
    cleaned = raw.strip()
    for m in ("```json", "```"):
        if cleaned.startswith(m):
            cleaned = cleaned[len(m):].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            economy_ended = bool(parsed.get("economy_ended", False))
            items = parsed.get("suppliers", [])
            if not isinstance(items, list):
                return [], economy_ended
            for n in items:
                n = str(n).strip()
                if n and not _is_skipped(n) and n not in suppliers:
                    suppliers.append(n)
            return suppliers, economy_ended
        if isinstance(parsed, list):
            for n in parsed:
                n = str(n).strip()
                if n and not _is_skipped(n) and n not in suppliers:
                    suppliers.append(n)
            return suppliers, False
    except (json.JSONDecodeError, ValueError):
        pass

    # 非 JSON：逐行兜底
    for line in cleaned.split("\n"):
        line = re.sub(r'^[\d\.\、\）\-\s]+', '', line.strip())
        line = line.strip().strip('"').strip("'").strip(",")
        if line and len(line) <= 30 and not _is_skipped(line):
            if len(line) >= 2 and line not in suppliers:
                suppliers.append(line)
    return suppliers, False
