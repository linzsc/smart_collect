"""高德平台接入描述
============================================================================

把高德特有的 CLI 参数、模板变量和平台专属步骤（select_all / s2_list_suppliers /
pricing_loop_done / pricing_result_organize）封装为 Platform，注册到
`collector/platform/registry.py`。通用引擎不再直接依赖本模块。
计价采集控制流已由 YAML 子流程（subflows/pricing_collect_gaode.yaml）表达。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from collector.domain.platform import Platform


# ---------------------------------------------------------------------------
# CLI 参数（平台特有）
# ---------------------------------------------------------------------------

def add_cli_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--address", required=True,
                        help="目的地，例如 '北京西站' / '西北旺万象汇'")
    parser.add_argument("--pickup",
                        help="上车点 (v2/v3 需要，例如 '北京西站')")
    parser.add_argument("--capture-mode", default="full", choices=["full", "test", "ui"],
                        help="计价采集模式: full=完整 detail_capture（工作日/休息日滚动，默认）; "
                             "test=轻量测试（仅点问号进入计价页，不做滚动）; "
                             "ui=UI树提取详细计价页（每运力商产出工作日/休息日 2 个 JSON）")


# ---------------------------------------------------------------------------
# 流程模板变量
# ---------------------------------------------------------------------------

def build_flow_vars(args: argparse.Namespace, flow_name: str) -> dict[str, str]:
    vars_: dict[str, str] = {"Address": args.address}
    if flow_name in ("v2", "v3"):   # 需要上车点
        vars_["Pickup"] = args.pickup or "我的位置"
    # 计价采集子流程：full=detail_capture / test=detail_entry_test / ui=detail_capture_ui（UI树提取）
    cm = getattr(args, "capture_mode", "full")
    if cm == "test":
        vars_["DetailSubflow"] = "detail_entry_test_gaode.yaml"
    elif cm == "ui":
        vars_["DetailSubflow"] = "detail_capture_ui_gaode.yaml"
    else:
        vars_["DetailSubflow"] = "detail_capture_gaode.yaml"
    return vars_


def _organize_result_screenshots(engine) -> None:
    """计价采集结束后，把必要截图聚合到 result/（工作日/休息日 × 运力商）。

    必要截图：打车页（全选经济后）+ 每个运力商每个标签前 N 张滚动截图。
    仅复制，不移动/删除 output/ 原图；失败只告警，不影响主流程。
    """
    try:
        from collector.platform.gaode.screenshot_organizer import collect_necessary_screenshots

        scroll_count = int(
            engine.profile_cfg.get("collection", {}).get("result_scroll_count", 4)
        )
        result_dir = str(Path(engine.output_dir).resolve().parent / "result")
        summary = collect_necessary_screenshots(
            output_dir=engine.output_dir,
            result_dir=result_dir,
            scroll_count=scroll_count,
            logger=engine._log,
        )
        groups = summary.get("groups", {})
        engine._log(f"── 结果整理: {summary.get('copied', 0)} 张 → {result_dir}")
        for tab, supps in groups.items():
            for supp, files in supps.items():
                label = "滚动截图" if tab in ("工作日", "休息日") else "详细计价页"
                engine._log(f"    {tab}/{supp}: {label} {len(files)} 张")
    except Exception as e:
        engine._log(f"  ⚠ 结果整理失败: {e}")


# ---------------------------------------------------------------------------
# 平台特有步骤：select_all（目标锚定的幂等全选，SEL-01）
# ---------------------------------------------------------------------------

def _ensure_select_all_ui_tree(engine, label: str) -> bool:
    """UI 树幂等全选：定位「全选经济」勾选框，用经济型行 desc 推断勾选状态。

    全部已选 → 跳过点击；未全选 → 点击勾选框 → 重验。失败返回 False（回退 VLM）。
    """
    from collector.platform.gaode.ui_tree_supplier import (
        extract_economy_suppliers,
        locate_select_all,
        nodes_from_xml,
    )

    try:
        xml = engine.adb.dump_ui_tree()
        if not xml or not isinstance(xml, str):
            return False
        nodes = nodes_from_xml(xml)
        sel = locate_select_all(nodes, label)
        if sel is None:
            engine._log("  [全选] UI树未找到勾选框，回退 VLM")
            return False
        res = extract_economy_suppliers(nodes)
        rows = res.suppliers
        if not rows:
            engine._log("  [全选] UI树无经济型行可推断，回退 VLM")
            return False
        engine.state["_ui_tree_nodes"] = nodes

        if all(r.selected for r in rows):
            engine._log(f"  [全选] UI树: 已勾选（{len(rows)} 行全部已选），跳过点击")
            return True

        cx, cy = sel["checkbox"]["center"]
        engine._log(f"  [全选] UI树: 未全选，点击勾选框 ({cx},{cy})")
        engine.adb.click(cx, cy)
        engine._wait(engine.timing.get("after_tap_wait", 2.0), "after_select_all")
        xml2 = engine.adb.dump_ui_tree()
        if not xml2 or not isinstance(xml2, str):
            return False
        res2 = extract_economy_suppliers(nodes_from_xml(xml2))
        if res2.suppliers and all(r.selected for r in res2.suppliers):
            engine._log(f"  [全选] UI树: 点击后 {len(res2.suppliers)} 行全部已选 ✓")
            return True
        engine._log("  [全选] UI树点击后仍未全选，回退 VLM")
        return False
    except Exception as e:  # noqa: BLE001
        engine._log(f"  [全选] UI树异常({e})，回退 VLM")
        return False




def handle_fare_detail_ui(engine, step: dict) -> None:
    """fare_detail_ui：UI 树提取详细计价页（dump → 解析 → 下滑 → 拼接 → 写 JSON）。

    - 只采「预约用车」之上的内容；没有预约用车则全部；
    - 坐标坍塌组跳过（等滚动后重 dump 获得正确坐标）；
    - 每滑一屏 dump 一次，按 (段, 时段) 去重拼接；
    - 每个运力商每个标签产出一个 JSON：result/{tab}/{supplier}/fare_detail.json；
    - UI 树不可用时留空并告警（OCR 截图链路作为兜底仍在）。
    """
    from collector.platform.gaode.fare_detail_parse import (
        FareDetail,
        merge_fare_detail,
        parse_fare_detail_nodes,
        write_fare_detail_json,
    )
    from collector.platform.gaode.ui_tree_supplier import nodes_from_xml

    supplier = str(engine.state.get("supplier", "")).strip()
    tab = str(engine.state.get("tab", "")).strip()
    engine._log(f"── 计价UI提取: {supplier or '?'} / {tab or '?'} ──")

    merged = FareDetail(supplier=supplier, tab=tab)
    max_rounds = int(step.get("max_rounds", 12))
    sw = engine._screen_size[0] if engine._screen_size else 1200
    sh = engine._screen_size[1] if engine._screen_size else 2670
    no_add_streak = 0
    down_scrolls = 0

    for rnd in range(max_rounds):
        xml = engine.adb.dump_ui_tree()
        if not xml or not isinstance(xml, str):
            engine._log(f"  [计价UI] dump 失败(第{rnd+1}次)，停止（OCR 链路兜底）")
            break
        nodes = nodes_from_xml(xml)
        fd = parse_fare_detail_nodes(nodes, supplier, tab)
        added = merge_fare_detail(merged, fd)
        engine._log(
            f"  [计价UI] 第{rnd+1}轮 dump: 段{len(fd.sections)} "
            f"坍塌跳过{fd.collapsed_skipped} 新增{added}行"
        )
        if fd.stopped_at_yuyue:
            engine._log("  [计价UI] 已到「预约用车」，停止（只取其上）")
            break
        if added == 0:
            no_add_streak += 1
            if no_add_streak >= 2:
                engine._log("  [计价UI] 连续 2 轮无新增，停止")
                break
        else:
            no_add_streak = 0
        # 下滑一屏再 dump（把 WebView 坍塌区滚入可见区，获得正确坐标）
        engine.adb.slide(sw // 2, int(sh * 0.6), sw // 2, int(sh * 0.4), 500)
        engine._wait(step.get("scroll_wait", 0.6), "fare_scroll")
        down_scrolls += 1

    # 回到顶部：仿原 scroll_until_visible 的 scroll_back_to_top（否则下一轮切 tab 时 tab 在屏外）
    if down_scrolls:
        up_swipes = min(8, max(3, (down_scrolls + 1) // 2 + 1))
        engine._log(f"  [计价UI] 回滚到顶部（下滑 {down_scrolls} 次 → 上滑 {up_swipes} 次）")
        for _ in range(up_swipes):
            engine.adb.slide(sw // 2, int(sh * 0.25), sw // 2, int(sh * 0.75), 200)
            engine._wait(0.2, "fare_scroll_top")

    result_dir = str(Path(engine.output_dir).resolve().parent / "result")
    write_fare_detail_json(merged, result_dir, logger=engine._log)
    if merged.sections:
        engine._log("  [计价UI] 段: " + ", ".join(
            f"{s.title}({len(s.rows)})" for s in merged.sections))
    else:
        engine._log("  [计价UI] ⚠ 未提取到结构化内容（UI 树不可用，建议走 OCR 截图链路）")


def handle_ui_tree_click(engine, step: dict) -> bool:
    """ui_tree_click：用 UI 树定位目标并点击（零 LLM）。

    ui_target:
      - rail_economy : 左侧导航「经济」
      - q_button     : 当前运力商行的「?」问号（supplier 取自 state）
    成功返回 True；UI 树不可用/找不到返回 False（由 flow_engine 回退 VLM ground_click）。
    """
    target = step.get("ui_target", "")
    nodes = engine.state.get("_ui_tree_nodes")
    if not nodes:
        xml = engine.adb.dump_ui_tree()
        if not xml or not isinstance(xml, str):
            engine._log(f"  [UI树点击] dump 失败，回退 VLM ({target})")
            return False
        from collector.platform.gaode.ui_tree_supplier import nodes_from_xml
        nodes = nodes_from_xml(xml)
        engine.state["_ui_tree_nodes"] = nodes

    from collector.platform.gaode.ui_tree_supplier import (
        locate_q_button,
        locate_rail_category,
        locate_row_elements,
        locate_supplier_row,
        nodes_from_xml,
        parse_supplier_desc,
    )

    if target == "rail_economy":
        node = locate_rail_category(nodes, "经济")
        if node is None:
            engine._log("  [UI树点击] 未找到左侧导航「经济」，回退 VLM")
            return False
        cx, cy = node["center"]
        engine._log(f"  [UI树点击] 左侧导航「经济」@ ({cx},{cy})")
        engine.adb.click(cx, cy)
        engine._wait(step.get("wait_after", 0.5), "ui_rail_wait")
        return True

    if target == "q_button":
        supplier = str(engine.state.get("supplier", "")).strip()
        row = locate_supplier_row(nodes, supplier) if supplier else None
        if row is None:
            engine._log(f"  [UI树点击] 未找到「{supplier or '?'}」行，回退 VLM")
            return False

        # ── 勾选保证：被勾选才会有「?」→ 未选先点勾选框，重验后再点问号 ──
        if parse_supplier_desc(row.get("content_desc") or "").get("selected") is False:
            cb = locate_row_elements(nodes, row).checkbox
            if cb is None:
                engine._log(f"  [UI树点击] 「{supplier}」未勾选且找不到勾选框，回退 VLM")
                return False
            cx, cy = cb["center"]
            engine._log(f"  [UI树点击] 「{supplier}」未勾选 → 点勾选框 ({cx},{cy})")
            engine.adb.click(cx, cy)
            engine._wait(step.get("ensure_wait", 0.8), "ui_check_wait")
            xml = engine.adb.dump_ui_tree()
            if not xml or not isinstance(xml, str):
                return False
            nodes = nodes_from_xml(xml)
            engine.state["_ui_tree_nodes"] = nodes
            row = locate_supplier_row(nodes, supplier)
            if row is None or parse_supplier_desc(row.get("content_desc") or "").get("selected") is not True:
                engine._log(f"  [UI树点击] 「{supplier}」勾选后仍未选中，回退 VLM")
                return False
            engine._log(f"  [UI树点击] 「{supplier}」已勾选 ✓")

        q = locate_q_button(nodes, row)
        if q is None:
            engine._log(f"  [UI树点击] 未找到「{supplier}」问号，回退 VLM")
            return False
        cx, cy = q["center"]
        engine._log(f"  [UI树点击] 「{supplier}」问号 @ ({cx},{cy})")
        engine.adb.click(cx, cy)
        engine._wait(step.get("wait_after", 1.2), "ui_q_wait")
        return True

    engine._log(f"  [UI树点击] 未知 ui_target: {target}，回退 VLM")
    return False


def handle_select_all(engine, step: dict) -> None:
    """执行「全选/全选经济」幂等勾选（YAML 步骤 type: select_all）。"""
    label = step.get("label", "全选")
    engine._log(f"── 全选勾选: {label} ──")

    if _ensure_select_all_ui_tree(engine, label):
        engine.state["select_all_done"] = True   # 供 verify 步骤断言
        engine._log(f"  ✓ {label} 已勾选（UI 树）")
        return

    from collector.platform.gaode.select_all import ensure_all_selected

    region = step.get("expected_region") or         engine.profile_cfg.get("collection", {}).get("select_all_region")
    ensure_all_selected(
        adb=engine.adb,
        grounder=engine.grounder,
        label=label,
        screen_size=engine._screen_size,
        expected_region=region,
        screenshot=lambda name: engine._screenshot(name, save=True),
        stats=engine.stats,
        verbose=engine.verbose,
        wait_after_click=engine.timing.get("after_tap_wait", 2.0),
    )
    engine.state["select_all_done"] = True   # 供 verify 步骤断言
    engine._log(f"  ✓ {label} 已勾选（VLM）")


# ---------------------------------------------------------------------------
# 平台特有步骤：pricing_result_organize（YAML 流程末尾聚合结果，RES-01）
# ---------------------------------------------------------------------------

def handle_pricing_result_organize(engine, step: dict) -> None:
    """计价采集结束后，把必要截图聚合到 result/（RES-01，复用 FSM 路径的整理函数）。"""
    _organize_result_screenshots(engine)


# ---------------------------------------------------------------------------
# 平台特有步骤：extract_list / loop_until 的 S2 handler（YAML 原语流程 v4）
# ---------------------------------------------------------------------------

def handle_s2_list_suppliers(engine, step: dict) -> None:
    """extract_list: 识别当前屏经济型供应商列表（写入 engine.state）。"""
    from collector.platform.gaode.supplier_list import handle_s2_list_suppliers as _impl
    _impl(engine, step)


def handle_pricing_loop_done(engine, step: dict) -> bool:
    """loop_until: 当前屏无新的未采集运力商则终止。"""
    from collector.platform.gaode.supplier_list import handle_pricing_loop_done as _impl
    return _impl(engine, step)


def handle_mark_supplier_processed(engine, step: dict) -> None:
    """采集完成后标记当前运力商为已处理（防重复采集）。"""
    from collector.platform.gaode.supplier_list import handle_mark_supplier_processed as _impl
    _impl(engine, step)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_platform() -> Platform:
    base = Path(__file__).resolve().parent
    return Platform(
        name="gaode",
        flows_dir=base / "flows",
        profile_path=base / "profiles" / "gaode.json",
        default_flow="v1",
        add_cli_args=add_cli_args,
        build_flow_vars=build_flow_vars,
        step_handlers={
            "fare_detail_ui": handle_fare_detail_ui,
            "ui_tree_click": handle_ui_tree_click,
            "select_all": handle_select_all,
            "pricing_result_organize": handle_pricing_result_organize,
            "s2_list_suppliers": handle_s2_list_suppliers,
            "pricing_loop_done": handle_pricing_loop_done,
            "mark_supplier_processed": handle_mark_supplier_processed,
        },
    )
