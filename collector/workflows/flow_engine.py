"""
Config-Driven Flow Engine
============================================================================

Generic YAML-driven executor for mobile UI automation flows.

Each flow is a YAML file listing steps. The engine executes them linearly,
using VLM for all visual grounding. No hardcoded coordinates, no
per-flow Python code needed — just YAML.

Step types:
  - open_app          : Launch an app by package name
  - ground_click      : Screenshot → VLM ground element → click
  - input_text        : Screenshot → click input → clear → type → confirm
  - scroll            : Swipe gesture
  - wait              : Sleep
  - screenshot        : Save screenshot (does NOT return path)

Features:
  - Template variables: {{.Address}}, {{.Pickup}}
  - Page verification: optional VLM page classification before grounding
  - Recovery: strategies for page mismatches (vlm_find_and_click, back_retry)
  - Fallback: second grounding prompt if first fails
  - Debug: every action annotated on screenshot
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from string import Template
from typing import Any

from collector.application.context import ExecutionContext
from collector.infrastructure.device.adb_utils import AdbTools
from collector.infrastructure.vision.vlm_grounder import VLMGrounder


# ---------------------------------------------------------------------------
# Flow Engine
# ---------------------------------------------------------------------------

class FlowEngine:
    """Execute a YAML-defined mobile automation flow."""

    _ANNO_BBOX = "#FF0000"
    _ANNO_DOT = "#00FF00"

    def __init__(
        self,
        adb: AdbTools,
        grounder: VLMGrounder,
        flow_path: str,
        vars_: dict[str, str] | None = None,
        output_dir: str = "./output",
        verbose: bool = True,
        profile_cfg: dict[str, Any] | None = None,
        platform_step_handlers: dict[str, Any] | None = None,
        mode: str = "debug",
        text_extractor: Any | None = None,
    ):
        self.adb = adb
        self.grounder = grounder
        self.vars = vars_ or {}
        self.verbose = verbose
        self.profile_cfg = profile_cfg or {}
        # 平台特有步骤（如 gaode 的 select_all）由 Platform.step_handlers 注入，
        # 通用引擎不直接依赖任何平台模块。
        self._platform_step_handlers = platform_step_handlers or {}
        # 共享执行上下文：stats/等待/截图/标注/日志 收敛到 application/context.py。
        # 计价 FSM 等子流程通过 engine.ctx 共享同一实例，统计与等待自动归并。
        self.ctx = ExecutionContext(
            adb=adb, grounder=grounder,
            output_dir=output_dir, mode=mode, verbose=verbose,
            log_prefix="Flow", text_extractor=text_extractor,
        )
        self.output_dir = self.ctx.output_dir
        self.mode = self.ctx.mode
        self.stats = self.ctx.stats

        self._flow_dir = Path(flow_path).resolve().parent  # subflow 相对路径解析基准

        with open(flow_path, "r", encoding="utf-8") as f:
            self.flow = self._deep_resolve(self.vars, f.read())

        self.timing = self.flow.get("timing", {})
        self.package = self.flow.get("package", "")
        # 运行时状态：extract_list / for_each / loop_until / verify 读写这里，
        # 模板 `{{.S.<key>}}` 引用（区别于加载期变量 `{{.<Key>}}`）。
        self.state: dict[str, Any] = dict(self.flow.get("init_state", {}))
        self._shot_seq = 0

    # ------------------------------------------------------------------
    # 输出模式
    # ------------------------------------------------------------------

    @property
    def debug_mode(self) -> bool:
        """debug 模式：每步截图 + 标记图。"""
        return self.ctx.debug_mode

    @property
    def scratch_dir(self) -> Path | None:
        """collect 模式下的临时截图目录（供 VLM 定位，不计入最终输出）。"""
        return self.ctx.scratch_dir

    def cleanup(self) -> None:
        """删除 collect 模式的临时截图目录。"""
        self.ctx.cleanup()

    # ------------------------------------------------------------------
    # 耗时统计
    # ------------------------------------------------------------------

    def _wait(self, seconds: float, tag: str = "") -> None:
        """带统计的等待：累加流程设定的等待时长（归入共享 ctx）。"""
        self.ctx.wait(seconds, tag)

    def add_wait(self, seconds: float) -> None:
        """合并子流程（如平台 handler）产生的等待时长，用于总耗时归因。"""
        self.ctx.add_wait(seconds)

    @property
    def _wait_total(self) -> float:
        """累计流程等待时长（委托 ctx，供耗时归因读取）。"""
        return self.ctx.wait_seconds

    def _api_seconds(self) -> float:
        """当前累计 API 耗时（对 mock 兼容返回 0.0）。"""
        return self.ctx.api_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute all steps in sequence."""
        name = self.flow.get("name", "unnamed")
        self._log(f"── {name} ──")
        self._shot_seq = 0
        run_t0 = time.time()
        api_t0 = self._api_seconds()
        wait_t0 = self._wait_total

        self._run_steps(self.flow.get("steps", []))

        self.stats["elapsed"] = time.time() - run_t0
        self.stats["api_seconds"] = self._api_seconds() - api_t0
        self.stats["wait_seconds"] = self._wait_total - wait_t0
        self._log(f"\n── {name} 完成 ──")
        self._log(f"步骤: {self.stats['steps_executed']}, VLM: {self.stats['vlm_calls']} 次")
        self._log(
            f"⏱ 总耗时 {self.stats['elapsed']:.1f}s | "
            f"API {self.stats['api_seconds']:.1f}s | 等待 {self.stats['wait_seconds']:.1f}s"
        )

    def _run_steps(self, steps: list[dict]) -> None:
        """顺序执行一组步骤（顶层 run 与 for_each/loop_until/subflow 共用）。"""
        for step in steps:
            self.stats["steps_executed"] += 1
            step_id = self._render(step.get("id", "?"))
            step_type = step.get("type", "?")
            optional = step.get("optional", False)

            self._log(f"\n[{step_id}] ({step_type}) {step.get('description', '')}")
            t0 = time.time()
            api0 = self._api_seconds()
            wait0 = self._wait_total

            try:
                if step_type == "open_app":
                    self._do_open_app(step)
                elif step_type == "ground_click":
                    self._do_ground_click(step)
                elif step_type == "ground_doublecheck":
                    self._do_ground_doublecheck(step)
                elif step_type == "scroll_until_visible":
                    self._do_scroll_until_visible(step)
                elif step_type == "input_text":
                    self._do_input_text(step)
                elif step_type == "scroll":
                    self._do_scroll(step)
                elif step_type == "wait":
                    self._do_wait(step)
                elif step_type == "back":
                    self._do_back(step)
                elif step_type == "screenshot":
                    self._do_screenshot(step)
                elif step_type == "extract_list":
                    self._do_extract_list(step)
                elif step_type == "for_each":
                    self._do_for_each(step)
                elif step_type == "loop_until":
                    self._do_loop_until(step)
                elif step_type == "subflow":
                    self._do_subflow(step)
                elif step_type == "verify":
                    self._do_verify(step)
                elif step_type in self._platform_step_handlers:
                    self._platform_step_handlers[step_type](self, step)
                else:
                    self._log(f"  ⚠ 未知步骤类型: {step_type}")
            except StepFailed as e:
                if optional:
                    self._log(f"  ⚠ 跳过可选步骤: {e}")
                else:
                    raise
            except Exception as e:
                if optional:
                    self._log(f"  ⚠ 可选步骤异常，跳过: {e}")
                else:
                    raise

            step_s = time.time() - t0
            api_s = self._api_seconds() - api0
            wait_s = self._wait_total - wait0
            self._log(
                f"  ⏱ [{step_id}] 步骤 {step_s:.1f}s | "
                f"API {api_s:.1f}s | 等待 {wait_s:.1f}s"
            )

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    def _do_open_app(self, step: dict) -> None:
        pkg = step.get("package", self.package)
        self.adb.open_app(pkg)
        self._wait(self.timing.get("app_launch_wait", 3.0), "app_launch_wait")
        if self.debug_mode:
            self._screenshot(f"{step.get('id', 'open_app')}_after")

    def _do_ground_click(self, step: dict) -> None:
        # ── 坐标缓存（PERF-03 语义）：cache_key 命中直接点击，不调 VLM/不截图 ──
        cache_key = self._render(step.get("cache_key"))
        if cache_key:
            cached = self.state.get(cache_key)
            if isinstance(cached, (list, tuple)) and len(cached) == 2:
                cx, cy = int(cached[0]), int(cached[1])
                self._log(f"  ♻ 复用缓存坐标 {cache_key}: ({cx},{cy})")
                self.adb.click(cx, cy)
                self._wait(self._after_tap_wait(step), "after_tap_wait")
                return

        shot = self._screenshot(step.get("id", "step"))
        ref_img = self._resolve_ref(step.get("ref_image"))
        ref_imgs = self._resolve_refs(step.get("ref_images"))

        # ── optional: page verification ──
        verify_cfg = step.get("verify_page")
        if verify_cfg:
            page_type = self._verify_page(shot, verify_cfg.get("expected", ""))
            if page_type != verify_cfg.get("expected"):
                self._log(f"  ⚠ 页面不匹配: 预期={verify_cfg.get('expected')} 实际={page_type}")
                if self._try_recovery(shot, verify_cfg.get("on_page_mismatch", [])):
                    shot = self._screenshot(f"{step.get('id','step')}_recovered")
                else:
                    self._log("  ⚠ 恢复失败，继续尝试定位目标元素")

        # ── grounding（描述支持 `{{.S.<key>}}` 运行时状态模板）──
        cfg = step.get("ground", {})
        desc = self._render(cfg.get("element_desc", ""))
        alts = [self._render(a) for a in cfg.get("retry_alt_descs", [])]

        center = self._vlm_ground_and_click(shot, desc, alts, step.get("id", "?"),
                                            ref_image=ref_img, ref_images=ref_imgs)

        if center:
            if cache_key:
                self.state[cache_key] = [center[0], center[1]]
            self._wait(self._after_tap_wait(step), "after_tap_wait")
            return

        # ── fallback ──
        fallback = step.get("fallback")
        if fallback:
            self._log("  🔄 兜底查找…")
            center = self._vlm_ground_and_click(shot, self._render(fallback.get("prompt", "")),
                                                 [], f"{step.get('id','?')}_fallback",
                                                 ref_image=ref_img, ref_images=ref_imgs)
            if center:
                self._wait(self._after_tap_wait(step), "after_tap_wait")
                return

        if step.get("mandatory"):
            raise StepFailed(f"mandatory ground_click '{step.get('id')}' failed")
        self._log(f"  ⚠ 未找到目标元素")

    def _after_tap_wait(self, step: dict) -> float:
        """步骤可配置等待（wait_after），缺省用 timing.after_tap_wait。"""
        wa = step.get("wait_after")
        return float(wa) if wa is not None else float(self.timing.get("after_tap_wait", 2.0))

    def _do_ground_doublecheck(self, step: dict) -> None:
        """ground_click + 截图重验证（用于全选等需确认的操作）.

        Flow:
          1. 第一轮: VLM 判断 SELECTED=true/false, selected=true → return
          2. selected=false → 截图 + VLM ground → click
          3. 第二轮: 截图重验证, selected=true → done, 否则再 click 一次
        """
        step_id = step.get("id", "?")
        cfg = step.get("ground", {})
        desc = cfg.get("element_desc", "")
        alts = cfg.get("retry_alt_descs", [])
        ref_img = self._resolve_ref(step.get("ref_image"))
        ref_imgs = self._resolve_refs(step.get("ref_images"))

        # ── 第一轮: VLM 判断状态 ──
        shot = self._screenshot(f"{step_id}_1")
        result1 = self._vlm_ground_ref(shot, desc, ref_img, ref_imgs)

        if result1.selected:
            self._log("  ✓ 首轮判断已选中，跳过点击")
            return

        if result1.found and result1.center:
            bbox = result1.bbox or [0, 0, 0, 0]
            cx, cy = result1.center
            self._log(f"  ○ 首轮判断未选中 → 点击 ({cx},{cy})")
            self._annotate(shot, f"{step_id}_1", bbox, cx, cy, 0)
            self.adb.click(cx, cy)
            self._wait(self.timing.get("after_tap_wait", 2.0), "after_tap_wait")
        else:
            # 首轮没找到，尝试 alt_descs
            self._log("  ⚠ 首轮未找到元素，尝试 alt descs")
            center = self._vlm_ground_and_click(shot, desc, alts, f"{step_id}_1",
                                                 ref_image=ref_img, ref_images=ref_imgs)
            if center:
                self._wait(self.timing.get("after_tap_wait", 2.0), "after_tap_wait")
            else:
                self._log("  ⚠ 首轮仍失败，继续重验证")
                if step.get("mandatory"):
                    raise StepFailed(f"ground_doublecheck '{step_id}' first round failed")

        # ── 第二轮: 截图重验证 ──
        self._log("  🔍 Double check…")
        shot2 = self._screenshot(f"{step_id}_2")
        result2 = self._vlm_ground_ref(shot2, desc, ref_img, ref_imgs)

        if result2.selected:
            self._log("  ✓ Double check 已确认选中")
            return

        # 第二轮未选中 → 再 click 一次
        self._log("  ⚠ Double check 未选中，再次点击")
        if result2.found and result2.center:
            bbox2 = result2.bbox or [0, 0, 0, 0]
            cx2, cy2 = result2.center
            self._annotate(shot2, f"{step_id}_retry", bbox2, cx2, cy2, 0)
            self.adb.click(cx2, cy2)
            self._wait(self.timing.get("after_tap_wait", 2.0), "after_tap_wait")
        else:
            center2 = self._vlm_ground_and_click(
                shot2, desc, alts, f"{step_id}_retry",
                ref_image=ref_img, ref_images=ref_imgs)
            if center2:
                self._wait(self.timing.get("after_tap_wait", 2.0), "after_tap_wait")

    def _do_scroll_until_visible(self, step: dict) -> None:
        """小步下滑直到 VLM 在截图中发现指定文字。

        增强选项：
          - frame_suffix: 滚动帧文件名后缀（如 `{{.S.supplier}}`，供 result 聚合分组）
          - stop_on_stable: 未命中标记且页面不再变化（像素比对）时停止
          - stable_threshold: 变化像素占比阈值（默认 0.01）
          - wait_after_slide: 每次滑动后等待（默认 0.5s）
          - scroll_back_to_top: 结束后快速回顶 3 次
        """
        step_id = self._render(step.get("id", "?"))
        target = self._render(step.get("target_text", ""))
        max_swipes = step.get("max_swipes", 15)
        suffix = self._render(step.get("frame_suffix", ""))
        stable_threshold = float(step.get("stable_threshold", 0.01))
        wait_after_slide = float(step.get("wait_after_slide", 0.5))
        sw, sh = self._screen_size
        amount = sh // 3
        y1, y2 = sh * 3 // 4, sh * 3 // 4 - amount

        prev = None
        for i in range(max_swipes):
            stem = f"{step_id}_scroll_{i}" + (f"_{suffix}" if suffix else "")
            shot = self._screenshot(stem)
            if self.debug_mode:
                self._annotate_swipe(shot, Path(shot).stem, sw // 2, y1, sw // 2, y2, Path(shot).stem)

            # OCR 优先：本地 OCR 检测目标文字（如「预约用车」），未命中/异常再走 VLM 兜底
            found = False
            if step.get("ocr_first") and self.ctx.ocr is not None and shot:
                self.ctx.incr_ocr_calls()
                try:
                    ocr_res = self.ctx.ocr.extract(shot)
                    if not ocr_res.success:
                        self.ctx.incr_ocr_failures()
                    found = ocr_res.success and ocr_res.contains(target)
                    self._log(f"  [{i}] OCR「{target}」: {'FOUND' if found else '未命中'}")
                except Exception as e:  # noqa: BLE001 - 异常转 VLM 兜底
                    self.ctx.incr_ocr_failures()
                    self.ctx.log(f"  ⚠ OCR 检测异常，转 VLM 兜底: {e}")
            if not found:
                if step.get("target_prompt"):
                    desc = self._render(step.get("target_prompt"))
                else:
                    desc = f"当前截图中是否出现「{target}」文字？只回答 YES 或 NO。"
                self.stats["vlm_calls"] += 1
                resp = self.ctx.vision.query_text(shot, desc)
                found = resp.is_affirmative
                self._log(f"  [{i}] VLM「{target}」: {'FOUND' if found else '↓'}")

            if found:
                found_stem = f"{step_id}_found" + (f"_{suffix}" if suffix else "")
                self._screenshot(found_stem)
                self._log(f"  ✓ 找到「{target}」，停止")
                self._scroll_back_to_top_if_needed(step, sw, sh)
                return

            # CAP-05 语义：未命中标记且页面无变化 → 停止
            if step.get("stop_on_stable") and prev is not None:
                from collector.quality.state_verifier import verify_screen_diff
                diff = verify_screen_diff(prev, shot)
                if diff < stable_threshold:
                    self._log(f"  [{i}] 页面无变化 (diff={diff:.4f})，停止")
                    self._scroll_back_to_top_if_needed(step, sw, sh)
                    return

            self.adb.slide(sw // 2, y1, sw // 2, y2, 300)
            self._wait(wait_after_slide, "detail_scroll")
            prev = shot

        self._log(f"  ⚠ 未找到「{target}」")
        self._scroll_back_to_top_if_needed(step, sw, sh)

    def _scroll_back_to_top_if_needed(self, step: dict, sw: int, sh: int) -> None:
        """快速回顶 3 次大段上滑（不截图不标注，纯机械操作）。"""
        if not step.get("scroll_back_to_top"):
            return
        for _ in range(3):
            self.adb.slide(sw // 2, sh // 4, sw // 2, sh * 3 // 4, 150)
            self._wait(0.1, "scroll_top_wait")
        self._log("  ↑ 回顶完成")

    def _do_input_text(self, step: dict) -> None:
        step_id = step.get("id", "?")
        value = self._render(step.get("value", ""))
        confirm_method = step.get("confirm", "key_enter")

        # 1. Screenshot + ground the input box
        shot = self._screenshot(f"{step_id}_before_input")
        cfg = step.get("ground", {})
        desc = self._render(cfg.get("element_desc", "文字输入框"))
        alts = [self._render(a) for a in cfg.get("retry_alt_descs", [])]

        center = self._vlm_ground_and_click(shot, desc, alts, f"{step_id}_input")

        if center:
            # 点击输入框后稍等聚焦
            self._wait(0.3, "short_wait")
            self.adb.clear_text()
            self._wait(0.2, "short_wait")
            self.adb.type(value)
            self._wait(0.5, "short_wait")
        else:
            self._log(f"  ⚠ 找不到输入框，尝试直接输入")
            self.adb.clear_text()
            self._wait(0.2, "short_wait")
            self.adb.type(value)
            self._wait(0.5, "short_wait")

        # 2. Confirm
        self._screenshot(f"{step_id}_after_input")

        if confirm_method == "key_search":
            # 高德搜索场景：输入完点搜索按钮（不是回车）
            self._log("  ↗ 点击搜索按钮")
            confirm_cfg = step.get("confirm_ground", {})
            confirm_desc = confirm_cfg.get("element_desc", "搜索按钮")
            confirm_alts = confirm_cfg.get("retry_alt_descs", [])
            confirm_shot = self._screenshot(f"{step_id}_confirm")
            center = self._vlm_ground_and_click(confirm_shot, confirm_desc,
                                                  confirm_alts, f"{step_id}_confirm")
            if not center:
                self._log("  ⚠ 找不到搜索按钮，回车兜底")
                self.adb.key_enter()

        elif confirm_method == "key_enter":
            self._log("  ↗ 回车确认")
            self.adb.key_enter()

        elif confirm_method == "none":
            # 输入后无需确认：不点击搜索按钮、不回车（候选列表自动出现，主场景）
            self._log("  ↻ 输入后无需确认（跳过搜索按钮/回车）")

        elif confirm_method == "tap_element":
            confirm_cfg = step.get("confirm_ground", {})
            confirm_desc = confirm_cfg.get("element_desc", "确认按钮")
            confirm_alts = confirm_cfg.get("retry_alt_descs", [])
            confirm_shot = self._screenshot(f"{step_id}_confirm")
            center = self._vlm_ground_and_click(confirm_shot, confirm_desc,
                                                  confirm_alts, f"{step_id}_confirm")
            if not center:
                self._log("  ⚠ 找不到确认按钮，尝试回车")
                self.adb.key_enter()

        self._wait(self.timing.get("after_confirm_wait", 3.0), "after_confirm_wait")

    def _do_scroll(self, step: dict) -> None:
        # 条件滚动：if_state 全部匹配才执行（如仅当当前屏无新供应商时下滑找更多）
        if_state = step.get("if_state")
        if if_state and not all(self.state.get(k) == v for k, v in if_state.items()):
            self._log(f"  ↷ 条件不满足，跳过滑动: {self._render(step.get('id', 'scroll'))}")
            return

        direction = step.get("direction", "up")
        repeat = step.get("repeat", 1)
        sw, sh = self._screen_size
        duration = step.get("duration_ms", 400)
        # 可选手势参数 from_y / to_y（0~1 屏高比例），覆盖默认手势。
        # 计价页 S0 上滑用 2/3→1/4 屏（与 FSM 一致），保证实际滚动生效。
        fy, ty = step.get("from_y"), step.get("to_y")

        def _y(default_expr: int) -> int:
            return int(float(fy) * sh) if fy is not None else default_expr

        step_id = self._render(step.get("id", "scroll"))
        # 计数器：实际执行滑动时 state[counter_key] +1（如 s4_next 的 scroll_count，
        # 用于「下滑不超过 3 次」兜底）；被 if_state 跳过则不计数。
        counter_key = step.get("counter_key")
        if self.debug_mode:
            self._screenshot(f"{step_id}_before")   # 滑动前

        last_gesture: tuple[int, int, int, int] | None = None
        for i in range(repeat):
            if direction == "up":
                x1, y1 = sw // 2, _y(sh // 2)
                x2, y2 = sw // 2, (int(float(ty) * sh) if ty is not None else sh // 4)
            elif direction == "down":
                x1, y1 = sw // 2, _y(sh // 3)
                x2, y2 = sw // 2, (int(float(ty) * sh) if ty is not None else sh * 2 // 3)
            elif direction == "half_down":
                x1, y1 = sw // 2, _y(sh * 2 // 3)
                x2, y2 = sw // 2, (int(float(ty) * sh) if ty is not None else sh * 2 // 3 - sh // 2)
            else:
                x1 = y1 = x2 = y2 = 0
            self.adb.slide(x1, y1, x2, y2, duration)
            last_gesture = (x1, y1, x2, y2)
            self._wait(step.get("after_wait", 1.0), "after_scroll")

        if counter_key:
            self.state[counter_key] = self.state.get(counter_key, 0) + 1
        if self.debug_mode:
            after = self._screenshot(f"{step_id}_after")    # 滑动后（查看滚动效果）
            if last_gesture:
                self._annotate_swipe(after, Path(after).stem, *last_gesture, Path(after).stem)
        self._log(f"  ↕ 滑动: {direction} ×{repeat}")

    def _do_wait(self, step: dict) -> None:
        secs = step.get("seconds", 1.0)
        self._log(f"  ⏳ 等待 {secs}s")
        self._wait(secs, "step_wait")

    def _do_screenshot(self, step: dict) -> None:
        self._screenshot(step.get("id", "shot"))

    # ------------------------------------------------------------------
    # 流程原语：back / extract_list / for_each / loop_until / subflow / verify
    # ------------------------------------------------------------------

    def _do_back(self, step: dict) -> None:
        """确定性返回：adb.back() + 可配置等待（PERF-02 方案一）。"""
        step_id = self._render(step.get("id", "back"))
        if self.debug_mode:
            self._screenshot(f"{step_id}_before")   # 记录返回前现场
        self.adb.back()
        self._wait(float(step.get("wait_after", 1.0)), "back_wait")

    def _do_extract_list(self, step: dict) -> None:
        """结构化提取列表（写入 engine.state）。

        - handler: 平台步骤处理器（如 gaode 的 s2_list_suppliers），
          由 handler 自行执行 VLM 并写入 state；
        - 内置模式：prompt + parse（json_array / json_dict）+ skip_keywords，
          结果写入 state[var]，可选 meta 写入 state[meta_var]。
        """
        handler = step.get("handler")
        if handler:
            fn = self._platform_step_handlers.get(handler)
            if fn is None:
                raise StepFailed(f"extract_list: 未知 handler {handler}")
            fn(self, step)
            return

        var = step.get("var", "items")
        meta_var = step.get("meta_var")
        prompt = self._render(step.get("prompt", ""))
        shot = self._screenshot(step.get("id", "extract"))
        self.stats["vlm_calls"] += 1
        resp = self.ctx.vision.query_text(shot, prompt)
        raw = resp.raw_response
        items, meta = self._parse_extract_response(raw, step)
        self.state[var] = items
        if meta_var is not None:
            self.state[meta_var] = meta
        self._log(f"  extract_list: {len(items)} 项 -> state.{var}" +
                  (f" | meta.{meta_var}={meta}" if meta_var is not None else ""))

    @staticmethod
    def _parse_extract_response(raw: str, step: dict) -> tuple[list[Any], Any]:
        """内置 extract_list 响应解析。

        parse=json_array: 顶层 JSON 数组（每项取文本）。
        parse=json_dict:  顶层 JSON 对象，items_key 取列表，meta_key 取 meta。
        均支持 skip_keywords 过滤；解析失败返回 ([], None)。
        """
        cleaned = raw.strip()
        for m in ("```json", "```"):
            if cleaned.startswith(m):
                cleaned = cleaned[len(m):].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

        skip = step.get("skip_keywords", [])
        items: list[Any] = []
        meta: Any = None
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            parsed = None

        if isinstance(parsed, list):
            items = [str(i).strip() for i in parsed if str(i).strip()]
        elif isinstance(parsed, dict):
            raw_items = parsed.get(step.get("items_key", "items"))
            if isinstance(raw_items, list):
                items = [str(i).strip() for i in raw_items if str(i).strip()]
            meta_key = step.get("meta_key")
            if meta_key:
                meta = parsed.get(meta_key)
        if skip:
            items = [i for i in items if not any(k in i for k in skip)]
        return items, meta

    def _do_for_each(self, step: dict) -> None:
        """遍历列表，每项写入 state[item]（+state[index]）后执行嵌套 steps。

        items: 可为 YAML 字面列表，或 state 变量名（如 "suppliers"）。
        max: 可选，限制最大迭代数。
        """
        raw_items = step.get("items")
        if isinstance(raw_items, str):
            items = self.state.get(raw_items, [])
        elif isinstance(raw_items, list):
            items = raw_items
        else:
            items = []
        max_items = step.get("max")
        if max_items is not None:
            items = items[: int(max_items)]
        item_var = step.get("item", "item")
        index_var = step.get("index")
        nested = step.get("steps", [])
        self._log(f"  for_each: {len(items)} 项")

        for i, item in enumerate(items):
            self.state[item_var] = item
            if index_var:
                self.state[index_var] = i
            self._log(f"  ── [{i}] {item} ──")
            self._run_steps(nested)

    def _do_loop_until(self, step: dict) -> None:
        """循环执行嵌套 steps，直到终止条件满足或达到 max_rounds。

        终止条件（三选一）：
          - until_state:   {key: value} 全部匹配（如 {done: true}）
          - until_var:     state[key] 为真
          - until_handler: 平台 handler 返回真值（如 gaode pricing_loop_done）
        """
        max_rounds = int(step.get("max_rounds", 10))
        until_state = step.get("until_state")
        until_var = step.get("until_var")
        until_handler = step.get("until_handler")
        nested = step.get("steps", [])

        for r in range(max_rounds):
            self._log(f"  ↻ 循环第 {r + 1}/{max_rounds} 轮")
            self._run_steps(nested)
            if self._loop_done(until_state, until_var, until_handler, step):
                self._log("  ✓ 循环终止条件满足，结束")
                return
        self._log(f"  ⚠ 循环达到上限 {max_rounds} 轮")

    def _loop_done(self, until_state, until_var, until_handler, step: dict) -> bool:
        if until_handler:
            fn = self._platform_step_handlers.get(until_handler)
            if fn is None:
                raise StepFailed(f"loop_until: 未知 until_handler {until_handler}")
            return bool(fn(self, step))
        if until_var:
            return bool(self.state.get(until_var))
        if until_state:
            return all(self.state.get(k) == v for k, v in until_state.items())
        return False

    def _do_subflow(self, step: dict) -> None:
        """加载子流程 YAML（file 相对当前 flow 目录解析）并内联执行。

        与主流程共享同一 ctx / stats / 截图编号，统计自动归并。
        """
        file = step.get("file", "")
        if not file:
            raise StepFailed("subflow: 缺少 file")
        path = Path(file)
        if not path.is_absolute():
            path = self._flow_dir / file
        if not path.exists():
            raise StepFailed(f"subflow: 文件不存在 {path}")

        with open(path, "r", encoding="utf-8") as f:
            sub = self._deep_resolve(self.vars, f.read())
        name = sub.get("name", path.name)
        self._log(f"── 子流程: {name} ──")
        self._run_steps(sub.get("steps", []))

    def _do_verify(self, step: dict) -> None:
        """后置校验：expect_state（状态断言）/ handler（平台校验器）/ activity_prefix（ADB 前台 Activity）。

        校验失败抛 StepFailed（可被 optional 跳过）。
        """
        if self.debug_mode:
            self._screenshot(f"{self._render(step.get('id', 'verify'))}_before")
        expect = step.get("expect_state")
        if expect:
            for k, v in expect.items():
                if self.state.get(k) != v:
                    raise StepFailed(
                        f"verify 失败: state[{k}]={self.state.get(k)!r} != {v!r}"
                    )
            self._log("  ✓ verify: 状态符合预期")
            return

        handler = step.get("handler")
        if handler:
            fn = self._platform_step_handlers.get(handler)
            if fn is None:
                raise StepFailed(f"verify: 未知 handler {handler}")
            if fn(self, step) is False:
                raise StepFailed(f"verify 失败: handler {handler}")
            self._log(f"  ✓ verify: {handler} 通过")
            return

        activity = step.get("activity_prefix")
        if activity:
            from collector.quality.state_verifier import verify_activity
            if not verify_activity(self.adb, activity):
                raise StepFailed(f"verify 失败: activity={activity}")
            self._log(f"  ✓ verify: activity {activity}")
            return

        raise StepFailed("verify: 缺少 expect_state / handler / activity_prefix")

    def _render(self, text: str) -> str:
        """运行时模板渲染：`{{.<Key>}}` → vars_，`{{.S.<key>}}` → state。"""
        if not text or "{{." not in text:
            return text

        def _replace(m):
            key = m.group(1)
            if key.startswith("S."):
                return str(self.state.get(key[2:], m.group(0)))
            return str(self.vars.get(key, m.group(0)))

        return re.sub(r"\{\{.([\w.]+)\}\}", _replace, text)

    # ------------------------------------------------------------------
    # VLM helpers
    # ------------------------------------------------------------------

    def _vlm_ground_and_click(
        self,
        image_path: str,
        desc: str,
        alt_descs: list[str] | None,
        step_tag: str,
        ref_image: str | None = None,
        ref_images: list[str] | None = None,
    ) -> tuple[int, int] | None:
        """VLM grounding → annotate → click. Returns center or None.

        Returns None when: element not found, OR element is already selected
        (when VLM says SELECTED=true with sentinel coords). Callers should
        inspect the raw response for SELECTED before calling this.
        """
        sw, sh = self._screen_size
        descriptions = [desc] + (alt_descs or [])

        for attempt, d in enumerate(descriptions):
            if not d:
                continue
            self.stats["vlm_calls"] += 1
            t0 = time.time()
            # WS-2：走 domain/vision 结构化结果（GroundingResult）
            result = self.ctx.vision.ground(image_path, d, screen_w=sw, screen_h=sh,
                                            ref_image=ref_image, ref_images=ref_images)
            elapsed = time.time() - t0

            # Check SELECTED before attempting click
            if result.selected:
                self._log(
                    f"  ✓ VLM 判断: 已选中，跳过点击 "
                    f"(attempt {attempt + 1}, {elapsed:.1f}s)"
                )
                return None

            if result.has_geometry:
                bbox = result.bbox
                cx, cy = result.center
                self._log(
                    f"  ✓ VLM bbox={bbox} center=({cx},{cy}) "
                    f"(attempt {attempt + 1}, {elapsed:.1f}s)"
                )
                self._annotate(image_path, step_tag, bbox, cx, cy, attempt)
                self.adb.click(cx, cy)
                return (cx, cy)

            self.stats["vlm_failures"] += 1

        return None

    def _vlm_ground_and_click_ref(
        self, shot, desc, alts, tag,
        ref_image=None, ref_images=None,
    ) -> tuple[int, int] | None:
        return self._vlm_ground_and_click(shot, desc, alts, tag,
                                           ref_image=ref_image, ref_images=ref_images)

    def _vlm_ground_ref(self, shot, desc, ref_image=None, ref_images=None):
        sw, sh = self._screen_size
        self.stats["vlm_calls"] += 1
        return self.ctx.vision.ground(shot, desc, screen_w=sw, screen_h=sh,
                                      ref_image=ref_image, ref_images=ref_images)

    def _resolve_ref(self, path: str | None) -> str | None:
        if not path:
            return None
        from pathlib import Path as _P
        p = _P(path)
        if p.is_absolute():
            return str(p) if p.exists() else None
        # relative to project root/assets/
        root = _P(__file__).resolve().parents[2]  # <root>
        resolved = root / "assets" / path
        if resolved.exists():
            return str(resolved)
        # legacy: also try project root directly
        legacy = root / path
        if legacy.exists():
            return str(legacy)
        return None

    def _resolve_refs(self, paths: list[str] | None) -> list[str] | None:
        if not paths:
            return None
        resolved = [r for p in paths if (r := self._resolve_ref(p))]
        return resolved or None

    def _verify_page(self, image_path: str, expected: str) -> str:
        """Classify current page via VLM."""
        self.stats["vlm_calls"] += 1
        result = self.ctx.vision.classify_page(image_path)
        page_type = result.page_type or "unknown"
        self._log(f"  📄 页面分类: {page_type}")
        return page_type

    def _try_recovery(self, current_shot: str, mismatch_rules: list[dict]) -> bool:
        """Execute recovery strategies for page mismatch."""
        for rule in mismatch_rules:
            target_page = rule.get("page", "?")
            strategies = rule.get("recovery", [])
            self._log(f"  🔄 恢复目标: {target_page}")

            for strat in strategies:
                strat_type = strat.get("strategy", "")
                self._log(f"    策略: {strat_type}")

                if strat_type == "vlm_find_and_click":
                    prompt = strat.get("prompt", "")
                    if self._vlm_ground_and_click(current_shot, prompt, [],
                                                    "recovery"):
                        self._wait(self.timing.get("after_confirm_wait", 3.0), "after_confirm_wait")
                        return True

                elif strat_type == "back_retry":
                    self.adb.back()
                    self._wait(1.5, "short_wait")

        return False

    # ------------------------------------------------------------------
    # Infrastructure
    # ------------------------------------------------------------------

    @property
    def _screen_size(self) -> tuple[int, int]:
        return self.ctx.screen_size

    def _screenshot(self, name: str, save: bool | None = None) -> str:
        """截图。save=None: debug 模式保存到 output/screenshots，collect 模式存临时目录。"""
        self._shot_seq += 1
        path = self.ctx.capture(f"{self._shot_seq:02d}_{self._render(name)}", save=save)
        if path is None:
            raise StepFailed(f"截图失败: {name}")
        # debug：每张截图都生成对应标注（帧名标签），保证标注与裸截图一致、可溯源动作
        if self.debug_mode:
            stem = Path(path).stem
            self.ctx.annotate(path, stem, lambda d, s=stem: d.text((12, 12), s, fill="red"))
        return path

    def _annotate(self, image_path, step, bbox, cx, cy, attempt=0):
        if not self.debug_mode:
            return  # 标记图仅 debug 模式输出
        stem = Path(image_path).stem
        tag = f"{stem}_attempt{attempt}" if attempt else stem

        def _draw(draw):
            draw.rectangle(bbox, outline=self._ANNO_BBOX, width=4)
            r = 20
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=self._ANNO_DOT, width=4)
            draw.line((cx - r - 10, cy, cx + r + 10, cy), fill=self._ANNO_DOT, width=3)
            draw.line((cx, cy - r - 10, cx, cy + r + 10), fill=self._ANNO_DOT, width=3)

        self.ctx.annotate(image_path, tag, _draw)

    def _annotate_swipe(self, image_path: str, tag: str,
                        x1: int, y1: int, x2: int, y2: int,
                        label: str | None = None) -> None:
        """debug：绘制滑动箭头（含帧名标签），用于溯源滑动动作。"""
        if not self.debug_mode:
            return
        color = self._ANNO_BBOX
        arrow_size = 18

        def _draw(draw):
            draw.line((x1, y1, x2, y2), fill=color, width=5)
            angle = math.atan2(y2 - y1, x2 - x1)
            ax1 = x2 - arrow_size * math.cos(angle - math.pi / 6)
            ay1 = y2 - arrow_size * math.sin(angle - math.pi / 6)
            ax2 = x2 - arrow_size * math.cos(angle + math.pi / 6)
            ay2 = y2 - arrow_size * math.sin(angle + math.pi / 6)
            draw.polygon([(x2, y2), (ax1, ay1), (ax2, ay2)], fill=color)
            draw.ellipse((x1 - 6, y1 - 6, x1 + 6, y1 + 6), fill=color)
            if label:
                draw.text((12, 12), label, fill="red")

        self.ctx.annotate(image_path, tag, _draw)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[Flow] {msg}")

    @staticmethod
    def _template(text: str, vars_: dict[str, str]) -> str:
        """Replace {{.VarName}} placeholders with values from vars_."""
        if "{{." not in text:
            return text
        # Simple {{.Key}} substitution
        def _replace(m):
            key = m.group(1)
            return vars_.get(key, m.group(0))
        return re.sub(r'\{\{\.(\w+)\}\}', _replace, text)

    @staticmethod
    def _deep_resolve(vars_: dict[str, str], yaml_text: str) -> dict:
        """Apply template substitution throughout the YAML text."""
        if "{{." not in yaml_text:
            import yaml as _yaml
            return _yaml.safe_load(yaml_text)
        resolved = FlowEngine._template(yaml_text, vars_)
        import yaml as _yaml
        return _yaml.safe_load(resolved)


class StepFailed(Exception):
    """Raised when a mandatory step cannot complete."""
    pass
