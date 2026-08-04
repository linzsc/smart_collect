"""OCR 文本提取适配器：滴滴内部 OCR → domain/vision TextExtractionResult
============================================================================

实现 collector/domain/vision/interfaces.py 的 TextExtractor 协议，
供「预约用车」等锚点文本的本地检测（OCR 优先，VLM 兜底，v2 详细计价页）。

用法：
    extractor = OcrTextExtractor()
    result = extractor.extract("/path/to/screenshot.jpg")
    result.contains("预约用车")   # True/False
"""
from __future__ import annotations

from collector.domain.vision.interfaces import TextExtractor
from collector.domain.vision.models import TextBlock, TextExtractionResult
from collector.infrastructure.vision.ocr_client import DidiOcrCli


class OcrTextExtractor(TextExtractor):
    """基于滴滴内部 OCR 服务的本地文本提取（单图模式）。"""

    def __init__(
        self,
        client: DidiOcrCli | None = None,
        retry: int = 3,
        timeout: tuple[int, int] | int = (2, 10),
    ):
        self._client = client
        self._retry = retry
        self._timeout = timeout

    def extract(self, image_path: str) -> TextExtractionResult:
        """本地单图 OCR → TextExtractionResult。

        失败不抛异常：返回 success=False + reason（供上层 VLM 兜底）。
        """
        try:
            # 逐帧提取：每次新建轻量客户端，避免 cache_ret 无限膨胀
            client = self._client or DidiOcrCli(max_workers=1)
            client.scan_concurrency_local_files(
                [image_path], retry=self._retry, timeout=self._timeout,
            )
            item = client.cache_ret.get(image_path)
            if item is None:
                return TextExtractionResult(success=False, reason="OCR 未返回结果")
            _ocr_data, ocr_locations = item
            blocks = [
                TextBlock(
                    text=loc["text"],
                    bbox=[int(loc["x"]), int(loc["y"]), int(loc["w"]), int(loc["h"])],
                    confidence=float(loc.get("c") or 1.0),
                )
                for loc in ocr_locations
            ]
            return TextExtractionResult(blocks=blocks, success=True)
        except Exception as e:  # noqa: BLE001 - 兜底交给上层 VLM
            return TextExtractionResult(success=False, reason=str(e))
