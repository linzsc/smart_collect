"""经济型供应商列表识别（S2）—— YAML extract_list / loop_until 的平台 handler
============================================================================

S2「识别经济型供应商列表」的平台 handler，供 YAML 计价采集子流程
（subflows/pricing_collect_gaode.yaml）的 `extract_list` / `loop_until` 使用：

  - handle_s2_list_suppliers : extract_list handler，写入 engine.state
      suppliers          当前屏「经济型」栏内、未处理过的运力商（已按 _SKIP_KEYWORDS 过滤）
      round_had_suppliers 本轮是否有新运力商
      economy_ended       经济型栏是否已结束（出现特快车/出租车/优享型等）
  - handle_pricing_loop_done : loop_until 终止条件 handler（返回 bool）
      经济型栏已结束 或 本轮无新运力商 → 停止外层循环
"""

from __future__ import annotations

from collector.platform.gaode.supplier_parse import parse_suppliers_response

# S2 提示词（CAP-09）
_S2_PROMPT = (
    "高德打车页面用灰色分割线划分多个栏（如「经济型」「特快车/特惠快车」「出租车」「优享型」等）。"
    "只列出「经济型」栏内（两道灰线之间）的运力商行，每行含车型名、预估价格和 ? 问号。"
    "不要列出其他栏（特快车/特惠快车、快车、拼车、出租车、优享型）的行。"
    '以 JSON 返回: {"suppliers": ["曹操出行", ...], "economy_ended": false}'
    "economy_ended: 截图中出现「特快车/特惠快车」「出租车」「优享型」等非经济型栏"
    "（即经济型栏已结束）时为 true，否则 false。"
)


def handle_s2_list_suppliers(engine, step: dict) -> None:
    """extract_list handler：识别当前屏「经济型」栏运力商并写入 engine.state。"""
    shot = engine._screenshot(step.get("id", "s2_suppliers"))
    engine.stats["vlm_calls"] += 1
    resp = engine.ctx.vision.query_text(shot, _S2_PROMPT)
    raw = resp.raw_response.strip()
    engine._log(f"  [S2] VLM: {raw[:300]}")

    suppliers, economy_ended = parse_suppliers_response(raw)

    # 只保留未处理过的运力商（跨轮次去重，等价 FSM 的 attempted）
    processed = engine.state.setdefault("_processed", set())
    new_suppliers = [s for s in suppliers if s not in processed]
    processed.update(new_suppliers)

    engine.state["suppliers"] = new_suppliers
    engine.state["round_had_suppliers"] = bool(new_suppliers)
    engine.state["economy_ended"] = economy_ended
    if economy_ended:
        engine._log(f"  [S2] 经济型栏已结束，本轮新运力商 {len(new_suppliers)} 个")
    else:
        engine._log(f"  [S2] 本轮新运力商 {len(new_suppliers)} 个")


def handle_pricing_loop_done(engine, step: dict) -> bool:
    """loop_until 终止条件：经济型栏已结束 或 本轮无新运力商。"""
    st = engine.state
    done = bool(st.get("economy_ended") or not st.get("round_had_suppliers"))
    engine._log(f"  [S2] 循环终止判定: economy_ended={st.get('economy_ended')} "
                f"round_had_suppliers={st.get('round_had_suppliers')} -> {done}")
    return done
