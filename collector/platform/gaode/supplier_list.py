"""经济型供应商列表识别（S2）—— YAML extract_list / loop_until 的平台 handler
============================================================================

S2「识别经济型供应商列表」的平台 handler，供 YAML 计价采集子流程
（subflows/pricing_collect_gaode.yaml）的 `extract_list` / `loop_until` 使用：

  - handle_s2_list_suppliers : extract_list handler，写入 engine.state
      suppliers          当前屏「经济型」栏内、未采集过的运力商（已按 _SKIP_KEYWORDS 过滤）
      round_had_suppliers 本轮是否有未采集运力商
      economy_ended       经济型栏是否已结束（出现特快车/出租车/优享型等）
  - handle_mark_supplier_processed : 采集完成后标记当前运力商为已处理（防重复采集）
  - handle_pricing_loop_done : loop_until 终止条件 handler（返回 bool）
      当前屏无新的未采集运力商 → 停止外层循环
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


def _identify(engine, step: dict) -> tuple[list[str], bool]:
    """识别当前屏「经济型」栏供应商 → (suppliers, economy_ended)。"""
    shot = engine._screenshot(step.get("id", "s2_suppliers"))
    engine.stats["vlm_calls"] += 1
    resp = engine.ctx.vision.query_text(shot, _S2_PROMPT)
    raw = resp.raw_response.strip()
    engine._log(f"  [S2] VLM: {raw[:200]}")
    return parse_suppliers_response(raw)


def handle_s2_list_suppliers(engine, step: dict) -> None:
    """extract_list handler：识别当前屏经济型供应商并写入 engine.state。

    空列表（当前屏无未采集）时自动下滑（≤1/4 屏）并重新识别，累计不超过
    _MAX_SCROLLS 次：
      - 未出现特殊词：下滑找更多（最多 3 次）；
      - 已出现特殊词：再下滑 1 次确认下方没有更多经济型供应商后停止。
    保证终止判定（pricing_loop_done）看到的永远是「下滑后」的识别结果。
    只返回未采集过的运力商；不在此标记已处理（由 mark_supplier_processed
    采集完成后标记，避免漏采）。
    """
    processed = engine.state.setdefault("_processed", set())
    suppliers, economy_ended = _identify(engine, step)
    new_suppliers = [s for s in suppliers if s not in processed]
    if new_suppliers:
        engine.state["s2_confirmed_ended"] = False

    scroll_count = int(engine.state.get("scroll_count", 0))
    confirmed_ended = bool(engine.state.get("s2_confirmed_ended"))
    step_id = step.get("id", "s2_suppliers")
    while (not new_suppliers) and scroll_count < _MAX_SCROLLS             and not (economy_ended and confirmed_ended):
        if engine.debug_mode:
            engine._screenshot(f"{step_id}_scroll_before")
        sw, sh = engine._screen_size
        engine.adb.slide(sw // 2, int(0.5 * sh), sw // 2, int(0.25 * sh), 600)  # ≤1/4 屏
        engine._wait(1.0, "s2_scroll")
        if engine.debug_mode:
            engine._screenshot(f"{step_id}_scroll_after")
        scroll_count += 1
        engine.state["scroll_count"] = scroll_count
        if economy_ended:
            engine.state["s2_confirmed_ended"] = True
            confirmed_ended = True
        engine._log(f"  [S2] 空列表，下滑 {scroll_count}/{_MAX_SCROLLS} 次后重新识别")
        suppliers, economy_ended = _identify(engine, step)
        new_suppliers = [s for s in suppliers if s not in processed]
        if new_suppliers:
            engine.state["s2_confirmed_ended"] = False
            confirmed_ended = False

    engine.state["suppliers"] = new_suppliers
    engine.state["round_had_suppliers"] = bool(new_suppliers)
    engine.state["economy_ended"] = economy_ended
    if economy_ended:
        engine._log(f"  [S2] 经济型栏已结束，本轮新运力商 {len(new_suppliers)} 个")
    else:
        engine._log(f"  [S2] 本轮新运力商 {len(new_suppliers)} 个")


def handle_mark_supplier_processed(engine, step: dict) -> None:
    """采集完成后标记当前运力商为已处理（防重复采集）。

    无论采集是否成功都标记（子流程内部失败会跳过），避免同一运力商无限重试。
    """
    supplier = str(engine.state.get("supplier", "")).strip()
    if supplier:
        engine.state.setdefault("_processed", set()).add(supplier)
        engine._log(f"  [S2] 已采集并标记: {supplier}")


# 兜底：整个采集过程下滑（s4_next 找更多）不超过 3 次，防止异常时无限下滑
_MAX_SCROLLS = 3


def handle_pricing_loop_done(engine, step: dict) -> bool:
    """loop_until 终止条件：
        1. 已采集满 max_suppliers（默认 10）；或
        2. 出现「出租车/特快车/优享型」（经济型栏结束）且其上的经济型供应商全部采完
           （不满 10 个也算完成）；或
        3. 兜底：下滑累计达 _MAX_SCROLLS（3 次）且当前屏仍无未采集供应商。

    出现特殊词后不再下滑（s4_next 同时要求 economy_ended=false）。
    """
    st = engine.state
    target = int(engine.profile_cfg.get("collection", {}).get("max_suppliers", 10))
    collected = len(st.get("_processed", set()))
    if collected >= target:
        engine._log(f"  [S2] 循环终止: 已采集 {collected}/{target}")
        return True
    economy_done = bool(st.get("economy_ended")) and not bool(st.get("round_had_suppliers"))
    scroll_cap = int(st.get("scroll_count", 0)) >= _MAX_SCROLLS and not bool(st.get("round_had_suppliers"))
    done = economy_done or scroll_cap
    engine._log(f"  [S2] 循环终止判定: collected={collected}/{target} economy_done={economy_done} "
                f"scroll_cap={scroll_cap} -> {done}")
    return done
