"""
共享 ExecutionContext 测试（WS-1 P1）
============================================================================

验证 FlowEngine 与计价 FSM 共享的基础设施：
  - stats：默认值 / 递增 / api_seconds 透传
  - wait：等待时长归因（wait_seconds）
  - screenshot：debug/collect 落盘策略、探针临时目录、重试
  - annotate：debug 门控 + PNG 落盘
  - screen_size / scratch_dir / cleanup

用法:
  .venv/bin/python tests/test_execution_context.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collector.application.context import ExecutionContext
from collector.infrastructure.device.adb_utils import MockAdbTools


# ======================================================================
# Suite 1: stats
# ======================================================================

def test_stats_defaults():
    ctx = ExecutionContext(adb=MagicMock(), grounder=MagicMock(), output_dir="/tmp/x")
    assert ctx.stats["vlm_calls"] == 0
    assert ctx.stats["vlm_failures"] == 0
    assert ctx.stats["steps_executed"] == 0
    assert ctx.stats["api_seconds"] == 0.0
    assert ctx.stats["wait_seconds"] == 0.0
    assert ctx.stats["elapsed"] == 0.0
    return "PASS ✓"


def test_stats_incr():
    ctx = ExecutionContext(adb=MagicMock(), grounder=MagicMock(), output_dir="/tmp/x")
    ctx.incr_vlm_calls()
    ctx.incr_vlm_calls(2)
    ctx.incr_vlm_failures()
    ctx.incr_steps()
    assert ctx.stats["vlm_calls"] == 3
    assert ctx.stats["vlm_failures"] == 1
    assert ctx.stats["steps_executed"] == 1
    return "PASS ✓"


def test_api_seconds_passthrough():
    grounder = MagicMock()
    grounder.api_seconds = 1.5
    ctx = ExecutionContext(adb=MagicMock(), grounder=grounder, output_dir="/tmp/x")
    assert ctx.api_seconds == 1.5
    # 无 api_seconds 属性（mock）→ 0.0
    ctx2 = ExecutionContext(adb=MagicMock(), grounder=MagicMock(), output_dir="/tmp/x")
    assert ctx2.api_seconds == 0.0
    return "PASS ✓"


def test_wait_accrues_wait_seconds():
    ctx = ExecutionContext(adb=MagicMock(), grounder=MagicMock(), output_dir="/tmp/x")
    ctx.wait(0.01, "test")
    assert ctx.wait_seconds >= 0.01
    ctx.add_wait(0.02)
    assert ctx.wait_seconds >= 0.03
    # 非正值不计入
    before = ctx.wait_seconds
    ctx.wait(0, "noop")
    ctx.add_wait(None)
    assert ctx.wait_seconds == before
    return "PASS ✓"


def test_shared_stats_identity():
    """两个子流程共享同一 ctx → stats 同一 dict，自动归并。"""
    ctx = ExecutionContext(adb=MagicMock(), grounder=MagicMock(), output_dir="/tmp/x")
    a, b = ctx, ctx  # 模拟 FlowEngine 与 FSM 共享
    a.incr_vlm_calls(2)
    b.incr_vlm_calls(3)
    assert ctx.stats["vlm_calls"] == 5
    assert a.stats is b.stats is ctx.stats
    return "PASS ✓"


# ======================================================================
# Suite 2: screen_size / scratch_dir
# ======================================================================

def test_screen_size():
    adb = MagicMock()
    adb.screen_size = (1080, 2400)
    ctx = ExecutionContext(adb=adb, grounder=MagicMock(), output_dir="/tmp/x")
    assert ctx.screen_size == (1080, 2400)
    return "PASS ✓"


def test_screen_size_missing_raises():
    adb = MagicMock()
    adb.screen_size = None
    ctx = ExecutionContext(adb=adb, grounder=MagicMock(), output_dir="/tmp/x")
    try:
        ctx.screen_size
    except RuntimeError:
        return "PASS ✓"
    raise AssertionError("应抛 RuntimeError")


def test_scratch_dir_mode_gated():
    adb = MagicMock()
    ctx_debug = ExecutionContext(adb=adb, grounder=MagicMock(), output_dir="/tmp/x", mode="debug")
    assert ctx_debug.scratch_dir is None
    ctx_collect = ExecutionContext(adb=adb, grounder=MagicMock(), output_dir="/tmp/y", mode="collect")
    assert ctx_collect.scratch_dir is not None
    scratch = ctx_collect.scratch_dir
    assert scratch.exists()
    ctx_collect.cleanup()
    assert not scratch.exists()
    return "PASS ✓"


# ======================================================================
# Suite 3: screenshot
# ======================================================================

def test_capture_debug_saves_to_screenshots():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ExecutionContext(adb=MockAdbTools(), grounder=MagicMock(),
                               output_dir=str(Path(tmp) / "out"), mode="debug")
        path = ctx.capture("01_step")
        assert path is not None
        p = Path(path)
        assert p.parent == ctx.screenshots_dir
        assert p.exists()
    return "PASS ✓"


def test_capture_collect_goes_to_scratch():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        ctx = ExecutionContext(adb=MockAdbTools(), grounder=MagicMock(),
                               output_dir=str(out), mode="collect")
        path = ctx.capture("probe_x")
        assert path is not None
        p = Path(path)
        assert p.parent != out / "screenshots", "collect 探针帧不应落盘"
        assert p.parent == ctx.scratch_dir
        # save=True 强制落盘（select_all_after 语义）
        path2 = ctx.capture("select_all_after", save=True)
        assert Path(path2).parent == out / "screenshots"
    return "PASS ✓"


def test_capture_retry_success_and_failure():
    with tempfile.TemporaryDirectory() as tmp:
        # 前两次失败，第三次成功 → True
        adb = MagicMock()
        adb.get_screenshot.side_effect = [False, False, True]
        ctx = ExecutionContext(adb=adb, grounder=MagicMock(), output_dir=str(Path(tmp) / "out"))
        assert ctx.capture_to(str(Path(tmp) / "a.jpg")) is True
        # 全部失败 → False / None
        adb2 = MagicMock()
        adb2.get_screenshot.return_value = False
        ctx2 = ExecutionContext(adb=adb2, grounder=MagicMock(), output_dir=str(Path(tmp) / "out2"))
        assert ctx2.capture_to(str(Path(tmp) / "b.jpg")) is False
        assert ctx2.capture("c") is None
    return "PASS ✓"


# ======================================================================
# Suite 4: annotate
# ======================================================================

def test_annotate_debug_only():
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src.png"
        Image.new("RGB", (20, 20), "white").save(src)

        # collect：不输出标记图
        out_collect = tmp / "out_collect"
        ctx1 = ExecutionContext(adb=MagicMock(), grounder=MagicMock(),
                                output_dir=str(out_collect), mode="collect")
        ctx1.annotate(str(src), "tag1", lambda d: None)
        assert not (out_collect / "annotations" / "tag1.png").exists()

        # debug：输出标记图
        out_debug = tmp / "out_debug"
        ctx2 = ExecutionContext(adb=MagicMock(), grounder=MagicMock(),
                                output_dir=str(out_debug), mode="debug")
        ctx2.annotate(str(src), "tag2", lambda d: d.rectangle((1, 1, 5, 5)))
        assert (out_debug / "annotations" / "tag2.png").exists()
    return "PASS ✓"


def test_annotate_missing_source_no_crash():
    ctx = ExecutionContext(adb=MagicMock(), grounder=MagicMock(),
                           output_dir="/tmp/anno_x", mode="debug")
    ctx.annotate("/tmp/not_exist.png", "tag", lambda d: None)  # 不抛异常
    return "PASS ✓"


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    print("=" * 60)
    print("  ExecutionContext Tests (WS-1 P1)")
    print("=" * 60)

    suites = [
        ("Suite 1: stats", [
            ("默认值", test_stats_defaults),
            ("递增", test_stats_incr),
            ("api_seconds 透传", test_api_seconds_passthrough),
            ("wait 归因", test_wait_accrues_wait_seconds),
            ("共享 stats 归并", test_shared_stats_identity),
        ]),
        ("Suite 2: screen/scratch", [
            ("screen_size", test_screen_size),
            ("screen_size 缺失抛错", test_screen_size_missing_raises),
            ("scratch_dir 模式门控 + cleanup", test_scratch_dir_mode_gated),
        ]),
        ("Suite 3: screenshot", [
            ("debug 落盘 screenshots", test_capture_debug_saves_to_screenshots),
            ("collect 探针进 scratch", test_capture_collect_goes_to_scratch),
            ("重试成功/失败", test_capture_retry_success_and_failure),
        ]),
        ("Suite 4: annotate", [
            ("debug 门控", test_annotate_debug_only),
            ("源缺失不崩溃", test_annotate_missing_source_no_crash),
        ]),
    ]

    total = failed = 0
    for suite_name, tests in suites:
        print(f"\n── {suite_name} ──")
        for label, fn in tests:
            total += 1
            try:
                s = fn()
                print(f"  [{s}] {label}")
                if "FAIL" in s:
                    failed += 1
            except Exception as e:
                print(f"  [FAIL ✗] {label}: {e}")
                failed += 1

    print(f"\n  通过 {total - failed}/{total}")
    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
