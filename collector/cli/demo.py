"""
智能采集 Demo — Entry Point
============================================================================

多平台截图采集（当前内置 gaode），支持多流程版本 + 平台特有步骤（如计价采集）。
平台由 `--platform` 选择，新增平台只需在 collector/platform/<name>/ 实现并注册。

VLM 调用方式与 Ali GUIOwlWrapper 一致 (OpenAI client + smart_resize + base64).

Usage:
  # v1 — 搜索框进入（打车页到即结束）
  .venv/bin/python -m collector.demo \\
      --platform gaode --flow v1 \\
      --address "西北旺万象汇" \\
      --adb-path /opt/homebrew/bin/adb \\
      --vlm-api-key "sk-xxx" \\
      --vlm-base-url "https://ws-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

  # v2 — 底部打车tab进入 + 计价采集（YAML 子流程 subflows/pricing_collect_gaode.yaml）
  .venv/bin/python -m collector.demo \\
      --platform gaode --flow v2 \\
      --address "西北旺万象汇" \\
      --pickup "北京西站" \\
      --adb-path /opt/homebrew/bin/adb \\
      --vlm-api-key "sk-xxx" \\
      --vlm-base-url "https://ws-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parents[1]  # <root>/collector
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collector.infrastructure.device.adb_utils import AdbTools
from collector.infrastructure.vision.ocr_adapter import OcrTextExtractor
from collector.infrastructure.vision.vlm_grounder import VLMGrounder
from collector.platform.registry import available_platforms, get_platform
from collector.workflows.flow_engine import FlowEngine


def _peek_platform_name(argv: list[str]) -> str:
    """从 argv 预扫描 --platform 的值（未传则用默认 gaode）。"""
    for i, a in enumerate(argv):
        if a == "--platform" and i + 1 < len(argv):
            return argv[i + 1]
    return "gaode"


def _resolve_platform(argv: list[str]):
    """解析平台；未知平台回退默认，让 argparse 输出 choices 错误。"""
    name = _peek_platform_name(argv)
    try:
        return get_platform(name)
    except KeyError:
        return get_platform("gaode")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="智能采集 Demo — 移动端打车截图采集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Platform / Flow ──
    parser.add_argument("--platform", default="gaode",
                        choices=available_platforms(),
                        help=f"平台 (默认: gaode，可选: {', '.join(available_platforms())})")
    parser.add_argument("--flow", default=None,
                        help="流程名（默认: 平台默认流程，如 gaode 的 v1）")

    # ── 公共参数 ──
    parser.add_argument("--adb-path", required=True,
                        help="ADB 可执行文件路径")
    parser.add_argument("--vlm-api-key", required=True,
                        help="DashScope / MaaS API Key")
    parser.add_argument("--vlm-base-url", required=True,
                        help="VLM OpenAI-compatible base URL")
    parser.add_argument("--device", help="设备序列号")
    parser.add_argument("--vlm-model", default="qwen3-vl-plus",
                        help="VLM 模型 ID (默认: qwen3-vl-plus)")
    parser.add_argument("--output-dir", default="./output",
                        help="截图输出目录 (默认: ./output)")
    parser.add_argument("--quiet", action="store_true",
                        help="静默模式")
    parser.add_argument("--image-max-pixels", type=int, default=400000,
                        help="截图最大像素数 (默认: 400000)")
    parser.add_argument("--mode", default="debug", choices=["debug", "collect"],
                        help="debug: 每步截图+标记图；collect: 仅保存详细计价页截图 (默认: debug)")
    parser.add_argument("--no-ocr", action="store_true",
                        help="关闭本地 OCR（默认开启，用于「预约用车」检测；OCR_PROFILE=DEV 切环境）")

    # ── 预扫描 --platform，注册平台自己的参数（如 gaode 的 --address/--pickup），
    #    保证 `--help` 也能完整展示平台参数 ──
    platform = _resolve_platform(sys.argv)
    if platform.add_cli_args:
        platform.add_cli_args(parser)
    args = parser.parse_args()

    if not Path(args.adb_path).exists():
        print(f"❌ ADB not found at: {args.adb_path}")
        sys.exit(1)

    # ── 解析流程 ──
    flow_name = args.flow or platform.default_flow
    flow_file = platform.resolve_flow(flow_name)
    if not flow_file.exists():
        print(f"❌ Flow 文件不存在: {flow_file}")
        print(f"   平台 {platform.name} 可用流程: {', '.join(platform.list_flow_names())}")
        sys.exit(1)

    flow_vars = platform.build_flow_vars(args, flow_name) if platform.build_flow_vars else {}
    profile_cfg = platform.load_profile()

    # ── Bootstrap ──
    print("=" * 60)
    print(f"  智能采集 Demo — {platform.name} (flow={flow_name})")
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

    engine = FlowEngine(
        adb=adb,
        grounder=grounder,
        flow_path=str(flow_file),
        vars_=flow_vars,
        output_dir=args.output_dir,
        verbose=not args.quiet,
        profile_cfg=profile_cfg,
        platform_step_handlers=platform.step_handlers,
        mode=args.mode,
        text_extractor=None if args.no_ocr else OcrTextExtractor(),
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
    finally:
        engine.cleanup()  # 删除 collect 模式的临时截图目录

    elapsed = time.time() - t_start
    print(f"\n[Flow] 耗时: {elapsed:.1f}s")
    print(f"[Flow] VLM: {engine.stats['vlm_calls']} 次调用, "
          f"失败: {engine.stats['vlm_failures']}")
    print(f"[Flow] API: {engine.stats.get('api_seconds', 0):.1f}s | "
          f"等待: {engine.stats.get('wait_seconds', 0):.1f}s")


if __name__ == "__main__":
    main()
