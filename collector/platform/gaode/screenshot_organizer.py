"""
采集结果整理（RES-01）：筛选必要截图并按「工作日/休息日 × 运力商」聚合到 result/
============================================================================

计价采集结束后调用。从 output/ 筛选必要截图：

- 打车页（全选经济后）：文件名含 `select_all_after` 或 `verify_select_all_before`（每轮校验都会拍；全选已勾选跳过点击时的兜底），取最早一张
- 每个运力商每个标签页的全部滚动截图：`*_<标签>_scroll_<i>_<运力商>.jpg`（数量随下滑次数，CAP-10）
- 轻量模式（--capture-mode test）：`detail_shot_<运力商>.jpg` 归入 `计价页/<运力商>/`

聚合结构（result/）：

    result/
    ├── 工作日/
    │   ├── 冒泡页/
    │   │   └── p04_select_all_after.jpg      # 打车页：每个大文件夹各 1 次
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
DEFAULT_SCROLL_COUNT: int | None = None  # None = 取全部滚动帧（数量随下滑次数，CAP-10）
# 打车页（全选经济后）文件名关键字：select_all_after（真正点击后）
# 或 verify_select_all_before（每轮校验时都会拍；全选已勾选跳过点击时无 select_all_after 的兜底）
RIDE_PAGE_KEYWORDS: tuple[str, ...] = ("select_all_after", "verify_select_all_before")
RIDE_PAGE_FOLDER = "冒泡页"             # 打车页所在子文件夹（每个大文件夹各 1 次）
DETAIL_SHOT_KEYWORD = "detail_shot"    # 轻量模式（--capture-mode test）：detail_shot_<运力商>.jpg
DETAIL_PAGE_PSEUDO_TAB = "计价页"       # 轻量模式详细计价页证据所在的伪标签目录

Logger = Callable[[str], None] | None


def collect_necessary_screenshots(
    output_dir: str | Path,
    result_dir: str | Path = "./result",
    tabs: tuple[str, ...] = DEFAULT_TABS,
    scroll_count: int | None = DEFAULT_SCROLL_COUNT,
    ride_page_keyword: str | None = None,
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

    shots_dir = out / "screenshots"  # 裸截图统一在 output/screenshots/
    jpgs = sorted(shots_dir.glob("*.jpg")) if shots_dir.is_dir() else []
    # 打车页证据：任意关键字命中；取最早一张（避免每轮校验都复制一份造成噪音）
    ride_candidates = [p for p in jpgs
                       if any(k in p.name for k in RIDE_PAGE_KEYWORDS)
                       or (ride_page_keyword and ride_page_keyword in p.name)]
    ride_pages = ride_candidates[:1]
    if not ride_pages:
        log("  ⚠ 结果整理: 未找到打车页截图"
            f"（含 {'/'.join(RIDE_PAGE_KEYWORDS)}），跳过打车页；仍整理详细计价页")

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

    # 轻量模式：detail_shot_<运力商>.jpg 作为「详细计价页」证据，归入伪标签「计价页」
    for p in jpgs:
        m = re.match(rf".*{re.escape(DETAIL_SHOT_KEYWORD)}_(.+)\.jpg$", p.name)
        if m:
            supplier = m.group(1).strip()
            if supplier:
                groups.setdefault((DETAIL_PAGE_PSEUDO_TAB, supplier), {})[0] = p

    if not groups and not ride_pages:
        log("  ⚠ 结果整理: 未找到打车页与详细计价页截图，跳过")
        return {"result_dir": str(res), "ride_pages": [p.name for p in ride_pages],
                "groups": {}, "copied": 0}

    # 重新生成 result/（幂等）
    if res.exists():
        shutil.rmtree(res)

    all_tabs = list(tabs) + ([DETAIL_PAGE_PSEUDO_TAB] if any(k[0] == DETAIL_PAGE_PSEUDO_TAB for k in groups) else [])
    copied = 0
    built: dict[str, dict[str, list[str]]] = {tab: {} for tab in all_tabs}
    for tab in all_tabs:
        tab_groups = {k: v for k, v in groups.items() if k[0] == tab}
        if not tab_groups:
            continue
        # 打车页 → 真实标签 <标签>/冒泡页/（每个大文件夹各 1 次；「计价页」伪标签只放详细计价页证据）
        if ride_pages and tab in tabs:
            bubble_dir = res / tab / RIDE_PAGE_FOLDER
            bubble_dir.mkdir(parents=True, exist_ok=True)
            for rp in ride_pages:
                shutil.copy2(rp, bubble_dir / rp.name)
                copied += 1
        for (_, supplier), by_idx in sorted(tab_groups.items()):
            indices = sorted(by_idx)
            chosen = [by_idx[i] for i in indices if scroll_count is None or i < scroll_count]
            if not chosen:
                continue
            folder = res / tab / supplier
            folder.mkdir(parents=True, exist_ok=True)
            for src_file in chosen:
                shutil.copy2(src_file, folder / src_file.name)
                copied += 1
            built.setdefault(tab, {})[supplier] = [f.name for f in chosen]

    log(f"  ✓ 结果整理: {copied} 张必要截图 → {res}")
    for tab in all_tabs:
        if built.get(tab) and ride_pages and tab in tabs:
            log(f"    {tab}/冒泡页: 打车页 {len(ride_pages)} 张")
        for supplier, files in built.get(tab, {}).items():
            log(f"    {tab}/{supplier}: {'滚动' if tab in tabs else '详细计价页'}截图 {len(files)} 张")

    return {
        "result_dir": str(res),
        "ride_pages": [p.name for p in ride_pages],
        "groups": built,
        "copied": copied,
    }
