"""Platform 接入契约
============================================================================

新增平台 = 在 `collector/platform/<name>/` 下实现本契约并注册到注册表，
不需要修改通用代码（cli / workflows / infrastructure）。

一个 Platform 描述：

- name          平台标识（用于 --platform 与流程文件名 <flow>_<name>.yaml）
- flows_dir     YAML 流程目录
- profile_path  平台 Profile（JSON）
- default_flow  默认流程名（如 "v1"）
- add_cli_args  可选：向 CLI 注册平台自己的参数（如高德的 --address / --pickup）
- build_flow_vars 可选：从 CLI 参数构造流程模板变量 {{.Var}}
- step_handlers 平台特有步骤类型 -> 处理器；通用引擎遇到未知步骤时查这里

本模块不依赖任何 SDK / 平台代码。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 平台特有步骤处理器签名：handler(engine, step) -> None
# engine 为 FlowEngine（可访问 adb/grounder/output_dir/stats/timing 等），
# step 为 YAML 中的步骤 dict。
StepHandler = Callable[[Any, dict], None]

# 平台 CLI 参数注册：add_cli_args(parser) -> None
CliArgsRegistrar = Callable[[argparse.ArgumentParser], None]

# 流程模板变量构造：build_flow_vars(args, flow_name) -> dict[str, str]
FlowVarsBuilder = Callable[[argparse.Namespace, str], dict[str, str]]


@dataclass(frozen=True)
class Platform:
    """一个可执行采集流程的平台接入描述。"""

    name: str
    flows_dir: Path
    profile_path: Path
    default_flow: str = "v1"
    add_cli_args: CliArgsRegistrar | None = None
    build_flow_vars: FlowVarsBuilder | None = None
    step_handlers: dict[str, StepHandler] = field(default_factory=dict)

    # ── 便捷方法 ──

    def resolve_flow(self, flow_name: str) -> Path:
        """按约定解析流程文件：<flows_dir>/<flow_name>_<platform_name>.yaml"""
        return self.flows_dir / f"{flow_name}_{self.name}.yaml"

    def list_flow_names(self) -> list[str]:
        """列出平台内置流程名（去掉 <name> 后缀），供 CLI 提示用。"""
        names: list[str] = []
        for f in sorted(self.flows_dir.glob(f"*_{self.name}.yaml")):
            stem = f.stem
            prefix = stem[: -len(f"_{self.name}")]
            names.append(prefix)
        return names

    def load_profile(self) -> dict[str, Any]:
        """加载平台 Profile（JSON）。"""
        import json

        with open(self.profile_path, "r", encoding="utf-8") as f:
            return json.load(f)
