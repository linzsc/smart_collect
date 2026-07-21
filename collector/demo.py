"""
智能采集 Demo — Entry Point
============================================================================

高德地图打车采集，支持多流程版本 (v1/v2) + 计价采集子流程。

VLM 调用方式与 Ali GUIOwlWrapper 一致 (OpenAI client + smart_resize + base64).

Usage:
  # v1 — 搜索框进入
  .venv/bin/python -m collector.demo \\
      --address "西北旺万象汇" \\
      --adb-path /opt/homebrew/bin/adb \\
      --vlm-api-key "sk-xxx" \\
      --vlm-base-url "https://ws-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" \\
      --flow v1

  # v2 — 底部打车tab进入（需 --pickup 上车点）
  .venv/bin/python -m collector.demo \\
      --address "西北旺万象汇" \\
      --pickup "北京西站" \\
      --adb-path /opt/homebrew/bin/adb \\
      --vlm-api-key "sk-xxx" \\
      --vlm-base-url "https://ws-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" \\
      --flow v2

  # 带计价采集
  .venv/bin/python -m collector.demo \\
      --address "西北旺万象汇" \\
      --adb-path /opt/homebrew/bin/adb \\
      --vlm-api-key "sk-xxx" \\
      --vlm-base-url "https://ws-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" \\
      --flow v1 --collect
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from collector.adb_utils import AdbTools
from collector.flow_engine import FlowEngine
from collector.ride_pricing import RidePricingFSM
from collector.vlm_grounder import VLMGrounder


def main() -> None:
    parser = argparse.ArgumentParser(
        description="智能采集 Demo — 高德地图打车计价采集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Required ──
    parser.add_argument("--address", required=True,
                        help="目的地，例如 '北京西站' / '西北旺万象汇'")
    parser.add_argument("--adb-path", required=True,
                        help="ADB 可执行文件路径")
    parser.add_argument("--vlm-api-key", required=True,
                        help="DashScope / MaaS API Key")
    parser.add_argument("--vlm-base-url", required=True,
                        help="VLM OpenAI-compatible base URL")

    # ── Flow ──
    parser.add_argument("--flow", default="v1", choices=["v1", "v2", "v3"],
                        help="流程版本 (默认: v1)")
    parser.add_argument("--pickup",
                        help="上车点 (v2 需要，例如 '北京西站')")

    # ── Optional ──
    parser.add_argument("--device", help="设备序列号")
    parser.add_argument("--vlm-model", default="qwen3-vl-plus",
                        help="VLM 模型 ID (默认: qwen3-vl-plus)")
    parser.add_argument("--output-dir", default="./output",
                        help="截图输出目录 (默认: ./output)")
    parser.add_argument("--quiet", action="store_true",
                        help="静默模式")
    parser.add_argument("--collect", action="store_true",
                        help="进入打车页后执行计价采集子流程")
    parser.add_argument("--supplier", default="经济型",
                        help="要采集的供应商名称 (默认: 经济型)")
    parser.add_argument("--image-max-pixels", type=int, default=400000,
                        help="截图最大像素数 (默认: 400000)")

    args = parser.parse_args()

    if not Path(args.adb_path).exists():
        print(f"❌ ADB not found at: {args.adb_path}")
        sys.exit(1)

    # ── Bootstrap ──
    print("=" * 60)
    print(f"  智能采集 Demo — 高德地图 (flow={args.flow})")
    print("=" * 60)

    adb = AdbTools(args.adb_path, device=args.device)
    grounder = VLMGrounder(
        api_key=args.vlm_api_key,
        base_url=args.vlm_base_url,
        model=args.vlm_model,
        image_max_pixels=args.image_max_pixels,
    )

    # 清理上次结果
    output_path = Path(args.output_dir)
    if output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    # 检查 ADB
    print("\n[Setup] 检查 ADB 连接...")
    test_shot = str(output_path / "_test_connection.png")
    if not adb.get_screenshot(test_shot):
        print("❌ 无法连接设备。")
        sys.exit(1)
    print(f"  ✓ 设备已连接，屏幕尺寸: {adb.screen_size}")

    # ── 流程变量 ──
    flow_vars = {"Address": args.address}
    if args.flow in ("v2", "v3"):
        flow_vars["Pickup"] = args.pickup or "我的位置"

    # ── 加载 Flow YAML ──
    flow_file = _THIS_DIR / "flows" / f"{args.flow}_gaode.yaml"
    if not flow_file.exists():
        print(f"❌ Flow 文件不存在: {flow_file}")
        sys.exit(1)

    engine = FlowEngine(
        adb=adb,
        grounder=grounder,
        flow_path=str(flow_file),
        vars_=flow_vars,
        output_dir=args.output_dir,
        verbose=not args.quiet,
    )

    t_start = time.time()
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n⚠ 用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 运行出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - t_start
    print(f"\n[Flow] 耗时: {elapsed:.1f}s")
    print(f"[Flow] VLM: {engine.stats['vlm_calls']} 次调用, "
          f"失败: {engine.stats['vlm_failures']}")

    # ── 计价采集子流程 ──
    if args.collect:
        print("\n" + "=" * 60)
        print("  计价规则采集流程")
        print("=" * 60)

        profile_path = str(_THIS_DIR / "profiles" / "gaode.json")
        with open(profile_path, "r", encoding="utf-8") as f:
            profile_cfg = json.load(f)

        pricer = RidePricingFSM(
            adb=adb,
            grounder=grounder,
            supplier=args.supplier,
            profile_cfg=profile_cfg,
            output_dir=args.output_dir,
            verbose=not args.quiet,
        )

        t2 = time.time()
        try:
            results = pricer.run()
            print(f"\n[Pricing] 截图: {len(results)} 张")
        except KeyboardInterrupt:
            print("\n⚠ 用户中断")
        except Exception as e:
            print(f"\n❌ 计价采集出错: {e}")
            import traceback
            traceback.print_exc()

        print(f"\n[Pricing] 耗时: {time.time() - t2:.1f}s")


if __name__ == "__main__":
    main()
