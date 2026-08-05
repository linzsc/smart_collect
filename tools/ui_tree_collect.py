#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 树交互采集脚本（P0-0 标定工具）

用法（在项目根目录运行）:
    python3 tools/ui_tree_collect.py            # 默认设备 c84d0df2
    python3 tools/ui_tree_collect.py <序列号>    # 指定设备

交互流程:
    输入页面名称 → 自动 uiautomator dump → 解析文字/坐标 → 记录
    输入 end / 结束 / quit / q 退出
    输入 last → 重复采集上一个页面

输出（注意不在 output/ 下——demo 启动会 rmtree ./output，会清掉采集数据）:
    data/ui_tree/ui_tree_pages.jsonl        每行一个页面记录（JSON Lines，追加写）{page, time, device, node_count, nodes[]}
    data/ui_tree/ui_tree_pages_array.json   采集结束自动生成的美化版 JSON 数组（全部页面）
    data/ui_tree/screens/{page}.png         每页截图（供后续 OCR 对比分析）
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(ROOT, "data", "ui_tree", "ui_tree_pages.jsonl")
PRETTY_OUT_FILE = os.path.join(ROOT, "data", "ui_tree", "ui_tree_pages_array.json")
SCREEN_DIR = os.path.join(ROOT, "data", "ui_tree", "screens")
DEFAULT_SERIAL = "c84d0df2"

SUGGESTED_PAGES = [
    "手机页(桌面)",
    "首页",
    "点击打车后",
    "输入POI页",
    "打车页(冒泡页)",
    "计价页(弹窗)",
    "详细计价页",
]

_TAG_RE = re.compile(r"<[^>]+>")
_BASE64_RE = re.compile(r"(?:data:)?(?:image/[^;]+;)?(?:[\w/+-]+;)?base64,[A-Za-z0-9+/=]*")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_IGNORE_TEXT = {"", " ", "　", "None", "null"}


def clean_text(raw: str) -> str:
    """清洗 uiautomator 文本：反转义 → 去 HTML 标签 → 去 base64 → 压空白。"""
    if not raw:
        return ""
    t = html_lib.unescape(raw)          # &lt; &#10; → 真实字符
    t = _TAG_RE.sub(" ", t)             # 去 <font color=...> 等富文本标签
    t = _BASE64_RE.sub(" ", t)          # 去 base64 图片串
    t = _MULTI_SPACE_RE.sub(" ", t)
    return t.strip()


def parse_bounds(bounds: str | None) -> dict | None:
    if not bounds:
        return None
    m = _BOUNDS_RE.search(bounds)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return {
        "left": x1, "top": y1, "right": x2, "bottom": y2,
        "center": [(x1 + x2) // 2, (y1 + y2) // 2],
    }


def adb(serial: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", "-s", serial, *args],
        capture_output=True, text=True, timeout=30,
    )


def check_device(serial: str) -> bool:
    """检查 adb 设备是否在线，给出引导提示。"""
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        print("❌ 找不到 adb 命令。请先安装 platform-tools 或确认 adb 在 PATH 中。")
        return False
    devices = [line.split("\t")[0] for line in r.stdout.splitlines()[1:] if line.strip()]
    if serial in devices:
        print(f"✅ 设备 {serial} 在线")
        return True
    if not devices:
        print("❌ 没有检测到任何设备。请检查：")
        print("   1. 手机开启「开发者选项 → USB 调试」并插上 USB")
        print("   2. 手机上允许这台电脑的调试授权")
        print(f"   3. 然后运行: adb devices 确认能看到 {serial}")
    else:
        print(f"❌ 设备 {serial} 不在线。当前设备: {devices}")
        print(f"   可改用: python3 tools/ui_tree_collect.py {devices[0]}")
    return False



def adb_bytes(serial: str, *args: str) -> subprocess.CompletedProcess:
    """二进制输出通道（截图等）。"""
    return subprocess.run(
        ["adb", "-s", serial, *args],
        capture_output=True, timeout=30,
    )


def collect_xml(serial: str) -> str | None:
    """uiautomator dump + cat，失败重试一次。返回 XML 文本或 None。"""
    for attempt in (1, 2):
        r = adb(serial, "shell", "uiautomator", "dump", "/sdcard/ui.xml")
        out = (r.stdout + r.stderr).lower()
        if r.returncode == 0 and "dumped" in out:
            cat = adb(serial, "shell", "cat", "/sdcard/ui.xml")
            if cat.returncode == 0 and cat.stdout.strip():
                return cat.stdout
            print("  ⚠ cat ui.xml 为空，重试")
        else:
            print(f"  ⚠ uiautomator dump 失败(第{attempt}次): {out.strip()[:120]}")
        time.sleep(1)
    return None


def capture_screenshot(serial: str, page: str) -> str | None:
    os.makedirs(SCREEN_DIR, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", page).strip("_") or "page"
    path = os.path.join(SCREEN_DIR, f"{safe}.png")
    r = adb_bytes(serial, "exec-out", "screencap", "-p")
    if r.returncode != 0 or not r.stdout:
        return None
    with open(path, "wb") as f:
        f.write(r.stdout)
    return path


def parse_nodes(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _parse_nodes_regex(xml_text)

    nodes = []
    for el in root.iter("node"):
        text = clean_text(el.get("text", ""))
        desc = clean_text(el.get("content-desc", ""))
        b = parse_bounds(el.get("bounds", ""))
        if b is None:
            continue
        clickable = el.get("clickable", "false") == "true"
        if text in _IGNORE_TEXT and desc in _IGNORE_TEXT and not clickable:
            continue
        nodes.append({
            "text": text,
            "content_desc": desc,
            "class": el.get("class", ""),
            "resource_id": el.get("resource-id", ""),
            "clickable": clickable,
            "checked": el.get("checked"),
            **b,
        })
    return nodes


def _parse_nodes_regex(xml_text: str) -> list[dict]:
    nodes = []
    for m in re.finditer(r"<node\b[^>]*>", xml_text):
        attrs = m.group(0)
        get = lambda k: (re.search(rf'{k}="([^"]*)"', attrs) or [None, ""])[1]
        text = clean_text(get("text"))
        desc = clean_text(get("content-desc"))
        b = parse_bounds(get("bounds"))
        if b is None:
            continue
        clickable = get("clickable") == "true"
        if text in _IGNORE_TEXT and desc in _IGNORE_TEXT and not clickable:
            continue
        nodes.append({
            "text": text, "content_desc": desc,
            "class": get("class"), "resource_id": get("resource-id"),
            "clickable": clickable, "checked": get("checked"),
            **b,
        })
    return nodes


def export_pretty_json() -> int:
    """把 ui_tree_pages.jsonl 汇总成美化版 JSON 数组，返回记录总数。"""
    if not os.path.exists(OUT_FILE):
        return 0
    records = []
    with open(OUT_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  ⚠ 跳过无法解析的行: {line[:80]}")
    os.makedirs(os.path.dirname(PRETTY_OUT_FILE), exist_ok=True)
    with open(PRETTY_OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return len(records)


def main() -> None:
    serial = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SERIAL
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    os.makedirs(SCREEN_DIR, exist_ok=True)

    print("=" * 60)
    print("UI 树交互采集工具（P0-0 标定）")
    print("=" * 60)
    print(f"记录文件: {OUT_FILE}")
    print(f"截图目录: {SCREEN_DIR}")
    print()
    print("建议按这个顺序采集（名称可自定义，能认出来就行）：")
    for i, p in enumerate(SUGGESTED_PAGES, 1):
        print(f"  {i}. {p}")
    print()
    if not check_device(serial):
        sys.exit(1)
    print()

    count = 0
    last = ""
    while True:
        try:
            name = input("页面名称 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not name:
            continue
        if name.lower() in ("end", "quit", "exit", "q", "结束", "退出"):
            break
        if name.lower() == "last":
            if not last:
                print("  ⚠ 还没有上一个页面")
                continue
            name = last

        print(f"  采集「{name}」...")
        xml_text = collect_xml(serial)
        if xml_text is None:
            print("  ⚠ 采集失败，跳过本页（检查屏幕是否亮着、是否停在目标页面）")
            continue

        nodes = parse_nodes(xml_text)
        shot = capture_screenshot(serial, name)
        rec = {
            "page": name,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "device": serial,
            "node_count": len(nodes),
            "screenshot": shot,
            "nodes": nodes,
        }
        with open(OUT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        count += 1
        last = name

        print(f"  ✅ 记录 {len(nodes)} 个节点" + (f"，截图 {shot}" if shot else ""))
        shown = [n for n in nodes if n["text"]][:12]
        for n in shown:
            cls = (n["class"] or "?").split(".")[-1]
            print(f"    「{n['text']}」 {cls} clickable={n['clickable']} center={n['center']}")
        total_text = sum(1 for n in nodes if n["text"])
        if total_text > len(shown):
            print(f"    ...（共 {total_text} 个文字节点，其余见记录文件）")
        print("    输入 last 可重采本页；输入 end 结束")

    n = export_pretty_json()
    print(f"\n完成，本次采集 {count} 个页面 → {OUT_FILE}")
    if n:
        print(f"已生成美化版 JSON 数组 → {PRETTY_OUT_FILE}（共 {n} 个页面）")


if __name__ == "__main__":
    main()
