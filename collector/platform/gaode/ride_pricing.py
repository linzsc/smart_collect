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

from PIL import Image, ImageChops, ImageDraw

from collector.infrastructure.device.adb_utils import AdbTools
from collector.infrastructure.vision.vlm_grounder import VLMGrounder

_REF_QUESTION_ICON = "assets/button_to_price.png"
_SKIP_SUPPLIERS = {"出租车", "优享"}          # 精确匹配（兼容旧引用）
_SKIP_KEYWORDS = ("快车", "拼车", "的士", "出租", "优享")      # 关键词：快车/拼车/出租车/的士/优享类一律不采集（CAP-08/09）
# 详细计价页终点标记：出现蓝色「预约用车」即停止滚动（CAP-01）
_END_MARKER = "预约用车"


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
        self._tab_coords: dict[str, tuple[int, int]] = {}  # 标签坐标缓存（PERF-03）

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
        """保存裸截图到 output/screenshots/（collect 模式：计价采集阶段全部保存）。"""
        shot_dir = self.output_dir / "screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        path = str(shot_dir / f"{stem}.jpg")
        self._ensure_screenshot(path)
        return path

    # ==================================================================
    # Public API
    # ==================================================================

    def run(self) -> list[str]:
        self._log(f"══ 计价采集: {self.supplier} ══")
        self._top_seq = 0
        screenshots: list[str] = []
        target_count = self.collection_cfg.get("max_suppliers", 10)
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

        # ── 循环：逐个采集「全选经济」下的运力商（CAP-06）──
        collected = self._collect_suppliers(target_count, screenshots)

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

    def _collect_suppliers(self, target_count: int, screenshots: list[str]) -> list[str]:
        """逐个采集「经济型」栏下的运力商（CAP-06/09）。

        流程：
          1. S2 识别当前屏「经济型」栏（两道灰线之间）的运力商列表 + economy_ended；
          2. 逐个采集列表中未采集过的运力商（S3a 点问号 → S3c 详细计价页）；
          3. 列表最后一个采完且数量仍 < target_count → 下滑打车页查看新的运力商；
          4. 终止：达到 target_count（默认 10），或经济型栏结束
             （截图出现「特快车/特惠快车」「出租车」「优享型」，其下运力商不再采集）。
        """
        collected: list[str] = []
        attempted: set[str] = set()
        scroll_round = 0
        max_rounds = self.collection_cfg.get("max_scroll_rounds", 8)

        while len(collected) < target_count and scroll_round < max_rounds:
            # S2: 识别当前屏经济型供应商列表 + 栏是否结束
            candidates, economy_ended = self._s2_list_suppliers()
            new_suppliers = [s for s in candidates if s not in attempted]

            if not new_suppliers:
                if economy_ended:
                    self._log("  经济型栏已结束且无新运力商，停止")
                    break
                # 当前列表没有新运力商 → 下滑一屏确认是否还有
                self._log("  当前屏无新运力商，下滑确认")
                self._swipe_down("s4_nomore")
                self._wait(self.collection_cfg.get("after_scroll_wait", 1.0), "after_scroll_wait")
                scroll_round += 1
                candidates, economy_ended = self._s2_list_suppliers()
                new_suppliers = [s for s in candidates if s not in attempted]
                if not new_suppliers:
                    self._log("  经济型已采完，退出")
                    break

            self._log(
                f"── 屏 {scroll_round + 1}: 列表 {len(candidates)} 个, "
                f"新 {len(new_suppliers)} 个 (已采 {len(collected)}/{target_count})"
            )

            # 逐个采集当前列表中的新运力商
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
                if self._s3c_collect_detail_rules(supp_name):
                    self._log_step_timing(f"S3 「{supp_name}」", t0, a0, w0)
                    collected.append(supp_name)
                    self._log(f"    ✓ 「{supp_name}」完成 ({len(collected)}/{target_count})")
                else:
                    self._log(f"    ✗ 「{supp_name}」详细计价采集失败，不计入")

            if len(collected) >= target_count:
                self._log(f"  已达目标 {target_count} 个，停止")
                break

            if economy_ended:
                self._log("  经济型栏已结束（出现特快车/出租车/优享型），其下不再采集，停止")
                break

            # 列表最后一个已采完但仍不够 → 下滑打车页查看新的运力商
            self._log("  ↓ 下滑 1/6 屏，查看新运力商")
            self._swipe_down("s4_next")
            self._wait(self.collection_cfg.get("after_scroll_wait", 1.0), "after_scroll_wait")
            scroll_round += 1

        return collected

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

    def _s2_list_suppliers(self) -> tuple[list[str], bool]:
        """识别当前屏「经济型」栏下的运力商（CAP-09）。

        返回 (suppliers, economy_ended)：
          suppliers     经济型栏内（两道灰线之间）的运力商名称（已过滤快车/拼车/出租等）；
          economy_ended 截图中是否出现「特快车/特惠快车」「出租车」「优享型」等非经济型栏。
        """
        self._log("S2: 识别经济型供应商")
        shot = self._shot("s2_suppliers")
        desc = (
            "高德打车页面用灰色分割线划分多个栏（如「经济型」「特快车/特惠快车」「出租车」「优享型」等）。"
            "只列出「经济型」栏内（两道灰线之间）的运力商行，每行含车型名、预估价格和 ? 问号。"
            "不要列出其他栏（特快车/特惠快车、快车、拼车、出租车、优享型）的行。"
            '以 JSON 返回: {"suppliers": ["曹操出行", ...], "economy_ended": false}'
            "economy_ended: 截图中出现「特快车/特惠快车」「出租车」「优享型」等非经济型栏"
            "（即经济型栏已结束）时为 true，否则 false。"
        )
        self.stats["vlm_calls"] += 1
        result = self.grounder.query_text(shot, desc)
        raw = result.get("raw_response", "").strip()
        self._log(f"  VLM: {raw[:300]}")

        suppliers, economy_ended = self._parse_s2_response(raw)
        if economy_ended:
            self._log(f"  经济型栏已结束（特快车/出租车/优享型出现），当前栏剩余 {len(suppliers)} 个")
        return suppliers, economy_ended

    @staticmethod
    def _parse_s2_response(raw: str) -> tuple[list[str], bool]:
        """解析 S2 VLM 响应 → (suppliers, economy_ended)（CAP-09）。

        兼容两种格式：
          {"suppliers": [...], "economy_ended": true/false}   # 新格式
          ["快车", ...]                                       # 旧数组格式（economy_ended=False）
        按 _SKIP_KEYWORDS 过滤；解析失败返回 ([], False)。
        """
        suppliers: list[str] = []
        economy_ended = False
        cleaned = raw.strip()
        for m in ("```json", "```"):
            if cleaned.startswith(m):
                cleaned = cleaned[len(m):].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                economy_ended = bool(parsed.get("economy_ended", False))
                items = parsed.get("suppliers", [])
                if not isinstance(items, list):
                    return [], economy_ended
                for n in items:
                    n = str(n).strip()
                    if n and not any(kw in n for kw in _SKIP_KEYWORDS) and n not in suppliers:
                        suppliers.append(n)
                return suppliers, economy_ended
            if isinstance(parsed, list):
                for n in parsed:
                    n = str(n).strip()
                    if n and not any(kw in n for kw in _SKIP_KEYWORDS) and n not in suppliers:
                        suppliers.append(n)
                return suppliers, False
        except (json.JSONDecodeError, ValueError):
            pass

        # 非 JSON：逐行兜底
        for line in cleaned.split("\n"):
            line = re.sub(r'^[\d\.\、\)）\-\s]+', '', line.strip())
            line = line.strip().strip('"').strip("'").strip(",")
            if line and len(line) <= 30 and not any(kw in line for kw in _SKIP_KEYWORDS):
                if len(line) >= 2 and line not in suppliers:
                    suppliers.append(line)
        return suppliers, False

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

        center = self._extract_center(result)
        if center is None:
            self._log(f"    ⚠ 未找到「{supplier}」的问号")
            return False
        cx, cy = center

        self._log(f"    问号 at ({cx},{cy})")
        self._annotate_click(shot, f"q_{supplier}", result.get("bbox"), cx, cy)
        self.adb.click(cx, cy)
        self._wait(self.collection_cfg.get("pricing_page_wait", 1.2), "pricing_page_wait")
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
        self._log("      工作日 → 下滑至出现「预约用车」→ 回顶")
        self._tap_tab_and_scroll("工作日", supplier)

        # 3. 休息日
        self._log("      休息日 → 下滑至出现「预约用车」")
        self._tap_tab_and_scroll("休息日", supplier)

        # 4. 返回 ×2: 详细计价页 → 计价弹窗 → 打车页
        self._log("      返回 → 计价弹窗")
        self._tap_back_arrow(f"detail_exit_to_popup_{supplier}")
        self._log("      返回 → 打车页")
        self._tap_back_arrow(f"detail_exit_to_ride_{supplier}")
        return True

    def _tap_tab_and_scroll(self, tab_label: str, supplier: str) -> None:
        sw, sh = self._screen_size
        cached = self._tab_coords.get(tab_label)
        if cached:
            # PERF-03: 复用首次进入详细计价页时记录的标签坐标，直接点击（不调 LLM、不截图）
            cx, cy = cached
            self._log(f"      复用「{tab_label}」标签坐标 ({cx},{cy})")
            self.adb.click(cx, cy)
            self._wait(self.timing.get("tab_wait", 0.5), "tab_wait")
        else:
            # 首次进入详细计价页：LLM 记录标签坐标，后续直接复用
            shot = self._shot(f"detail_before_{tab_label}_{supplier}")
            desc = f"计价规则详情页。找「{tab_label}」标签页（与另一标签并列）。返回 bbox 和中心坐标。"
            self.stats["vlm_calls"] += 1
            result = self.grounder.ground(shot, desc, screen_w=sw, screen_h=sh)
            if result.get("found") and result.get("center"):
                cx, cy = result["center"]
                self._tab_coords[tab_label] = (cx, cy)
                self._annotate_click(shot, f"tab_{tab_label}_{supplier}", result.get("bbox"), cx, cy)
                self.adb.click(cx, cy)
                self._wait(self.timing.get("tab_wait", 0.5), "tab_wait")

        reached_end = self._scroll_to_bottom(tab_label, supplier)
        self._sub_seq += 1
        self._sub_shot(f"{tab_label}_bottom_{supplier}")

        # CAP-01/CAP-05: 到达终点（出现「预约用车」或页面不再变化）→ 回顶后继续后续流程。
        # 未到达（达到滑动上限）也回顶：保证顶部标签栏可见，可正常切换。
        if tab_label == "工作日":
            state = "到达终点（预约用车/页面无变化）" if reached_end else "未到达终点(达上限兜底)"
            self._log(f"      {tab_label}: {state} → 回顶")
            self._scroll_to_top(tab_label, supplier)

    # ==================================================================
    # 原子动作
    # ==================================================================

    def _tap_back_arrow(self, tag: str) -> None:
        """确定性返回：直接 Android back 键，不调用 VLM（PERF-02 方案一）。

        详细计价页 → 计价弹窗 → 打车页 的返回是确定性动作，无需视觉定位；
        保留返回前截图记录现场，等待收敛为 back_wait（默认 1.0s）。
        """
        shot = self._shot(tag)
        self._annotate_action_label(shot, tag, "BACK key")
        self.adb.back()
        self._wait(self.timing.get("back_wait", 1.0), "back_wait")

    def _swipe_down(self, tag: str) -> None:
        """打车页下滑 1/6 屏（每次滑动都截图；距离为原 1/3 屏的一半，CAP-07）。"""
        shot = self._shot(tag)  # 每次滑动都截图
        sw, sh = self._screen_size
        amount = sh // 6  # 1/6 屏（原 1/3 屏的一半）
        cx = sw // 2
        y1 = sh * 2 // 3
        y2 = y1 - amount
        self._log(f"  ↕ ({cx},{y1})→({cx},{y2}) [⅙屏]")
        self._annotate_swipe(shot, tag, cx, y1, cx, y2)
        self.adb.slide(cx, y1, cx, y2, self.collection_cfg.get("scroll_duration_ms", 500))

    def _scroll_to_bottom(self, tab: str, supplier: str) -> bool:
        """每次滑动后判断退出条件：出现「预约用车」**或**页面不再变化（CAP-05）。

        每轮：截图+标注 → 下滑 1/3 屏 → 等待稳定 → 再截图 →
        - LLM 判断是否出现蓝色「预约用车」→ 出现即停止；
        - 或本地像素比对：本次滑动前后页面基本无变化（已到底）→ 停止（防止 LLM 漏检）。
        循环有上限 max_swipes（collection.max_detail_swipes 可配）；达上限返回 False。
        """
        sw, sh = self._screen_size
        amount = sh // 3
        y1 = sh * 3 // 4
        y2 = y1 - amount
        max_swipes = int(self.collection_cfg.get("max_detail_swipes", 12))

        for i in range(max_swipes):
            shot = self._shot(f"{tab}_scroll_{i}_{supplier}")
            self._annotate_swipe(shot, f"{tab}_scroll_{i}_{supplier}", sw // 2, y1, sw // 2, y2)
            self.adb.slide(sw // 2, y1, sw // 2, y2, 300)
            self._wait(self.collection_cfg.get("detail_scroll_wait", 0.3), "short_wait")

            check = self._shot(f"{tab}_check_{i}_{supplier}")
            # 退出条件1：出现「预约用车」（LLM）
            if self._detect_end_marker(check):
                self._log(f"      {tab}[{i}]: 出现「{_END_MARKER}」→ 到达终点，停止")
                return True
            # 退出条件2：页面不再变化（本次滑动前后基本一致 → 已到底）
            if self._page_unchanged(shot, check):
                self._log(f"      {tab}[{i}]: 页面无变化 → 到达终点，停止")
                return True
            self._log(f"      {tab}[{i}]: 未出现「{_END_MARKER}」且页面仍在变化 → 继续下滑")

        self._log(f"      {tab}: 滑动 {max_swipes} 次仍未满足退出条件，按上限退出")
        return False

    def _page_unchanged(
        self,
        prev_path: str,
        curr_path: str,
        max_changed_ratio: float = 0.02,
        pixel_threshold: int = 12,
    ) -> bool:
        """本地像素比对：两张截图内容是否基本未变化（用于判定详情页已到底）。

        缩放为小尺寸灰度图并裁掉顶部状态栏后，统计差异像素占比；
        占比 < max_changed_ratio 视为页面未变化。文件缺失/读取失败返回 False（视为有变化）。
        """
        try:
            a = Image.open(prev_path).convert("L")
            b = Image.open(curr_path).convert("L")
            w = 80
            h = max(1, int(w * a.height / a.width))
            a = a.resize((w, h), Image.LANCZOS)
            b = b.resize((w, h), Image.LANCZOS)
            crop_top = int(h * 0.08)  # 裁掉顶部状态栏（时钟等）
            a = a.crop((0, crop_top, w, h))
            b = b.crop((0, crop_top, w, h))
            diff = ImageChops.difference(a, b)
            hist = diff.histogram()
            changed = sum(hist[pixel_threshold:])
            ratio = changed / (a.width * a.height)
            return ratio < max_changed_ratio
        except Exception as e:
            self._log(f"      ⚠ 页面比对失败（按有变化处理）: {e}")
            return False

    def _detect_end_marker(self, image_path: str) -> bool:
        """调用 LLM 判断详细计价页截图是否已出现蓝色「预约用车」。

        返回 True 表示出现（详情页内容已采集完整，可终止滚动）。
        """
        desc = (
            f"这是高德打车「详细计价规则」页面截图。"
            f"请判断页面上是否出现了蓝色文字「{_END_MARKER}」。"
            "只输出 YES 或 NO。"
        )
        self.stats["vlm_calls"] += 1
        resp = self.grounder.query_text(image_path, desc)
        raw = resp.get("raw_response", "").strip().upper()
        self._log(f"      LLM[{_END_MARKER}]: {raw[:60]}")
        return "YES" in raw

    def _scroll_to_top(self, tab: str, supplier: str) -> None:
        """快速回顶 3 次大段上滑，不截图不标注（纯机械操作）."""
        sw, sh = self._screen_size
        for i in range(3):
            self.adb.slide(sw // 2, sh // 4, sw // 2, sh * 3 // 4, 150)
            self._wait(self.timing.get("scroll_top_wait", 0.1), "short_wait")
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
        anno_dir = self.output_dir / "annotations"  # 标记图子文件夹
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
