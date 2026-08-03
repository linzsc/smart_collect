"""兼容层：collector.state_verifier → collector.quality.state_verifier

按 codex.md 目标结构重构后，原有导入路径保持兼容。
新代码请直接导入 canonical 路径：
    from collector.quality.state_verifier import verify_screen_diff, verify_activity
"""
from collector.quality.state_verifier import (  # noqa: F401
    verify_screen_diff,
    verify_activity,
)

__all__ = ["verify_screen_diff", "verify_activity"]
