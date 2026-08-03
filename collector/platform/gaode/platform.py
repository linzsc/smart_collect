"""高德平台接入描述
============================================================================

把高德特有的 CLI 参数、模板变量和 `pricing_collect` 步骤封装为 Platform，
注册到 `collector/platform/registry.py`。通用引擎不再直接依赖本模块。
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


# ---------------------------------------------------------------------------
# 流程模板变量
# ---------------------------------------------------------------------------

def build_flow_vars(args: argparse.Namespace, flow_name: str) -> dict[str, str]:
    vars_: dict[str, str] = {"Address": args.address}
    if flow_name in ("v2", "v3"):
        vars_["Pickup"] = args.pickup or "我的位置"
    return vars_


# ---------------------------------------------------------------------------
# 平台特有步骤：pricing_collect（委托给 RidePricingFSM）
# ---------------------------------------------------------------------------

def handle_pricing_collect(engine, step: dict) -> None:
    """执行计价采集子流程（原 FlowEngine._do_pricing_collect 逻辑）。"""
    supplier = step.get("supplier", "经济型")
    engine._log(f"── 计价采集: {supplier} ──")

    from collector.platform.gaode.ride_pricing import RidePricingFSM

    pricer = RidePricingFSM(
        adb=engine.adb,
        grounder=engine.grounder,
        supplier=supplier,
        profile_cfg=engine.profile_cfg,
        output_dir=str(engine.output_dir),
        verbose=engine.verbose,
        mode=engine.mode,
        scratch_dir=str(engine.scratch_dir) if engine.scratch_dir else None,
    )
    pricer.run()
    # 合并 VLM 统计
    engine.stats["vlm_calls"] += pricer.stats.get("vlm_calls", 0)
    engine.stats["vlm_failures"] += pricer.stats.get("vlm_failures", 0)


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
        step_handlers={"pricing_collect": handle_pricing_collect},
    )
