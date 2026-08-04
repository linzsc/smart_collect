"""
采集结果整理（RES-01）：筛选必要截图并按「工作日/休息日 × 运力商」聚合到 result/
============================================================================

计价采集结束后调用。从 output/ 筛选必要截图：

- 打车页（全选经济后）：文件名含 `select_all_after`，如 p04_select_all_after.jpg
- 每个运力商每个标签页的前 N 张滚动截图：`*_<标签>_scroll_<i>_<运力商>.jpg`（默认 N=4，i=0..3）

聚合结构（result/）：

    result/
    ├── 工作日/
    │   ├── 冒泡页/
    │   │   └── p04_select_all_after.jpg      # 打车页：每个大文件夹各 1 次（共 2 次）
    │   ├── <运力商A>/
    │   │   ├── *_工作日_scroll_0_<运力商A>.jpg
    │   │   └── ...
    │   └── <运力商B>/ ...
    └── 休息日/
        ├── 冒泡页/ ...
        ├── <运力商A>/ ...
        └── <运力商B>/ ...

只复制不移动，不删除 output/ 原图；output/ 为空或缺失时安全跳过。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

DEFAULT_TABS: tuple[str, ...] = ("工作日", "休息日")
DEFAULT_SCROLL_COUNT = 4               # 每个 (标签, 运力商) 保留 scroll_0..N-1
RIDE_PAGE_KEYWORD = "select_all_after"  # 打车页（全选经济后）文件名关键字
RIDE_PAGE_FOLDER = "冒泡页"             # 打车页所在子文件夹（每个大文件夹各 1 次）

Logger = Callable[[str], None] | None


def collect_necessary_screenshots(
    output_dir: str | Path,
    result_dir: str | Path = "./result",
    tabs: tuple[str, ...] = DEFAULT_TABS,
    scroll_count: int = DEFAULT_SCROLL_COUNT,
    ride_page_keyword: str = RIDE_PAGE_KEYWORD,
    logger: Logger = None,
) -> dict:
    """筛选必要截图并聚合到 result/，返回摘要。

    返回:
        {
          "result_dir": str,
          "ride_pages": [文件名...],
          "groups": {"工作日": {"运力商": [滚动截图文件名...], ...}, "休息日": {...}},
          "copied": int,
        }
    """
    out = Path(output_dir)
    res = Path(result_dir)

    def log(msg: str) -> None:
        if logger:
            logger(msg)

    if not out.is_dir():
        log(f"  ⚠ 结果整理: 输出目录不存在 {out}，跳过")
        return {"result_dir": str(res), "ride_pages": [], "groups": {}, "copied": 0}

    jpgs = sorted(out.glob("*.jpg"))
    ride_pages = [p for p in jpgs if ride_page_keyword in p.name]
    if not ride_pages:
        log(f"  ⚠ 结果整理: 未找到打车页截图（含 '{ride_page_keyword}'），跳过")
        return {"result_dir": str(res), "ride_pages": [], "groups": {}, "copied": 0}

    # (标签, 运力商) -> {滚动序号: 文件}
    groups: dict[tuple[str, str], dict[int, Path]] = {}
    for p in jpgs:
        for tab in tabs:
            m = re.match(rf".*_{re.escape(tab)}_scroll_(\d+)_(.+)\.jpg$", p.name)
            if m:
                supplier = m.group(2).strip()
                if supplier:
                    groups.setdefault((tab, supplier), {})[int(m.group(1))] = p
                break  # 一张截图只属于一个标签

    if not groups:
        log("  ⚠ 结果整理: 未找到任何滚动截图，跳过")
        return {"result_dir": str(res), "ride_pages": [p.name for p in ride_pages],
                "groups": {}, "copied": 0}

    # 重新生成 result/（幂等）
    if res.exists():
        shutil.rmtree(res)

    copied = 0
    built: dict[str, dict[str, list[str]]] = {tab: {} for tab in tabs}
    for tab in tabs:
        tab_groups = {k: v for k, v in groups.items() if k[0] == tab}
        if not tab_groups:
            continue
        # 打车页 → <标签>/冒泡页/（每个大文件夹各 1 次，共 2 次）
        bubble_dir = res / tab / RIDE_PAGE_FOLDER
        bubble_dir.mkdir(parents=True, exist_ok=True)
        for rp in ride_pages:
            shutil.copy2(rp, bubble_dir / rp.name)
            copied += 1
        for (_, supplier), by_idx in sorted(tab_groups.items()):
            indices = sorted(by_idx)
            chosen = [by_idx[i] for i in indices if i < scroll_count]
            if not chosen:
                continue
            folder = res / tab / supplier
            folder.mkdir(parents=True, exist_ok=True)
            for src_file in chosen:
                shutil.copy2(src_file, folder / src_file.name)
                copied += 1
            built.setdefault(tab, {})[supplier] = [f.name for f in chosen]

    log(f"  ✓ 结果整理: {copied} 张必要截图 → {res}")
    for tab in tabs:
        if built.get(tab):
            log(f"    {tab}/冒泡页: 打车页 {len(ride_pages)} 张")
        for supplier, files in built.get(tab, {}).items():
            log(f"    {tab}/{supplier}: 滚动截图 {len(files)} 张")

    return {
        "result_dir": str(res),
        "ride_pages": [p.name for p in ride_pages],
        "groups": built,
        "copied": copied,
    }
