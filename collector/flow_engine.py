"""兼容层：collector.flow_engine → collector.workflows.flow_engine

按 codex.md 目标结构重构后，原有导入路径保持兼容。
新代码请直接导入 canonical 路径：
    from collector.workflows.flow_engine import FlowEngine, StepFailed
"""
from collector.workflows.flow_engine import (  # noqa: F401
    FlowEngine,
    StepFailed,
)

__all__ = ["FlowEngine", "StepFailed"]
