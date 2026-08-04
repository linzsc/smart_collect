"""共享执行上下文（ExecutionContext）
============================================================================

FlowEngine 与计价 FSM（以及未来所有采集子流程）共用的一套基础设施：

  - stats     ：VLM 调用/失败、API 耗时、等待时长、步骤数、总耗时
  - wait      ：带归因的等待（累加 wait_seconds）
  - screenshot：截图（目标目录选择、重试、collect 探针临时目录）
  - annotate  ：调试标记图（debug 门控 + PNG 落盘）
  - log       ：统一前缀日志
  - screen_size：设备屏幕尺寸

设计：
  - 一次流程运行（FlowEngine.run）持有唯一 ExecutionContext；子流程
    （如计价采集）共享同一实例 → stats / 等待时长自动归并，无需手动合并。
  - 本模块位于 application 层（codex.md：application = 用例编排、RunContext），
    不依赖任何平台代码。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw

from collector.infrastructure.vision.adapters import VLMServiceAdapter

# 与原有实现一致：截图失败重试次数
_SCREENSHOT_RETRIES = 3
_RETRY_WAIT = 0.5


class ExecutionContext:
    """一次采集运行的共享基础设施上下文。"""

    def __init__(
        self,
        adb: Any,
        grounder: Any,
        *,
        output_dir: str = "./output",
        mode: str = "debug",
        verbose: bool = True,
        log_prefix: str = "Exec",
        stats: dict[str, Any] | None = None,
        text_extractor: Any | None = None,   # OCR TextExtractor（可空，None=OCR 关闭）
    ):
        self.adb = adb
        self.grounder = grounder
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 输出模式: debug(每步截图+标记图) / collect(仅保存必要截图)
        self.mode = mode if mode in ("debug", "collect") else "debug"
        self.verbose = verbose
        self.log_prefix = log_prefix
        self.stats = stats or {
            "vlm_calls": 0, "vlm_failures": 0, "steps_executed": 0,
            "api_seconds": 0.0, "wait_seconds": 0.0, "elapsed": 0.0,
            "ocr_calls": 0, "ocr_failures": 0,
        }
        self._text_extractor = text_extractor
        self._wait_total = 0.0
        self._scratch_dir: Path | None = None
        # 统一视觉能力服务（domain/vision 结构化结果），FlowEngine / 计价 FSM / handler 共用
        self._vision = VLMServiceAdapter(grounder)

    # ------------------------------------------------------------------
    # 模式 / 目录
    # ------------------------------------------------------------------

    @property
    def debug_mode(self) -> bool:
        return self.mode == "debug"

    @property
    def screenshots_dir(self) -> Path:
        """裸截图子文件夹。"""
        return self.output_dir / "screenshots"

    @property
    def annotations_dir(self) -> Path:
        """标记图子文件夹。"""
        return self.output_dir / "annotations"

    @property
    def scratch_dir(self) -> Path | None:
        """collect 模式下的临时截图目录（供 VLM 定位，不计入最终输出）。"""
        if self.debug_mode:
            return None
        if self._scratch_dir is None:
            self._scratch_dir = Path(tempfile.mkdtemp(prefix="collector_scratch_"))
        return self._scratch_dir

    def cleanup(self) -> None:
        """删除 collect 模式的临时截图目录（幂等）。"""
        if self._scratch_dir is not None:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_dir = None

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def incr_vlm_calls(self, n: int = 1) -> None:
        self.stats["vlm_calls"] = self.stats.get("vlm_calls", 0) + n

    def incr_vlm_failures(self, n: int = 1) -> None:
        self.stats["vlm_failures"] = self.stats.get("vlm_failures", 0) + n

    def incr_steps(self, n: int = 1) -> None:
        self.stats["steps_executed"] = self.stats.get("steps_executed", 0) + n

    @property
    def api_seconds(self) -> float:
        """当前累计 API 耗时（对 mock 兼容返回 0.0）。"""
        v = getattr(self.grounder, "api_seconds", None)
        return v if isinstance(v, (int, float)) else 0.0

    @property
    def wait_seconds(self) -> float:
        """累计流程等待时长。"""
        return self._wait_total

    @property
    def vision(self):
        """统一视觉能力服务：定位/问答/分类返回结构化结果（GroundingResult 等）。"""
        return self._vision

    @property
    def ocr(self):
        """本地 OCR 文本提取（TextExtractor）；未注入为 None（OCR 关闭）。"""
        return self._text_extractor

    def incr_ocr_calls(self, n: int = 1) -> None:
        self.stats["ocr_calls"] = self.stats.get("ocr_calls", 0) + n

    def incr_ocr_failures(self, n: int = 1) -> None:
        self.stats["ocr_failures"] = self.stats.get("ocr_failures", 0) + n

    def add_wait(self, seconds: float) -> None:
        """合并子流程产生的等待时长，用于总耗时归因。"""
        self._wait_total += float(seconds or 0.0)

    # ------------------------------------------------------------------
    # 等待
    # ------------------------------------------------------------------

    def wait(self, seconds: float, tag: str = "") -> None:
        """带统计的等待：累加流程设定的等待时长。"""
        if seconds and seconds > 0:
            time.sleep(seconds)
            self._wait_total += float(seconds)

    # ------------------------------------------------------------------
    # 设备
    # ------------------------------------------------------------------

    @property
    def screen_size(self) -> tuple[int, int]:
        sz = self.adb.screen_size
        if sz is not None:
            return sz
        raise RuntimeError("无法获取屏幕尺寸")

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def capture_to(self, path: str) -> bool:
        """截图到指定路径；失败重试 3 次。返回是否成功。"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(_SCREENSHOT_RETRIES):
            if self.adb.get_screenshot(path):
                return True
            self.log(f"  ⚠ 截图失败，重试 {attempt + 1}/{_SCREENSHOT_RETRIES}")
            self.wait(_RETRY_WAIT, "short_wait")
        return False

    def capture(self, stem: str, save: bool | None = None) -> str | None:
        """截图并按模式落盘。

        Args:
            stem: 文件名（不含扩展名）。
            save: True=output/screenshots；False=临时探针目录；
                  None=按模式（debug→落盘，collect→临时目录）。
        Returns:
            截图路径；连续失败返回 None。
        """
        if save is None:
            save = self.debug_mode
        if save:
            target = self.screenshots_dir
        else:
            target = self.scratch_dir or self.output_dir
        target.mkdir(parents=True, exist_ok=True)
        path = str(target / f"{stem}.jpg")
        return path if self.capture_to(path) else None

    # ------------------------------------------------------------------
    # 标注
    # ------------------------------------------------------------------

    def annotate(
        self,
        image_path: str,
        tag: str,
        draw_fn: Callable[[ImageDraw.ImageDraw], None],
    ) -> None:
        """调试标记图：仅 debug 模式落盘；失败只告警不影响主流程。"""
        if not self.debug_mode:
            return
        anno_dir = self.annotations_dir
        anno_dir.mkdir(parents=True, exist_ok=True)
        try:
            img = Image.open(image_path)
            if img.mode in ("RGBA", "P", "LA", "PA"):
                img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            draw_fn(draw)
            out = anno_dir / f"{tag}.png"
            img.save(out, "PNG")
            self.log(f"  📸 标注: {out}")
        except Exception as e:
            self.log(f"  ⚠ 标注失败: {e}")

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def log(self, msg: str, prefix: str | None = None) -> None:
        if self.verbose:
            print(f"[{prefix or self.log_prefix}] {msg}")
