"""兼容层：collector.adb_utils → collector.infrastructure.device.adb_utils

按 codex.md 目标结构重构后，原有导入路径保持兼容。
新代码请直接导入 canonical 路径：
    from collector.infrastructure.device.adb_utils import AdbTools, MockAdbTools
"""
from collector.infrastructure.device.adb_utils import (  # noqa: F401
    AdbTools,
    MockAdbTools,
)

__all__ = ["AdbTools", "MockAdbTools"]
