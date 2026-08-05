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
from collector.platform.gaode.ui_tree_supplier import SupplierRow, is_safe_center

# S2 提示词（CAP-11，单屏行级判断，不依赖「栏」跨屏概念）
_S2_PROMPT = (
    "这是高德打车「选择车型」页面的单屏截图。完成两件事：\n"
    "\n"
    "【任务1：列出普通运力商】\n"
    "运力商行特征：左侧圆形品牌 logo + 名称 + 灰色小字标签（如「敢坐敢赔」「隐私保护」），"
    "右侧「预估xx元」+ 方形勾选框。\n"
    "例如：火箭出行、旗妙出行、星徽出行、飞嘀打车、首汽约车、阳光出行、AA出行"
    "（不限于这些，符合特征的都算）。\n"
    "\n"
    "以下三类一律排除：\n"
    "1. 出租车类：名称含「的士」或「出租」（如「北京的士」「北京新出租」）；"
    "或标签带「打表计价」「纸质发票」「官方出品」；或 logo 是黄色 TAXI 样式；\n"
    "2. 平台产品行：名称为「特价拼车」「极速拼车」「特惠快车」「快车」「特快车」「出租车」"
    "（常带 0/5、1/8 这类角标），是平台产品不是运力商；\n"
    "3. 界面元素：「经济型·14」「全选经济」「查看更多已选车型」悬浮按钮、底部按钮栏。\n"
    "\n"
    "【任务2：判断是否采完】\n"
    "本屏是否出现以下栏目标题之一：「特快车」「出租车」「优享型」「专车」「六座」「豪华」？\n"
    "栏目标题特征：大号加粗黑字、独立成行、左侧没有圆形 logo、右侧没有预估价"
    "（可能带「全选优享」字样）。\n"
    "出现任意一个 → economy_ended = true，且任务1只统计位于该标题【上方】的运力商行，"
    "标题及以下的内容全部忽略；\n"
    "未出现 → economy_ended = false。\n"
    "\n"
    '只输出 JSON：{"suppliers": ["名称1", "名称2"], "economy_ended": false}'
)




def _log_supplier_coords(engine, suppliers) -> None:
    """日志输出运力商及对应坐标（仿视觉模式：识别 + 过滤 + 带坐标）。"""
    if not suppliers:
        engine._log("  [S2] 本屏无经济型运力商")
        return
    for s in suppliers:
        engine._log(f"    · {s.name} @ ({s.center[0]},{s.center[1]}) 已选={s.selected}")


def _identify_from_ui_tree(engine, step: dict) -> tuple[list[str], bool] | None:
    """UI 树优先识别当前屏经济型运力商（零 LLM）。

    返回 (suppliers, economy_ended)；dump 失败或「经济型·N」标题缺失（header_missing）
    返回 None，由调用方回退 VLM。
    """
    try:
        from collector.platform.gaode.ui_tree_supplier import (
            extract_economy_suppliers,
            nodes_from_xml,
        )
        xml = engine.adb.dump_ui_tree()
        if not xml or not isinstance(xml, str):
            engine._log("  [S2] UI树 dump 不可用，回退 VLM")
            return None
        nodes = nodes_from_xml(xml)
        res = extract_economy_suppliers(nodes)
        if res.header_missing:
            engine._log("  [S2] UI树「经济型」标题缺失(header_missing)，回退 VLM")
            return None

        engine.stats["ui_tree_dumps"] = engine.stats.get("ui_tree_dumps", 0) + 1
        _log_supplier_coords(engine, res.suppliers)
        # 缓存本屏节点与行节点，供后续「?」/勾选框定位（场景 C）
        engine.state["_ui_tree_nodes"] = nodes
        engine.state["_supplier_rows"] = {s.name: s.node for s in res.suppliers}
        engine.state["_economy_total"] = res.total_count
        engine._log(
            f"  [S2] UI树识别: {len(res.suppliers)} 个 "
            f"(total={res.total_count}, ended={res.economy_ended})"
        )
        return res.suppliers, res.economy_ended
    except Exception as e:  # noqa: BLE001 - UI 树路径任何异常都回退 VLM，不中断流程
        engine._log(f"  [S2] UI树解析异常({e})，回退 VLM")
        return None


def _identify(engine, step: dict) -> tuple[list[str], bool]:
    """识别当前屏「经济型」栏供应商 → (suppliers, economy_ended)。"""
    shot = engine._screenshot(step.get("id", "s2_suppliers"))
    engine.stats["vlm_calls"] += 1
    resp = engine.ctx.vision.query_text(shot, _S2_PROMPT)
    raw = resp.raw_response.strip()
    engine._log(f"  [S2] VLM: {raw[:200]}")
    return parse_suppliers_response(raw)


def handle_s2_list_suppliers(engine, step: dict) -> None:
    """extract_list handler：识别当前屏经济型供应商并写入 engine.state（UI 树优先）。

    - UI 树优先（零 LLM），失败/标题缺失回退 VLM；
    - 过滤：的士/出租/平台产品（is_skipped_supplier）+ 已采集 + 20% 余量；
    - 下滑由 YAML 循环步骤 s4_scroll 负责（本轮无可采时触发）。
    写 state：suppliers(本轮可采) / round_had_suppliers / economy_ended / _edge_suppliers(边缘行)。
    """
    processed = engine.state.setdefault("_processed", set())
    ui_result = _identify_from_ui_tree(engine, step)
    if ui_result is not None:
        rows, economy_ended = ui_result
    else:
        names, economy_ended = _identify(engine, step)
        rows = [SupplierRow(name=n, selected=None, detail="", center=(-1, -1), node={})
                for n in names]

    screen_h = engine._screen_size[1] if engine._screen_size else 0
    new_names: list[str] = []
    edge_names: list[str] = []
    for r in rows:
        if r.name in processed:
            continue
        if r.center[0] >= 0 and not is_safe_center(r.center[1], screen_h):
            edge_names.append(r.name)      # 贴边行：本轮不采，滚动到中间再采
        else:
            new_names.append(r.name)

    engine.state["suppliers"] = new_names
    engine.state["round_had_suppliers"] = bool(new_names)
    engine.state["economy_ended"] = economy_ended
    engine.state["_edge_suppliers"] = edge_names

    engine._log(f"  [S2] 本轮可采(未采集+余量OK): {new_names or '(无)'}")
    if edge_names:
        engine._log(f"  [S2] 边缘行(待滚动后采): {edge_names}")
    engine._log(f"  [S2] 已采集 {len(processed)} 个: {sorted(processed)}")
    if economy_ended:
        engine._log("  [S2] 经济型已结束(出现特快车/出租车/优享型等标题)")


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
    """loop_until 终止条件（CAP-11）：
        1. 已采集满 max_suppliers（默认 10）；或
        2. 屏内出现栏目标题（特快车/出租车/优享型/专车/六座/豪华，economy_ended=true）
           且该标题上方的经济型运力商全部采完（不满 10 个也算完成）；或
        3. 兜底：下滑累计达 _MAX_SCROLLS（3 次）且当前屏仍无未采集供应商。
    """
    st = engine.state
    target = int(engine.profile_cfg.get("collection", {}).get("max_suppliers", 10))
    collected = len(st.get("_processed", set()))
    if collected >= target:
        engine._log(f"  [S2] 循环终止: 已采集 {collected}/{target}")
        return True
    no_left = (not bool(st.get("round_had_suppliers"))
               and not bool(st.get("_edge_suppliers")))
    economy_done = bool(st.get("economy_ended")) and no_left
    scroll_cap = int(st.get("scroll_count", 0)) >= _MAX_SCROLLS and no_left
    done = economy_done or scroll_cap
    engine._log(f"  [S2] 循环终止判定: collected={collected}/{target} economy_done={economy_done} "
                f"scroll_cap={scroll_cap} -> {done}")
    return done
