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
    parser.add_argument("--capture-mode", default="full", choices=["full", "test"],
                        help="计价采集模式: full=完整 detail_capture（工作日/休息日滚动，默认）; "
                             "test=轻量测试（仅点问号进入计价页，不做滚动）")


# ---------------------------------------------------------------------------
# 流程模板变量
# ---------------------------------------------------------------------------

def build_flow_vars(args: argparse.Namespace, flow_name: str) -> dict[str, str]:
    vars_: dict[str, str] = {"Address": args.address}
    if flow_name in ("v2", "v3"):   # 需要上车点
        vars_["Pickup"] = args.pickup or "我的位置"
    # 计价采集子流程：full=detail_capture / test=detail_entry_test
    vars_["DetailSubflow"] = ("detail_capture_gaode.yaml"
                              if getattr(args, "capture_mode", "full") == "full"
                              else "detail_entry_test_gaode.yaml")
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
        for tab in ("工作日", "休息日"):
            for supp, files in groups.get(tab, {}).items():
                engine._log(f"    {tab}/{supp}: 滚动截图 {len(files)} 张 + 打车页")
    except Exception as e:
        engine._log(f"  ⚠ 结果整理失败: {e}")


# ---------------------------------------------------------------------------
# 平台特有步骤：select_all（目标锚定的幂等全选，SEL-01）
# ---------------------------------------------------------------------------

def handle_select_all(engine, step: dict) -> None:
    """执行「全选/全选经济」幂等勾选（YAML 步骤 type: select_all）。"""
    label = step.get("label", "全选")
    engine._log(f"── 全选勾选: {label} ──")

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
    engine._log(f"  ✓ {label} 已勾选")


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
            "select_all": handle_select_all,
            "pricing_result_organize": handle_pricing_result_organize,
            "s2_list_suppliers": handle_s2_list_suppliers,
            "pricing_loop_done": handle_pricing_loop_done,
            "mark_supplier_processed": handle_mark_supplier_processed,
        },
    )
