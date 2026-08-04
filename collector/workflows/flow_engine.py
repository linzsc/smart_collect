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
import re
import shutil
import tempfile
import time
from pathlib import Path
from string import Template
from typing import Any

from PIL import Image, ImageDraw

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
    ):
        self.adb = adb
        self.grounder = grounder
        self.vars = vars_ or {}
        self.verbose = verbose
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.profile_cfg = profile_cfg or {}
        # 平台特有步骤（如 pricing_collect）由 Platform.step_handlers 注入，
        # 通用引擎不直接依赖任何平台模块。
        self._platform_step_handlers = platform_step_handlers or {}
        # 输出模式: debug(每步截图+标记图) / collect(仅保存详细计价页截图)
        self.mode = mode if mode in ("debug", "collect") else "debug"
        self._scratch_dir: Path | None = None

        with open(flow_path, "r", encoding="utf-8") as f:
            self.flow = self._deep_resolve(self.vars, f.read())

        self.timing = self.flow.get("timing", {})
        self.package = self.flow.get("package", "")
        self._shot_seq = 0
        self.stats = {"vlm_calls": 0, "vlm_failures": 0, "steps_executed": 0,
              "api_seconds": 0.0, "wait_seconds": 0.0, "elapsed": 0.0}
        self._wait_total = 0.0  # 累计流程等待时长

    # ------------------------------------------------------------------
    # 输出模式
    # ------------------------------------------------------------------

    @property
    def debug_mode(self) -> bool:
        """debug 模式：每步截图 + 标记图。"""
        return self.mode == "debug"

    @property
    def scratch_dir(self) -> Path | None:
        """collect 模式下的临时截图目录（供 VLM 定位，不计入最终输出）。"""
        if self.debug_mode:
            return None
        if self._scratch_dir is None:
            self._scratch_dir = Path(tempfile.mkdtemp(prefix="collector_scratch_"))
        return self._scratch_dir

    def cleanup(self) -> None:
        """删除 collect 模式的临时截图目录。"""
        if self._scratch_dir is not None:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_dir = None

    # ------------------------------------------------------------------
    # 耗时统计
    # ------------------------------------------------------------------

    def _wait(self, seconds: float, tag: str = "") -> None:
        """带统计的等待：累加流程设定的等待时长。"""
        if seconds and seconds > 0:
            time.sleep(seconds)
            self._wait_total += float(seconds)

    def add_wait(self, seconds: float) -> None:
        """合并子流程（如平台 handler）产生的等待时长，用于总耗时归因。"""
        self._wait_total += float(seconds or 0.0)

    def _api_seconds(self) -> float:
        """当前累计 API 耗时（对 mock 兼容返回 0.0）。"""
        v = getattr(self.grounder, "api_seconds", None)
        return v if isinstance(v, (int, float)) else 0.0

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

        for step in self.flow.get("steps", []):
            self.stats["steps_executed"] += 1
            step_id = step.get("id", "?")
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
                elif step_type == "screenshot":
                    self._do_screenshot(step)
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

        self.stats["elapsed"] = time.time() - run_t0
        self.stats["api_seconds"] = self._api_seconds() - api_t0
        self.stats["wait_seconds"] = self._wait_total - wait_t0
        self._log(f"\n── {name} 完成 ──")
        self._log(f"步骤: {self.stats['steps_executed']}, VLM: {self.stats['vlm_calls']} 次")
        self._log(
            f"⏱ 总耗时 {self.stats['elapsed']:.1f}s | "
            f"API {self.stats['api_seconds']:.1f}s | 等待 {self.stats['wait_seconds']:.1f}s"
        )

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    def _do_open_app(self, step: dict) -> None:
        pkg = step.get("package", self.package)
        self.adb.open_app(pkg)
        self._wait(self.timing.get("app_launch_wait", 3.0), "app_launch_wait")

    def _do_ground_click(self, step: dict) -> None:
        shot = self._screenshot(step.get("id", "step"))

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

        # ── grounding ──
        cfg = step.get("ground", {})
        desc = cfg.get("element_desc", "")
        alts = cfg.get("retry_alt_descs", [])

        center = self._vlm_ground_and_click(shot, desc, alts, step.get("id", "?"))

        if center:
            self._wait(self.timing.get("after_tap_wait", 2.0), "after_tap_wait")
            return

        # ── fallback ──
        fallback = step.get("fallback")
        if fallback:
            self._log("  🔄 兜底查找…")
            center = self._vlm_ground_and_click(shot, fallback.get("prompt", ""),
                                                 [], f"{step.get('id','?')}_fallback")
            if center:
                self._wait(self.timing.get("after_tap_wait", 2.0), "after_tap_wait")
                return

        if step.get("mandatory"):
            raise StepFailed(f"mandatory ground_click '{step.get('id')}' failed")
        self._log(f"  ⚠ 未找到目标元素")

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

        if result1.get("selected"):
            self._log("  ✓ 首轮判断已选中，跳过点击")
            return

        if result1.get("found") and result1.get("center"):
            bbox = result1.get("bbox", [0, 0, 0, 0])
            cx, cy = result1["center"]
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

        if result2.get("selected"):
            self._log("  ✓ Double check 已确认选中")
            return

        # 第二轮未选中 → 再 click 一次
        self._log("  ⚠ Double check 未选中，再次点击")
        if result2.get("found") and result2.get("center"):
            bbox2 = result2.get("bbox", [0, 0, 0, 0])
            cx2, cy2 = result2["center"]
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
        """小步下滑直到 VLM 在截图中发现指定文字."""
        step_id = step.get("id", "?")
        target = step.get("target_text", "")
        max_swipes = step.get("max_swipes", 15)
        sw, sh = self._screen_size
        amount = sh // 3
        y1, y2 = sh * 3 // 4, sh * 3 // 4 - amount

        for i in range(max_swipes):
            shot = self._screenshot(f"{step_id}_scroll_{i}")
            desc = f"当前截图中是否出现「{target}」文字？只回答 YES 或 NO。"
            self.stats["vlm_calls"] += 1
            resp = self.grounder.query_text(shot, desc)
            raw = resp.get("raw_response", "").strip().upper()
            self._log(f"  [{i}] 找「{target}」: {'FOUND' if 'YES' in raw else '↓'}")

            if "YES" in raw:
                self._screenshot(f"{step_id}_found")
                self._log(f"  ✓ 找到「{target}」，停止")
                return

            self.adb.slide(sw // 2, y1, sw // 2, y2, 300)
            self._wait(0.5, "short_wait")

        self._log(f"  ⚠ 未找到「{target}」")

    def _do_input_text(self, step: dict) -> None:
        step_id = step.get("id", "?")
        value = step.get("value", "")
        confirm_method = step.get("confirm", "key_enter")

        # 1. Screenshot + ground the input box
        shot = self._screenshot(f"{step_id}_before_input")
        cfg = step.get("ground", {})
        desc = cfg.get("element_desc", "文字输入框")
        alts = cfg.get("retry_alt_descs", [])

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
        direction = step.get("direction", "up")
        repeat = step.get("repeat", 1)
        sw, sh = self._screen_size
        duration = step.get("duration_ms", 400)

        for i in range(repeat):
            if direction == "up":
                self.adb.slide(sw // 2, sh // 2, sw // 2, sh // 4, duration)
            elif direction == "down":
                self.adb.slide(sw // 2, sh // 3, sw // 2, sh * 2 // 3, duration)
            elif direction == "half_down":
                self.adb.slide(sw // 2, sh * 2 // 3, sw // 2, sh * 2 // 3 - sh // 2, duration)

            self._wait(step.get("after_wait", 1.0), "after_scroll")

        self._log(f"  ↕ 滑动: {direction} ×{repeat}")

    def _do_wait(self, step: dict) -> None:
        secs = step.get("seconds", 1.0)
        self._log(f"  ⏳ 等待 {secs}s")
        self._wait(secs, "step_wait")

    def _do_screenshot(self, step: dict) -> None:
        self._screenshot(step.get("id", "shot"))

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
            result = self.grounder.ground(image_path, d, screen_w=sw, screen_h=sh,
                                           ref_image=ref_image, ref_images=ref_images)
            elapsed = time.time() - t0

            # Check SELECTED before attempting click
            if result.get("selected"):
                self._log(
                    f"  ✓ VLM 判断: 已选中，跳过点击 "
                    f"(attempt {attempt + 1}, {elapsed:.1f}s)"
                )
                return None

            if result.get("found"):
                bbox = result.get("bbox", [0, 0, 0, 0])
                center = result.get("center", [0, 0])
                if bbox and bbox != [0, 0, 0, 0] and center:
                    cx, cy = center[0], center[1]
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

    def _vlm_ground_ref(self, shot, desc, ref_image=None, ref_images=None) -> dict:
        sw, sh = self._screen_size
        self.stats["vlm_calls"] += 1
        return self.grounder.ground(shot, desc, screen_w=sw, screen_h=sh,
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
        result = self.grounder.classify_page(image_path)
        page_type = result.get("page_type", "unknown")
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
        sz = self.adb.screen_size
        if sz is not None:
            return sz
        raise RuntimeError("无法获取屏幕尺寸")

    def _screenshot(self, name: str, save: bool | None = None) -> str:
        """截图。save=None: debug 模式保存到 output/screenshots，collect 模式存临时目录。"""
        self._shot_seq += 1
        if save is None:
            save = self.debug_mode
        if save:
            target = self.output_dir / "screenshots"  # 裸截图子文件夹
            target.mkdir(parents=True, exist_ok=True)
        else:
            target = self.scratch_dir or self.output_dir
        path = str(target / f"{self._shot_seq:02d}_{name}.jpg")
        for attempt in range(3):
            if self.adb.get_screenshot(path):
                return path
            self._log(f"  ⚠ 截图失败，重试 {attempt + 1}/3")
            self._wait(0.5, "short_wait")
        raise StepFailed(f"截图失败: {name}")

    def _annotate(self, image_path, step, bbox, cx, cy, attempt=0):
        if not self.debug_mode:
            return  # 标记图仅 debug 模式输出
        anno_dir = self.output_dir / "annotations"  # 标记图子文件夹
        anno_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(image_path).stem
        tag = f"{stem}_attempt{attempt}" if attempt else stem
        try:
            img = Image.open(image_path)
            if img.mode in ("RGBA", "P", "LA", "PA"):
                img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            draw.rectangle(bbox, outline=self._ANNO_BBOX, width=4)
            r = 20
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=self._ANNO_DOT, width=4)
            draw.line((cx - r - 10, cy, cx + r + 10, cy), fill=self._ANNO_DOT, width=3)
            draw.line((cx, cy - r - 10, cx, cy + r + 10), fill=self._ANNO_DOT, width=3)
            img.save(anno_dir / f"{tag}.png", "PNG")
            self._log(f"  📸 标注: {anno_dir / tag}.png")
        except Exception as e:
            self._log(f"  ⚠ 标注失败: {e}")

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
