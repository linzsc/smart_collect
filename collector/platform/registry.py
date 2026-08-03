"""平台注册表
============================================================================

- `get_platform(name)` / `available_platforms()`：CLI 与引擎取平台入口。
- 新增平台：在 `collector/platform/<name>/` 下实现 Platform，然后在本文件
  `_register_builtins()` 中加一行注册，通用代码无需改动。
"""

from __future__ import annotations

from collector.domain.platform import Platform

_PLATFORMS: dict[str, Platform] = {}


def register_platform(platform: Platform) -> None:
    """注册一个平台（同名覆盖，便于测试注入）。"""
    _PLATFORMS[platform.name] = platform


def get_platform(name: str) -> Platform:
    """按名称取平台；未注册则抛 KeyError 并列出可用平台。"""
    try:
        return _PLATFORMS[name]
    except KeyError:
        available = ", ".join(sorted(_PLATFORMS)) or "(none)"
        raise KeyError(
            f"未注册的平台: {name!r}，可用平台: {available}"
        ) from None


def available_platforms() -> list[str]:
    """所有已注册平台名（排序）。"""
    return sorted(_PLATFORMS)


def unregister_platform(name: str) -> None:
    """注销一个平台（主要用于测试清理）。"""
    _PLATFORMS.pop(name, None)


def _register_builtins() -> None:
    from collector.platform.gaode.platform import build_platform

    register_platform(build_platform())


_register_builtins()
