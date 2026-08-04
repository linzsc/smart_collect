"""
OCR 接入测试（离线，不依赖内网 OCR 服务）
============================================================================
覆盖：
  1. ocr_client：convert_point/convert_output/ocr_base_api/本地文件模式（mock requests.post）
  2. ocr_adapter：OcrTextExtractor.extract → TextExtractionResult（含 contains）
  3. flow_engine._do_scroll_until_visible：OCR 优先命中→不调 VLM；OCR 未命中→VLM 兜底
用法:
  .venv/bin/python tests/test_ocr_client.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _fake_ocr_response(text_blocks):
    """构造 OCR 服务假响应：results = [ [字块, 字块, ...] ]（每张图一个条目，条目=字块列表）"""
    blocks = [{
        "text": text,
        "text_region": [[100.0, 200.0], [300.0, 200.0], [300.0, 260.0], [100.0, 260.0]],
        "confidence": 0.95,
    } for text in text_blocks]
    return {"results": [blocks]}


# ======================================================================
# Suite 1: ocr_client
# ======================================================================

def test_convert_point():
    """convert_point: 滴滴字块 → {x,y,w,h,text,c}，w/h 为右/下边界。"""
    from collector.infrastructure.vision import ocr_client as oc

    p = {"text": "预约用车",
         "text_region": [[100.0, 200.0], [300.0, 200.0], [300.0, 260.0], [100.0, 260.0]],
         "confidence": 0.95}
    out = oc.convert_point(p)
    assert out["text"] == "预约用车"
    assert out["x"] == 100.0 and out["y"] == 200.0
    assert out["w"] == 300.0 and out["h"] == 260.0  # 右/下边界
    assert out["c"] == 0.95

    # CONV_DICT 纠错
    out2 = oc.convert_point({"text": "背操出行",
                             "text_region": [[0, 0], [10, 0], [10, 10], [0, 10]]})
    assert out2["text"] == "曹操出行"


def test_convert_output():
    """convert_output: (路径列表, 字块行) → {path: (ocrData, ocrLocations)}。"""
    from collector.infrastructure.vision import ocr_client as oc
    lines = [[{"x": 1, "y": 2, "w": 3, "h": 4, "text": "起步价", "c": 0.9}]]
    ret = oc.convert_output(["a.jpg"], lines)
    assert ret["a.jpg"][0] == "起步价"
    assert ret["a.jpg"][1][0]["text"] == "起步价"


def test_ocr_base_api_mock():
    """ocr_base_api：mock requests.post，验证 payload 结构与响应解析。"""
    from collector.infrastructure.vision import ocr_client as oc

    fake = MagicMock()
    fake.json.return_value = _fake_ocr_response(["预约用车", "起步价"])
    with patch("collector.infrastructure.vision.ocr_client.requests.post", return_value=fake) as m:
        ret = oc.ocr_base_api([b"img-bytes"], max_retries=1, timeout=(2, 10))
    m.assert_called_once()
    payload = json.loads(m.call_args.kwargs.get("data"))
    assert payload["images"][0] == "aW1nLWJ5dGVz"  # base64("img-bytes")
    assert len(ret["results"]) == 1
    assert len(ret["results"][0]) == 2


def test_didi_ocr_local_file_mock():
    """本地文件模式：mock requests.post → cache_ret 有结果。"""
    import os
    from collector.infrastructure.vision import ocr_client as oc

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "shot.jpg")
        Path(path).write_bytes(b"fake-jpg")
        fake = MagicMock()
        fake.json.return_value = _fake_ocr_response(["预约用车"])
        with patch("collector.infrastructure.vision.ocr_client.requests.post", return_value=fake):
            client = oc.DidiOcrCli(max_workers=1)
            client.scan_concurrency_local_files([path], retry=1, timeout=(2, 10))
        assert path in client.cache_ret
        ocr_data, ocr_locations = client.cache_ret[path]
        assert "预约用车" in ocr_data
        assert ocr_locations[0]["text"] == "预约用车"


# ======================================================================
# Suite 2: ocr_adapter
# ======================================================================

def test_ocr_adapter_extract():
    """OcrTextExtractor.extract → TextExtractionResult（blocks + contains）。"""
    import os
    from collector.infrastructure.vision.ocr_adapter import OcrTextExtractor

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "shot.jpg")
        Path(path).write_bytes(b"fake-jpg")
        fake = MagicMock()
        fake.json.return_value = _fake_ocr_response(["预约用车", "起步价 10元"])
        with patch("collector.infrastructure.vision.ocr_client.requests.post", return_value=fake):
            result = OcrTextExtractor(retry=1, timeout=(2, 10)).extract(path)
        assert result.success is True
        assert result.contains("预约用车") is True
        assert result.contains("起步价") is True
        assert result.contains("不存在") is False
        assert result.blocks[0].text == "预约用车"
        assert result.blocks[0].bbox == [100, 200, 300, 260]


def test_ocr_adapter_failure_no_raise():
    """OCR 异常 → success=False + reason，不抛异常（供上层 VLM 兜底）。"""
    from collector.infrastructure.vision.ocr_adapter import OcrTextExtractor
    with patch("collector.infrastructure.vision.ocr_client.requests.post",
               side_effect=RuntimeError("net")):
        result = OcrTextExtractor(retry=1, timeout=1).extract("/no/such/file.jpg")
    assert result.success is False
    assert result.reason


# ======================================================================
# Suite 3: flow_engine._do_scroll_until_visible OCR 优先
# ======================================================================

def test_scroll_until_visible_ocr_first():
    """OCR 优先：命中→不调 VLM；未命中→VLM 兜底。"""
    from collector.workflows.flow_engine import FlowEngine

    flow_yaml = """
name: "t"
version: "1"
steps:
  - id: "scroll"
    type: "scroll_until_visible"
    target_text: "预约用车"
    target_prompt: "是否出现「预约用车」？YES/NO"
    ocr_first: true
    max_swipes: 3
    wait_after_slide: 0.01
"""
    with tempfile.TemporaryDirectory() as tmp:
        flow = Path(tmp) / "flow.yaml"
        flow.write_text(flow_yaml, encoding="utf-8")
        mock_grounder = MagicMock()
        mock_grounder.api_seconds = 0.0
        mock_grounder.query_text.return_value = {"raw_response": "YES", "success": True}
        mock_adb = MagicMock()
        type(mock_adb).screen_size = PropertyMock(return_value=(1080, 2400))

        # 场景1：OCR 首帧命中 → VLM 不调用
        ocr_hit = MagicMock()
        ocr_hit.extract.return_value = MagicMock(success=True, contains=lambda t: True)
        engine = FlowEngine(
            adb=mock_adb, grounder=mock_grounder, flow_path=str(flow),
            output_dir=str(Path(tmp) / "out1"), verbose=False,
            text_extractor=ocr_hit,
        )
        engine.run()
        assert ocr_hit.extract.call_count >= 1
        assert mock_grounder.query_text.call_count == 0, "OCR 命中不应调 VLM"
        assert engine.stats["ocr_calls"] >= 1

        # 场景2：OCR 未命中 → VLM 兜底
        ocr_miss = MagicMock()
        ocr_miss.extract.return_value = MagicMock(success=True, contains=lambda t: False)
        engine2 = FlowEngine(
            adb=mock_adb, grounder=mock_grounder, flow_path=str(flow),
            output_dir=str(Path(tmp) / "out2"), verbose=False,
            text_extractor=ocr_miss,
        )
        engine2.run()
        assert mock_grounder.query_text.call_count >= 1, "OCR 未命中应走 VLM 兜底"
        assert engine2.stats["vlm_calls"] >= 1


# ======================================================================
# Runner
# ======================================================================

def main() -> None:
    tests = [
        ("convert_point 归一化", test_convert_point),
        ("convert_output", test_convert_output),
        ("ocr_base_api mock", test_ocr_base_api_mock),
        ("DidiOcrCli 本地文件 mock", test_didi_ocr_local_file_mock),
        ("OcrTextExtractor.extract", test_ocr_adapter_extract),
        ("OcrTextExtractor 失败兜底", test_ocr_adapter_failure_no_raise),
        ("scroll_until_visible OCR 优先", test_scroll_until_visible_ocr_first),
    ]
    all_pass = True
    for label, fn in tests:
        try:
            fn()
            print(f"  [PASS ✓] {label}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  [FAIL ✗] {label}: {e}")
            all_pass = False
    print("\n" + "=" * 50)
    print(f"  {'✓ 全部通过' if all_pass else '✗ 存在失败'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
