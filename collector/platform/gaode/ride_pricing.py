"""
计价采集 FSM — 全选经济 + 逐个供应商采集计价规则
============================================================================

截图命名规范:
  p01, p02, p03 ...  顶层步骤
  p05_01, p05_02 ... 嵌套子步骤（如详细计价页内滚动）

每个动作都有截图 + 标注 (Mobile-Agent-v3.5 风格).
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from collector.infrastructure.device.adb_utils import AdbTools
from collector.infrastructure.vision.vlm_grounder import VLMGrounder

_REF_QUESTION_ICON = "assets/button_to_price.png"
_SKIP_SUPPLIERS = {"出租车", "优享"}


class RidePricingFSM:
    """经济型打车计价采集."""

    _ANNO_CLICK_FILL = "red"
    _ANNO_SWIPE_COLOR = "red"

    def __init__(
        self,
        adb: AdbTools,
        grounder: VLMGrounder,
        supplier: str,
        profile_cfg: dict[str, Any],
        output_dir: str | None = None,
        verbose: bool = True,
        mode: str = "debug",
    ):
        self.adb = adb
        self.grounder = grounder
        self.supplier = supplier
        self.verbose = verbose
        self.output_dir = Path(output_dir or "./output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 输出模式: debug(每步截图+标记图) / collect(进入打车页后开始保存截图)
        self.mode = mode if mode in ("debug", "collect") else "debug"
        self.steps_cfg = profile_cfg.get("steps", {})
        self.timing = profile_cfg.get("timing", {})
        self.collection_cfg = profile_cfg.get("collection", {})
        self._top_seq = 0       # p01, p02, ...
        self._sub_seq = 0       # _01, _02, ... (reset per sub-step)
        self.stats = {"vlm_calls": 0, "vlm_failures": 0,
                      "api_seconds": 0.0, "wait_seconds": 0.0, "elapsed": 0.0}
        self._wait_total = 0.0  # 累计流程等待时长

    @property
    def debug_mode(self) -> bool:
        return self.mode == "debug"

    # ------------------------------------------------------------------
    # 耗时统计
    # ------------------------------------------------------------------

    def _wait(self, seconds: float, tag: str = "") -> None:
        """带统计的等待：累加流程设定的等待时长。"""
        if seconds and seconds > 0:
            time.sleep(seconds)
            self._wait_total += float(seconds)

    def _api_seconds(self) -> float:
        """当前累计 API 耗时（对 mock 兼容返回 0.0）。"""
        v = getattr(self.grounder, "api_seconds", None)
        return v if isinstance(v, (int, float)) else 0.0

    def _log_step_timing(self, label: str, t0: float, api0: float, wait0: float) -> None:
        """输出某一步骤/阶段的耗时统计。"""
        self._log(
            f"  ⏱ {label}: 步骤 {time.time() - t0:.1f}s | "
            f"API {self._api_seconds() - api0:.1f}s | 等待 {self._wait_total - wait0:.1f}s"
        )

    # ==================================================================
    # Screenshot helpers with nested numbering
    # ==================================================================

    def _shot(self, name: str) -> str:
        """顶级截图: p01_xxx, p02_xxx ..."""
        self._top_seq += 1
        return self._save(f"p{self._top_seq:02d}_{name}")

    def _sub_shot(self, name: str) -> str:
        """子步骤截图: 调用前 _sub_seq 已由外部递增."""
        return self._save(f"p{self._top_seq:02d}_{self._sub_seq:02d}_{name}")

    def _save(self, stem: str) -> str:
        """保存截图。collect 模式：计价采集阶段(进入打车页后)全部保存，含打车页滑动与详细计价页。"""
        path = str(self.output_dir / f"{stem}.jpg")
        self._ensure_screenshot(path)
        return path

    # ==================================================================
    # Public API
    # ==================================================================

    def run(self) -> list[str]:
        self._log(f"══ 计价采集: {self.supplier} ══")
        self._top_seq = 0
        screenshots: list[str] = []
        target_count = self.collection_cfg.get("max_suppliers", 2)
        run_t0 = time.time()
        api_t0 = self._api_seconds()
        wait_t0 = self._wait_total

        # ── 刚进打车页（collect 模式从此开始保存截图）──
        screenshots.append(self._shot("ride_page_entry"))

        # ── S0 + S1 ──
        t0, a0, w0 = time.time(), self._api_seconds(), self._wait_total
        self._s0_scroll_up()
        self._log_step_timing("S0 上滑", t0, a0, w0)

        t0, a0, w0 = time.time(), self._api_seconds(), self._wait_total
        self._s1_tap_select_all()
        self._log_step_timing("S1 全选经济", t0, a0, w0)
        screenshots.append(self._shot("after_select_all"))

        # ── 循环 ──
        collected: list[str] = []
        attempted: set[str] = set()
        scroll_round = 0
        max_rounds = self.collection_cfg.get("max_scroll_rounds", 8)

        while len(collected) < target_count and scroll_round < max_rounds:
            candidates = self._s2_list_suppliers()
            new_suppliers = [s for s in candidates if s not in attempted]

            if not new_suppliers:
                self._log("  当前屏无新供应商")
                self._swipe_down("s4_nomore")
                self._wait(self.collection_cfg.get("after_scroll_wait", 1.0), "after_scroll_wait")
                scroll_round += 1
                candidates = self._s2_list_suppliers()
                new_suppliers = [s for s in candidates if s not in attempted]
                if not new_suppliers:
                    self._log("  经济型已采完，退出")
                    break

            self._log(
                f"── 屏 {scroll_round + 1}: {len(candidates)} 候选, "
                f"{len(new_suppliers)} 新 (已采 {len(collected)}/{target_count})"
            )

            for supp_name in new_suppliers:
                if len(collected) >= target_count:
                    break
                attempted.add(supp_name)
                idx = len(collected) + 1
                self._log(f"  [{idx}/{target_count}] 「{supp_name}」→ 开始采集")

                t0, a0, w0 = time.time(), self._api_seconds(), self._wait_total
                ok = self._s3a_tap_question(supp_name)
                if not ok:
                    self._log(f"    ✗ 「{supp_name}」找不到问号，跳过")
                    # TODO: 页面感知 — 用素材库判断当前是否在打车页
                    # 如果不在打车页（例如弹窗/跳转），执行恢复流程
                    continue

                screenshots.append(self._shot(f"popup_{supp_name}"))
                self._s3c_collect_detail_rules(supp_name)
                self._log_step_timing(f"S3 「{supp_name}」", t0, a0, w0)
                collected.append(supp_name)
                self._log(f"    ✓ 「{supp_name}」完成 ({len(collected)}/{target_count})")

            if len(collected) >= target_count:
                break

            self._log("  ↓ 下滑 1/3 屏")
            self._swipe_down("s4_next")
            self._wait(self.collection_cfg.get("after_scroll_wait", 1.0), "after_scroll_wait")
            scroll_round += 1

        self.stats["elapsed"] = time.time() - run_t0
        self.stats["api_seconds"] = self._api_seconds() - api_t0
        self.stats["wait_seconds"] = self._wait_total - wait_t0
        self._log("─" * 50)
        self._log(f"完成: {len(collected)} 个 → {', '.join(collected)}")
        self._log(f"VLM: {self.stats['vlm_calls']} 次")
        self._log(
            f"⏱ 计价采集总耗时 {self.stats['elapsed']:.1f}s | "
            f"API {self.stats['api_seconds']:.1f}s | 等待 {self.stats['wait_seconds']:.1f}s"
        )
        screenshots.append(self._shot("all_done"))

        shot_home = self._shot("before_home")
        self._annotate_action_label(shot_home, "home", "Press HOME")
        self.adb.home()
        self._wait(0.5, "short_wait")
        return screenshots

    # ==================================================================
    # S0
    # ==================================================================

    def _s0_scroll_up(self) -> None:
        self._log("S0: 上滑拉出内容")
        shot = self._shot("s0_before_scroll")
        sw, sh = self._screen_size
        x1, y1 = sw // 2, sh * 2 // 3
        x2, y2 = sw // 2, sh // 4
        self._annotate_swipe(shot, "s0_scroll_up", x1, y1, x2, y2)
        self.adb.slide(x1, y1, x2, y2, self.collection_cfg.get("swipe_duration_ms", 400))
        self._wait(self.collection_cfg.get("after_scroll_wait", 1.0), "after_scroll_wait")

    # ==================================================================
    # S1
    # ==================================================================

    def _s1_tap_select_all(self) -> None:
        """S1: 幂等确保「全选经济」已勾选（SEL-01 目标锚定）。

        定位「全选经济」文字 → 定位右侧同一行主勾选框 → 裁剪 ROI 分类 →
        未勾选才点击 → 重新截图 → 重新验证同一勾选框为 CHECKED。
        """
        from collector.platform.gaode.select_all import SelectAllError, ensure_all_selected

        self._log("S1: 确保全选经济已勾选（目标锚定）")
        try:
            result = ensure_all_selected(
                adb=self.adb,
                grounder=self.grounder,
                label="全选经济",
                screen_size=self._screen_size,
                expected_region=self.collection_cfg.get("select_all_region"),
                screenshot=self._shot,
                stats=self.stats,
                verbose=self.verbose,
                wait_after_click=self.timing.get("after_tap_wait", 2.0),
            )
        except SelectAllError as e:
            # 无法证明状态正确 → 停止并保留现场（codex.md §2.2）
            self._log(f"  ✗ 全选经济失败: {e}")
            raise
        self._log(f"  ✓ 全选经济: {result.state} @ {result.checkbox_bbox}")

    # ==================================================================
    # S2
    # ==================================================================

    def _s2_list_suppliers(self) -> list[str]:
        self._log("S2: 识别经济型供应商")
        shot = self._shot("s2_suppliers")
        desc = (
            "高德打车页面。"
            "以 JSON 数组列出「经济型」分组标题**下方**所有供应商名称。"
            "只列经济型下面的行，且有「预估」和「?」问号的。"
            "排除「出租车」「优享」。"
            '格式: ["快车", "特惠快车"] 无则 []。'
        )
        self.stats["vlm_calls"] += 1
        result = self.grounder.query_text(shot, desc)
        raw = result.get("raw_response", "").strip()
        self._log(f"  VLM: {raw[:300]}")

        suppliers: list[str] = []
        try:
            cleaned = raw
            for m in ("```json", "```"):
                if cleaned.startswith(m):
                    cleaned = cleaned[len(m):].strip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                for n in parsed:
                    n = str(n).strip()
                    if n and not any(kw in n for kw in _SKIP_SUPPLIERS):
                        if n not in suppliers:
                            suppliers.append(n)
                return suppliers
        except (json.JSONDecodeError, ValueError):
            pass

        for line in raw.split("\n"):
            line = re.sub(r'^[\d\.\、\)）\-\s]+', '', line.strip())
            line = line.strip().strip('"').strip("'").strip(",")
            if line and len(line) <= 30 and not any(kw in line for kw in _SKIP_SUPPLIERS):
                if len(line) >= 2 and line not in suppliers:
                    suppliers.append(line)
        return suppliers

    # ==================================================================
    # S3a
    # ==================================================================

    def _s3a_tap_question(self, supplier: str) -> bool:
        self._log(f"    S3a: 点「{supplier}」问号")
        shot = self._shot(f"q_{supplier}")
        sw, sh = self._screen_size
        ref_path = str(Path(__file__).resolve().parents[3] / _REF_QUESTION_ICON)

        desc = f"找「{supplier}」行「预估」前面的 '?' 问号。附件是参考图。返回 bbox 和中心坐标。"
        self.stats["vlm_calls"] += 1
        result = self.grounder.ground(shot, desc, screen_w=sw, screen_h=sh,
                                       ref_image=ref_path if Path(ref_path).exists() else None)

        cx, cy = self._extract_center(result)
        if cx is None:
            self._log(f"    ⚠ 未找到「{supplier}」的问号")
            return False

        self._log(f"    问号 at ({cx},{cy})")
        self._annotate_click(shot, f"q_{supplier}", result.get("bbox"), cx, cy)
        self.adb.click(cx, cy)
        self._wait(self.collection_cfg.get("pricing_page_wait", 2.0), "pricing_page_wait")
        return True

    # ==================================================================
    # S3c: 详细计价规则
    # ==================================================================

    def _s3c_collect_detail_rules(self, supplier: str) -> bool:
        self._log(f"    S3c: 进入「{supplier}」详细计价规则")
        shot = self._shot(f"detail_entry_{supplier}")
        sw, sh = self._screen_size

        # 1. 点击「查看详细计价规则」
        desc = "高德打车计价弹窗。找「查看详细计价规则」或「计价规则」入口（弹窗中下部的文字链接/按钮）。返回 bbox 和中心坐标。"
        self.stats["vlm_calls"] += 1
        result = self.grounder.ground(shot, desc, screen_w=sw, screen_h=sh)

        if result.get("found") and result.get("center"):
            cx, cy = result["center"]
            self._annotate_click(shot, f"detail_btn_{supplier}", result.get("bbox"), cx, cy)
            self.adb.click(cx, cy)
            self._wait(self.timing.get("after_confirm_wait", 2.0), "after_confirm_wait")
        else:
            self._log("    ⚠ 未找到详细计价规则入口")
            return False

        self._shot(f"detail_page_{supplier}")

        # ── 子步骤编号器 ──
        self._sub_seq = 0

        # 2. 工作日
        self._log("      工作日 → 下滑到底 → 回顶")
        self._tap_tab_and_scroll("工作日", supplier)

        # 3. 休息日
        self._log("      休息日 → 下滑到底")
        self._tap_tab_and_scroll("休息日", supplier)

        # 4. 返回 ×2: 详细计价页 → 计价弹窗 → 打车页
        self._log("      返回 → 计价弹窗")
        self._tap_back_arrow(f"detail_exit_to_popup_{supplier}")
        self._log("      返回 → 打车页")
        self._tap_back_arrow(f"detail_exit_to_ride_{supplier}")
        return True

    def _tap_tab_and_scroll(self, tab_label: str, supplier: str) -> None:
        sw, sh = self._screen_size
        shot = self._shot(f"detail_before_{tab_label}_{supplier}")
        desc = f"计价规则详情页。找「{tab_label}」标签页（与另一标签并列）。返回 bbox 和中心坐标。"
        self.stats["vlm_calls"] += 1
        result = self.grounder.ground(shot, desc, screen_w=sw, screen_h=sh)
        if result.get("found") and result.get("center"):
            cx, cy = result["center"]
            self._annotate_click(shot, f"tab_{tab_label}_{supplier}", result.get("bbox"), cx, cy)
            self.adb.click(cx, cy)
            self._wait(1.0, "tab_wait")

        self._scroll_to_bottom(tab_label, supplier)
        self._sub_seq += 1
        self._sub_shot(f"{tab_label}_bottom_{supplier}")

        if tab_label == "工作日":
            self._scroll_to_top(tab_label, supplier)

    # ==================================================================
    # 原子动作
    # ==================================================================

    def _tap_back_arrow(self, tag: str) -> None:
        shot = self._shot(tag)
        sw, sh = self._screen_size
        desc = "页面左上角返回箭头'<'图标"
        self.stats["vlm_calls"] += 1
        result = self.grounder.ground(shot, desc, screen_w=sw, screen_h=sh)
        if result.get("found") and result.get("center"):
            cx, cy = result["center"]
            self._annotate_click(shot, tag, result.get("bbox"), cx, cy)
            self.adb.click(cx, cy)
            self._wait(self.timing.get("after_tap_wait", 2.0), "after_tap_wait")
        else:
            self._annotate_action_label(shot, tag, "BACK key")
            self.adb.back()
            self._wait(1.5, "back_wait")

    def _swipe_down(self, tag: str) -> None:
        """下滑 1/3 屏."""
        shot = self._shot(tag)
        sw, sh = self._screen_size
        amount = sh // 3
        cx = sw // 2
        y1 = sh * 2 // 3
        y2 = y1 - amount
        self._log(f"  ↕ ({cx},{y1})→({cx},{y2}) [⅓屏]")
        self._annotate_swipe(shot, tag, cx, y1, cx, y2)
        self.adb.slide(cx, y1, cx, y2, self.collection_cfg.get("scroll_duration_ms", 500))

    def _scroll_to_bottom(self, tab: str, supplier: str) -> None:
        """每步截图+标注 → 下滑 1/3 屏 → 每 3 步 VLM 判断到底."""
        sw, sh = self._screen_size
        amount = sh // 3
        y1 = sh * 3 // 4
        y2 = y1 - amount
        max_swipes = 12

        for i in range(max_swipes):
            shot = self._shot(f"{tab}_scroll_{i}_{supplier}")
            self._annotate_swipe(shot, f"{tab}_scroll_{i}_{supplier}", sw // 2, y1, sw // 2, y2)
            self.adb.slide(sw // 2, y1, sw // 2, y2, 300)
            self._wait(0.5, "short_wait")

            if i >= 2 and (i % 3 == 0 or i == max_swipes - 1):
                check = self._shot(f"{tab}_check_{i}_{supplier}")
                desc = "判断当前是否已到页面最底部。只输出 YES 或 NO。"
                self.stats["vlm_calls"] += 1
                resp = self.grounder.query_text(check, desc)
                raw = resp.get("raw_response", "").strip().upper()
                self._log(f"      {tab}[{i}]: {'BOTTOM' if 'YES' in raw else '↓'}")
                if "YES" in raw:
                    break

    def _scroll_to_top(self, tab: str, supplier: str) -> None:
        """快速回顶 3 次大段上滑，不截图不标注（纯机械操作）."""
        sw, sh = self._screen_size
        for i in range(3):
            self.adb.slide(sw // 2, sh // 4, sw // 2, sh * 3 // 4, 150)
            self._wait(0.2, "short_wait")
        self._log(f"      {tab} 回顶完成")

    # ==================================================================
    # 标注
    # ==================================================================

    def _extract_center(self, result: dict) -> tuple[int, int] | None:
        bbox = result.get("bbox")
        center = result.get("center")
        if bbox and bbox != [0, 0, 0, 0] and center:
            return center[0], center[1]
        if center and center != [0, 0]:
            return center
        return None

    def _tag(self, image_path: str, fallback: str) -> str:
        """Derive annotation tag from screenshot path stem.
        e.g. output/p01_s0_before_scroll.jpg → p01_s0_before_scroll"""
        return Path(image_path).stem

    def _annotate_click(self, image_path: str, tag: str,
                        bbox: list[int] | None, cx: int, cy: int) -> None:
        self._do_annotate(image_path, self._tag(image_path, tag), lambda draw: (
            draw.rectangle(bbox, outline="red", width=4) if (bbox and bbox != [0,0,0,0]) else None,
            draw.ellipse((cx-15, cy-15, cx+15, cy+15), fill=self._ANNO_CLICK_FILL,
                         outline=self._ANNO_CLICK_FILL),
        ))

    def _annotate_swipe(self, image_path: str, tag: str,
                        x1: int, y1: int, x2: int, y2: int) -> None:
        def _draw(draw):
            color = self._ANNO_SWIPE_COLOR
            arrow_size = 15
            draw.line((x1, y1, x2, y2), fill=color, width=4)
            angle = math.atan2(y2 - y1, x2 - x1)
            ax1 = x2 - arrow_size * math.cos(angle - math.pi / 6)
            ay1 = y2 - arrow_size * math.sin(angle - math.pi / 6)
            ax2 = x2 - arrow_size * math.cos(angle + math.pi / 6)
            ay2 = y2 - arrow_size * math.sin(angle + math.pi / 6)
            draw.polygon([(x2, y2), (ax1, ay1), (ax2, ay2)], fill=color)
            draw.ellipse((x1-6, y1-6, x1+6, y1+6), fill=color)
        self._do_annotate(image_path, self._tag(image_path, tag), _draw)

    def _annotate_action_label(self, image_path: str, tag: str, label: str) -> None:
        def _draw(draw):
            sw2, sh2 = Image.open(image_path).size
            draw.text((sw2 // 2 - 100, sh2 // 2 - 20), label, fill="red")
        self._do_annotate(image_path, self._tag(image_path, tag), _draw)

    def _do_annotate(self, image_path: str, tag: str, fn) -> None:
        if not self.debug_mode:
            return  # 标记图仅 debug 模式输出
        anno_dir = self.output_dir / "_annotations"
        anno_dir.mkdir(parents=True, exist_ok=True)
        try:
            img = Image.open(image_path)
            if img.mode in ("RGBA", "P", "LA", "PA"):
                img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            fn(draw)
            out = anno_dir / f"{tag}.png"
            img.save(out, "PNG")
            self._log(f"  📸 {out}")
        except Exception as e:
            self._log(f"  ⚠ 标注失败: {e}")

    # ==================================================================
    # Infrastructure
    # ==================================================================

    @property
    def _screen_size(self) -> tuple[int, int]:
        sz = self.adb.screen_size
        if sz is not None:
            return sz
        raise RuntimeError("无法获取屏幕尺寸")

    def _ensure_screenshot(self, path: str) -> bool:
        for attempt in range(3):
            if self.adb.get_screenshot(path):
                return True
            self._log(f"  ⚠ 截图重试 {attempt+1}/3")
            self._wait(0.5, "short_wait")
        return False

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[Pricing] {msg}")
