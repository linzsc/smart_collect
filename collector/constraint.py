"""兼容层：collector.constraint → collector.domain.constraint

按 codex.md 目标结构重构后，原有导入路径保持兼容。
新代码请直接导入 canonical 路径：
    from collector.domain.constraint import validate_bbox, center_from_bbox
"""
from collector.domain.constraint import (  # noqa: F401
    validate_bbox,
    center_from_bbox,
)

__all__ = ["validate_bbox", "center_from_bbox"]
