"""经济型供应商响应解析（单一实现）
============================================================================

S2「识别经济型供应商列表」的响应解析唯一实现，供 YAML extract_list
handler（supplier_list.py）等调用方共用，消除重复的脆弱 JSON 解析。
"""

from __future__ import annotations

import json
import re

_SKIP_SUPPLIERS = {"出租车", "优享"}          # 精确匹配（兼容旧引用）
_SKIP_KEYWORDS = ("快车", "拼车", "的士", "出租", "优享")      # 关键词：快车/拼车/出租车/的士/优享类一律不采集（CAP-08/09）


def parse_suppliers_response(raw: str) -> tuple[list[str], bool]:
    """解析 S2 VLM 响应 → (suppliers, economy_ended)（CAP-09）。

    兼容两种格式：
      {"suppliers": [...], "economy_ended": true/false}   # 新格式
      ["快车", ...]                                       # 旧数组格式（economy_ended=False）
    按 _SKIP_KEYWORDS 过滤；解析失败返回 ([], False)。
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
                if n and not any(kw in n for kw in _SKIP_KEYWORDS) and n not in suppliers:
                    suppliers.append(n)
            return suppliers, economy_ended
        if isinstance(parsed, list):
            for n in parsed:
                n = str(n).strip()
                if n and not any(kw in n for kw in _SKIP_KEYWORDS) and n not in suppliers:
                    suppliers.append(n)
            return suppliers, False
    except (json.JSONDecodeError, ValueError):
        pass

    # 非 JSON：逐行兜底
    for line in cleaned.split("\n"):
        line = re.sub(r'^[\d\.\、\)）\-\s]+', '', line.strip())
        line = line.strip().strip('"').strip("'").strip(",")
        if line and len(line) <= 30 and not any(kw in line for kw in _SKIP_KEYWORDS):
            if len(line) >= 2 and line not in suppliers:
                suppliers.append(line)
    return suppliers, False
