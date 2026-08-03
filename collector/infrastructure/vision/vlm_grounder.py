"""
VLM Grounding Client
============================================================================

Calls Qwen3-VL-Plus (DashScope / MaaS OpenAI-compatible endpoint) for visual
grounding: given a screenshot + natural-language element description → bbox.

Uses the same normalized-coordinate approach as Mobile-Agent-v3.5:
  - System prompt tells the VLM the screen is 1000×1000.
  - VLM returns coordinates in 0–1000 range.
  - Python rescales to actual screen pixels.
  - Image resize dimensions are irrelevant — VLM never sees real pixels.

Same API calling pattern as Ali's GUIOwlWrapper (OpenAI client, retry with
backoff, smart_resize + base64).
"""

import base64
import json
import math
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image


# ---------------------------------------------------------------------------
# Image processing (same algorithm as utils.py)
# ---------------------------------------------------------------------------

def smart_resize(
    height: int,
    width: int,
    factor: int = 16,
    min_pixels: int | None = None,
    max_pixels: int | None = None,
) -> tuple[int, int]:
    """Rescale dimensions to be factor-aligned while preserving aspect ratio.

    Same algorithm as Mobile-Agent-v3.5/mobile_use/utils.py:271-313.
    """
    IMAGE_MIN_TOKEN_NUM = 4
    IMAGE_MAX_TOKEN_NUM = 16384
    MAX_RATIO = 200

    max_pixels = max_pixels or (IMAGE_MAX_TOKEN_NUM * factor ** 2)
    min_pixels = min_pixels or (IMAGE_MIN_TOKEN_NUM * factor ** 2)
    assert max_pixels >= min_pixels, "max_pixels must be >= min_pixels."

    if max(height, width) / min(height, width) > MAX_RATIO:
        raise ValueError(
            f"Aspect ratio must be < {MAX_RATIO}, "
            f"got {max(height, width) / min(height, width)}"
        )

    def _round(n):
        return round(n / factor) * factor

    def _floor(n):
        return math.floor(n / factor) * factor

    def _ceil(n):
        return math.ceil(n / factor) * factor

    h_bar = max(factor, _round(height))
    w_bar = max(factor, _round(width))

    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = _floor(height / beta)
        w_bar = _floor(width / beta)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = _ceil(height * beta)
        w_bar = _ceil(width * beta)

    return h_bar, w_bar


def pil_to_base64(image: Image.Image) -> str:
    """PIL Image → base64 string (PNG)."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def image_to_base64(
    image_path: str,
    max_pixels: int = 1200000,
    factor: int = 28,
) -> str:
    """Load, smart_resize, and base64-encode an image.

    Same algorithm as utils.py image_to_base64.
    Since we use normalized 1000×1000 coords, we only need the data URL —
    no need to return scale factors.
    """
    if image_path.startswith("file://"):
        image_path = image_path[len("file://"):]

    img = Image.open(image_path)
    MIN_PIXELS = 3136

    resized_h, resized_w = smart_resize(
        img.height, img.width,
        factor=factor,
        min_pixels=MIN_PIXELS,
        max_pixels=max_pixels,
    )
    img = img.resize((resized_w, resized_h), Image.LANCZOS)
    return f"data:image/png;base64,{pil_to_base64(img)}"


# ---------------------------------------------------------------------------
# Grounding prompts — normalized 1000×1000 coordinate system
# ---------------------------------------------------------------------------

GROUNDING_SYSTEM_PROMPT = """# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {"name": "mobile_use", "description": "Use a touchscreen to interact with a mobile device, and take screenshots.\\n* This is an interface to a mobile device with touchscreen. You can perform actions like clicking, typing, swiping, etc.\\n* Some applications may take time to start or process actions, so you may need to wait and take successive screenshots to see the results of your actions.\\n* The screen's resolution is 1000x1000.\\n* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked.", "parameters": {"properties": {"action": {"description": "The action to perform. The available actions are:\\n* `click`: Click the point on the screen with coordinate (x, y).\\n* `answer`: Return the bounding box of the requested element.", "enum": ["click", "answer"], "type": "string"}, "coordinate": {"description": "(x, y): The x (pixels from left) and y (pixels from top) coordinates. Required by `action=click` and `action=answer`. Both in 0–1000 range.", "type": "array"}, "bbox": {"description": "[x1, y1, x2, y2]: bounding box in 0–1000 coordinates. Required only by `action=answer`.", "type": "array"}}, "required": ["action"], "type": "object"}, "args_format": "Format the arguments as a JSON object."}}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>

# Response format

Response format for every step:
1) Action: a short imperative describing what to do in the UI.
2) A single <tool_call>...</tool_call> block containing only the JSON: {"name": <function-name>, "arguments": <args-json-object>}.

Rules:
- Output exactly in the order: Action, <tool_call>.
- Be brief: one sentence for Action.
- Do not output anything else outside those two parts.
- The screen is EXACTLY 1000×1000 pixels. Every coordinate MUST be in 0–1000 range.
- When locating an element, use action=answer and include both coordinate (center) and bbox."""


def build_grounding_prompt(element_desc: str, page_context: str = "") -> str:
    """Build the user prompt for a single-turn grounding request."""
    context_line = f"\n**当前页面说明**：{page_context}\n" if page_context else ""
    return (
        f"在当前手机截图中定位以下UI元素，返回它的边界框(bbox)：\n\n"
        f"**要找的元素**：{element_desc}\n"
        f"{context_line}"
        f"**要求**：\n"
        f"- 仔细观察截图，找到与描述最匹配的可交互元素\n"
        f"- 如果有多个相似元素，选最主要、最可能被用户点击的那个\n"
        f"- 先写一行中文Action描述你找到了什么，然后输出<tool_call>\n"
        f"- 使用 action=answer，coordinate 填元素中心坐标，bbox 填紧密包裹的边界框\n"
        f"- 如果找不到该元素，**先解释为什么找不到**（例如「当前是POI详情页，没有打车tab」），然后 bbox 填 [0,0,0,0]"
    )


PAGE_CLASSIFICATION_PROMPT = """对当前手机截图做页面分类。仔细观察页面内容，判断当前属于以下哪一类页面的描述。

可用类别：
- **首页**：高德地图首页，有搜索框、地图视图、底部导航栏
- **搜索页**：搜索框已激活，可输入文字，通常有搜索历史或键盘弹出
- **搜索结果页**：显示了地点候选列表，每个候选包含地名和地址
- **POI详情页**：展示某个地点的详细信息（介绍、电话、评分、周边），有「路线」「导航」等按钮，但**没有底部驾车/公交/骑行/打车tab栏**
- **路径规划页**：显示了驾车/公交/骑行/打车等出行方式的tab栏，可以查看不同出行方式的路线
- **打车页**：显示了车型列表、价格、预估信息，有「全选」或呼叫按钮
- **其他/未知**：不属于以上任何类别

要求：
- 仔细分析页面特征
- 先写一行中文说明你看到了什么
- 然后输出 <tool_call>{"name": "mobile_use", "arguments": {"action": "answer", "page_type": "<类别名>"}}</tool_call>
- 类别名只能是上面列出的一种"""


def build_grounding_prompt_with_context(element_desc: str, expected_page: str) -> str:
    """Build grounding prompt with expected page context for cross-validation."""
    return (
        f"在当前手机截图中定位以下UI元素，返回它的边界框(bbox)：\n\n"
        f"**要找的元素**：{element_desc}\n\n"
        f"**预期页面**：当前应该位于「{expected_page}」。\n"
        f"如果截图明显不是{expected_page}（例如进入了一个不同的页面），"
        f"请在 Action 中明确指出差异（如：'预期路径规划页，实际进入POI详情页'），"
        f"然后 bbox 填 [0,0,0,0]。\n\n"
        f"**要求**：\n"
        f"- 仔细观察截图，找到与描述最匹配的可交互元素\n"
        f"- 如果有多个相似元素，选最主要、最可能被用户点击的那个\n"
        f"- 先写一行中文Action描述你找到了什么（或为什么找不到），然后输出<tool_call>\n"
        f"- 使用 action=answer，coordinate 填元素中心坐标，bbox 填紧密包裹的边界框\n"
        f"- 如果找不到该元素，**先解释原因**，然后 bbox 填 [0,0,0,0]"
    )


# ---------------------------------------------------------------------------
# VLM Grounder
# ---------------------------------------------------------------------------

class VLMGrounder:
    """Call Qwen3-VL-Plus for single-turn visual grounding.

    Uses normalized 1000×1000 coordinate system (same pattern as
    Mobile-Agent-v3.5). VLM returns 0–1000 coords → Python rescales
    to actual screen pixels.
    """

    RETRY_WAITING_SECONDS = 10

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "qwen3-vl-plus",
        max_retry: int = 5,
        temperature: float = 0.0,
        image_max_pixels: int = 1200000,
    ):
        self.model = model
        self.max_retry = min(max(max_retry, 1), 10)
        self.temperature = temperature
        self.image_max_pixels = image_max_pixels
        # 耗时统计
        self.api_seconds = 0.0   # 累计 API 调用耗时
        self.api_calls = 0       # 累计 API 调用次数

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=30,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ground(
        self,
        image_path: str,
        element_desc: str,
        screen_w: int,
        screen_h: int,
        ref_image: str | None = None,
        ref_images: list[str] | None = None,
    ) -> dict[str, Any]:
        """Locate an element in a screenshot.

        Args:
            image_path: Path to the screenshot.
            element_desc: Natural-language description of the target element.
            screen_w, screen_h: Actual device screen dimensions for coord rescaling.
            ref_image: Optional single reference image path.
            ref_images: Optional list of reference image paths (for multi-image matching).

        Returns:
            dict with element, bbox (screen pixels), center (screen pixels),
            conf, found, reason, raw_response.
        """
        user_text = build_grounding_prompt(element_desc)
        image_data_url = image_to_base64(
            image_path, max_pixels=self.image_max_pixels,
        )

        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]

        # 单张参考图
        if ref_image and Path(ref_image).exists():
            ref_data_url = image_to_base64(
                ref_image, max_pixels=self.image_max_pixels,
            )
            user_content.append(
                {"type": "image_url", "image_url": {"url": ref_data_url}},
            )

        # 多张参考图
        if ref_images:
            for rf in ref_images:
                if rf and Path(rf).exists():
                    rf_url = image_to_base64(rf, max_pixels=self.image_max_pixels)
                    user_content.append(
                        {"type": "image_url", "image_url": {"url": rf_url}},
                    )

        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": GROUNDING_SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": user_content,
            },
        ]

        raw_text, success = self._call_api(messages)
        return self._parse(raw_text, element_desc, screen_w, screen_h, success)

    def query_text(self, image_path: str, prompt: str) -> dict[str, Any]:
        """Send image + text to VLM, return raw text (no grounding format).

        Uses a simple chat completion — no grounding system prompt,
        no tool_call enforcement. For classification, listing, open-ended
        questions where we want plain text, not coordinates.
        """
        image_data_url = image_to_base64(
            image_path, max_pixels=self.image_max_pixels,
        )
        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": (
                    "你是一个移动应用截图分析助手。直接回答用户问题。"
                    "回答要简洁、精确。只输出答案文本，不要输出函数调用、"
                    "工具调用、XML标签或JSON结构。"
                )}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
        ]
        raw_text, success = self._call_api(messages)
        return {"raw_response": raw_text or "", "success": success}

    def query_structured(
        self,
        image_path: str,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """发送 图片+严格指令，只返回原始文本（不做任何自然语言解析）。

        供上层（如全选勾选框定位）严格解析 JSON；结构化缺失时由上层判 UNKNOWN。
        """
        image_data_url = image_to_base64(
            image_path, max_pixels=self.image_max_pixels,
        )
        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
        ]
        raw_text, success = self._call_api(messages)
        return {"raw_response": raw_text or "", "success": success}

    def ground_with_context(
        self,
        image_path: str,
        element_desc: str,
        expected_page: str,
        screen_w: int,
        screen_h: int,
        ref_image: str | None = None,
    ) -> dict[str, Any]:
        """Ground an element with expected page context.

        VLM cross-validates: if the page doesn't match expected_page,
        it reports why in the response — helping the FSM detect it's
        on the wrong page vs. the element just being missing.
        """
        user_text = build_grounding_prompt_with_context(element_desc, expected_page)
        image_data_url = image_to_base64(
            image_path, max_pixels=self.image_max_pixels,
        )

        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]

        if ref_image and Path(ref_image).exists():
            ref_data_url = image_to_base64(
                ref_image, max_pixels=self.image_max_pixels,
            )
            user_content.append(
                {"type": "image_url", "image_url": {"url": ref_data_url}},
            )

        messages = [
            {"role": "system", "content": [{"type": "text", "text": GROUNDING_SYSTEM_PROMPT}]},
            {"role": "user", "content": user_content},
        ]

        raw_text, success = self._call_api(messages)
        return self._parse(raw_text, element_desc, screen_w, screen_h, success)

    def classify_page(self, image_path: str) -> dict[str, Any]:
        """Classify the current page type from a screenshot.

        Returns:
            dict with keys: page_type (str), confidence (str), raw_response.
        """
        image_data_url = image_to_base64(
            image_path, max_pixels=self.image_max_pixels,
        )

        messages = [
            {"role": "system", "content": [{"type": "text", "text": GROUNDING_SYSTEM_PROMPT}]},
            {"role": "user", "content": [
                {"type": "text", "text": PAGE_CLASSIFICATION_PROMPT},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]},
        ]

        raw_text, success = self._call_api(messages)
        return self._parse_page_classification(raw_text, success)

    @staticmethod
    def _parse_page_classification(raw_text: str, api_success: bool) -> dict[str, Any]:
        """Parse page classification result from VLM response."""
        default = {
            "page_type": "unknown",
            "confidence": "unknown",
            "raw_response": raw_text or "",
        }

        if not raw_text or not api_success:
            default["page_type"] = "api_error"
            return default

        tool_json = _extract_tool_call_json(raw_text)
        if tool_json:
            args = tool_json.get("arguments", {})
            page_type = args.get("page_type", "unknown")
            return {"page_type": page_type, "confidence": "high", "raw_response": raw_text}

        # Fallback: parse from raw text
        raw_lower = raw_text.lower()
        for candidate in ["首页", "搜索页", "搜索结果页", "poi详情页",
                          "路径规划页", "打车页"]:
            if candidate.lower() in raw_lower:
                return {"page_type": candidate, "confidence": "medium", "raw_response": raw_text}

        default["raw_response"] = raw_text
        return default

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_api(self, messages: list[dict]) -> tuple[str, bool]:
        """Call the VLM API with exponential-backoff retry.

        Same pattern as GUIOwlWrapper.predict_mm().
        """
        wait_sec = self.RETRY_WAITING_SECONDS
        for attempt in range(self.max_retry):
            t0 = time.time()
            try:
                completion = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                )
                self.api_seconds += time.time() - t0
                self.api_calls += 1
                return (completion.choices[0].message.content or ""), True
            except Exception as e:
                self.api_seconds += time.time() - t0
                print(f"  [VLM] API error (attempt {attempt + 1}/{self.max_retry}): {e}")
                if attempt < self.max_retry - 1:
                    time.sleep(wait_sec)
                    wait_sec = min(wait_sec * 1.5, 60)
        return f"Error: max retries ({self.max_retry}) exceeded", False

    @staticmethod
    def _parse(
        raw_text: str,
        element_desc: str,
        screen_w: int,
        screen_h: int,
        api_success: bool,
    ) -> dict[str, Any]:
        """Parse VLM <tool_call> output, rescale 0–1000 → screen pixels."""
        default = {
            "element": element_desc,
            "bbox": None,
            "center": None,
            "conf": 0.0,
            "found": False,
            "selected": None,
            "reason": None,
            "raw_response": raw_text,
        }

        if not raw_text:
            default["reason"] = "Empty VLM response"
            return default

        # 只接受显式 SELECTED=true/false 标记；不再做自然语言兜底（SEL-01）。
        # 结构化结果缺失时 selected 保持 None（上层应判 UNKNOWN）。
        selected: bool | None = None
        sel_match = re.search(r'SELECTED\s*=\s*(true|false)', raw_text, re.IGNORECASE)
        if sel_match:
            selected = sel_match.group(1).lower() == "true"
        default["selected"] = selected

        if not api_success:
            default["reason"] = f"API call failed: {raw_text[:200]}"
            return default

        # Extract JSON from <tool_call>...</tool_call>
        tool_json = _extract_tool_call_json(raw_text)
        if not tool_json:
            default["reason"] = f"No <tool_call> JSON found in: {raw_text[:200]}"
            default["selected"] = selected
            return default

        try:
            args = tool_json.get("arguments", {})
        except (AttributeError, TypeError):
            default["reason"] = f"Malformed tool_call: {raw_text[:200]}"
            default["selected"] = selected
            return default

        coord_1k = args.get("coordinate")
        bbox_1k = args.get("bbox")

        if bbox_1k and len(bbox_1k) == 4 and bbox_1k != [0, 0, 0, 0]:
            # Rescale 0–1000 → screen pixels
            bbox = [
                round(bbox_1k[0] / 1000 * screen_w),
                round(bbox_1k[1] / 1000 * screen_h),
                round(bbox_1k[2] / 1000 * screen_w),
                round(bbox_1k[3] / 1000 * screen_h),
            ]
            if coord_1k and len(coord_1k) == 2:
                center = [
                    round(coord_1k[0] / 1000 * screen_w),
                    round(coord_1k[1] / 1000 * screen_h),
                ]
            else:
                center = [
                    (bbox[0] + bbox[2]) // 2,
                    (bbox[1] + bbox[3]) // 2,
                ]
            return {
                **default,
                "element": element_desc,
                "bbox": bbox,
                "center": center,
                "found": True,
                "selected": selected,
                "conf": 0.90,
            }
        elif (coord_1k and len(coord_1k) == 2
              and coord_1k != [0, 0]):  # reject sentinel [0,0] (= already selected)
            # Has center but no bbox — treat as found with synthetic bbox
            cx = round(coord_1k[0] / 1000 * screen_w)
            cy = round(coord_1k[1] / 1000 * screen_h)
            return {
                **default,
                "element": element_desc,
                "bbox": [cx - 30, cy - 30, cx + 30, cy + 30],
                "center": [cx, cy],
                "found": True,
                "selected": selected,
                "conf": 0.70,
            }
        else:
            return {
                **default,
                "found": False,
                "selected": selected,
                "reason": f"VLM returned no valid bbox/coordinate. Args: {args}",
            }


def _extract_tool_call_json(text: str) -> dict | None:
    """Extract the JSON object from a <tool_call>...</tool_call> block."""
    # Try standard format
    m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', text, re.DOTALL)
    if m:
        raw = m.group(1).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    # Try markdown code block
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try bare JSON
    m = re.search(r'\{[^{}]*"action"[^{}]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None
