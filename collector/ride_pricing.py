"""兼容层：collector.ride_pricing → collector.platform.gaode.ride_pricing

按 codex.md 目标结构重构后，原有导入路径保持兼容。
新代码请直接导入 canonical 路径：
    from collector.platform.gaode.ride_pricing import RidePricingFSM
"""
from collector.platform.gaode.ride_pricing import (  # noqa: F401
    RidePricingFSM,
    _SKIP_SUPPLIERS,
)

__all__ = ["RidePricingFSM", "_SKIP_SUPPLIERS"]
