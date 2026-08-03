"""兼容层：collector.vlm_grounder → collector.infrastructure.vision.vlm_grounder

按 codex.md 目标结构重构后，原有导入路径保持兼容。
新代码请直接导入 canonical 路径：
    from collector.infrastructure.vision.vlm_grounder import VLMGrounder
"""
from collector.infrastructure.vision.vlm_grounder import (  # noqa: F401
    VLMGrounder,
    smart_resize,
    pil_to_base64,
    image_to_base64,
    build_grounding_prompt,
    build_grounding_prompt_with_context,
    PAGE_CLASSIFICATION_PROMPT,
    GROUNDING_SYSTEM_PROMPT,
    _extract_tool_call_json,
)

__all__ = [
    "VLMGrounder",
    "smart_resize",
    "pil_to_base64",
    "image_to_base64",
    "build_grounding_prompt",
    "build_grounding_prompt_with_context",
    "PAGE_CLASSIFICATION_PROMPT",
    "GROUNDING_SYSTEM_PROMPT",
    "_extract_tool_call_json",
]
