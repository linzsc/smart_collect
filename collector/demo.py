"""兼容层：collector.demo → collector.cli.demo

按 codex.md 目标结构重构后，`python -m collector.demo` 保持可用。
新入口 canonical 路径：python -m collector.cli.demo
"""
from collector.cli.demo import main  # noqa: F401

__all__ = ["main"]


if __name__ == "__main__":
    main()
