"""目标锚定的幂等全选（SEL-01）
============================================================================

把「定位目标」和「判断状态」拆开：

    locate label → locate 主勾选框 → crop ROI → classify CHECKED/UNCHECKED/UNKNOWN
    → UNCHECKED 才点击 → 重新截图 → 重新定位同一个主勾选框 → CHECKED 才算成功

- 只判断「全选/全选经济」文字右侧同一行的主勾选框小图；
- 已勾选也必须返回真实 checkbox_bbox，禁止全零；
- VLM 输出要求严格 JSON（state 仅 checked/unchecked/unknown），不做自然语言兜底；
- 结构化缺失 → UNKNOWN，禁止盲点。
"""

from __future__ import annotations

import time
from collections import deque

import numpy as np
from typing import Any, Callable

from PIL import Image

from collector.domain.checkbox import CheckboxState, SelectAllTarget, validate_select_all_relation
from collector.infrastructure.vision.checkbox import classify_checkbox_roi, crop_roi

# 归一化坐标（与 VLMGrounder 一致）：0-1000
_COORD_MAX = 1000


class SelectAllError(Exception):
    """全选操作失败（无法定位 / 状态 UNKNOWN / 点击后验证失败）。"""


def _rescale_bbox(bbox_1k: Any, screen_w: int, screen_h: int) -> list[int] | None:
    """0-1000 归一化坐标 → 屏幕像素。"""
    if not isinstance(bbox_1k, list) or len(bbox_1k) != 4:
        return None
    try:
        return [
            round(bbox_1k[0] / _COORD_MAX * screen_w),
            round(bbox_1k[1] / _COORD_MAX * screen_h),
            round(bbox_1k[2] / _COORD_MAX * screen_w),
            round(bbox_1k[3] / _COORD_MAX * screen_h),
        ]
    except (TypeError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# 定位「全选」主勾选框（VLM 严格结构化）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是一个高德打车页面的 UI 解析器。"
    "只输出一个 JSON 对象，不要输出任何其他文字、解释或代码块标记。"
)


_LOCATE_PROMPT_TEMPLATE = (
    "在截图中定位「{LABEL}」文字及其右侧同一行的主勾选框。\n"
    "屏幕是 1000x1000 归一化坐标系，所有坐标在 0-1000 范围。\n"
    "严格只输出一个 JSON，字段如下：\n"
    '{\n'
    '  "target_found": true,\n'
    '  "target": "{LABEL}右侧主勾选框",\n'
    '  "label_bbox": [x1,y1,x2,y2],\n'
    '  "checkbox_bbox": [x1,y1,x2,y2],\n'
    '  "state": "checked" | "unchecked" | "unknown",\n'
    '  "relation": "right_same_row" | "other"\n'
    '}\n'
    "要求：\n"
    "- 勾选框必须位于「{LABEL}」文字右侧、与文字同一行（垂直中心接近）。\n"
    "- 只判断「{LABEL}」文字右侧的主勾选框，忽略页面其他供应商的勾选状态。\n"
    "- 即使已勾选，也必须返回真实 checkbox_bbox，禁止返回全零。\n"
    "- state 只能是 checked / unchecked / unknown 之一。\n"
    '- 找不到「{LABEL}」或勾选框时返回 {"target_found": false, "reason": "原因"}。'
)


def _build_locate_prompt(label: str, screen_w: int, screen_h: int) -> str:
    return _LOCATE_PROMPT_TEMPLATE.replace("{LABEL}", label)


def locate_select_all_target_vlm(
    grounder,
    image_path: str,
    label: str,
    screen_size: tuple[int, int],
    expected_region: list[int] | None = None,
    stats: dict | None = None,
    verbose: bool = True,
) -> SelectAllTarget:
    """用 VLM 严格结构化定位「label」右侧主勾选框。"""
    sw, sh = screen_size
    if stats is not None:
        stats["vlm_calls"] = stats.get("vlm_calls", 0) + 1

    from collector.infrastructure.vision.adapters import visual_query_result_from_dict

    resp = grounder.query_structured(
        image_path,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_locate_prompt(label, sw, sh),
    )
    # WS-2：统一结构化结果，JSON 提取集中在 domain/vision 适配器
    vq = visual_query_result_from_dict(resp) if isinstance(resp, dict) else resp
    obj = vq.structured
    raw = vq.raw_response

    if not obj:
        if verbose:
            print(f"  [SelectAll] ⚠ 结构化结果缺失，返回 UNKNOWN")
        return SelectAllTarget(
            target_found=False, target_label=label,
            state=CheckboxState.UNKNOWN.value,
            reason=f"结构化结果缺失: {raw[:120]}",
        )

    if obj.get("target_found") is False:
        return SelectAllTarget(
            target_found=False, target_label=label,
            state=CheckboxState.UNKNOWN.value,
            reason=str(obj.get("reason", "target_found=false")),
        )

    label_bbox = _rescale_bbox(obj.get("label_bbox"), sw, sh)
    checkbox_bbox = _rescale_bbox(obj.get("checkbox_bbox"), sw, sh)

    relation_valid, reason = validate_select_all_relation(
        label_bbox, checkbox_bbox, screen_size=screen_size, expected_region=expected_region,
    )

    # state 只接受 checked / unchecked / unknown
    state = str(obj.get("state", "")).strip().lower()
    if state not in (CheckboxState.CHECKED.value,
                     CheckboxState.UNCHECKED.value,
                     CheckboxState.UNKNOWN.value):
        state = CheckboxState.UNKNOWN.value

    center = None
    if checkbox_bbox:
        center = ((checkbox_bbox[0] + checkbox_bbox[2]) // 2,
                  (checkbox_bbox[1] + checkbox_bbox[3]) // 2)

    return SelectAllTarget(
        target_found=True,
        target_label=label,
        label_bbox=label_bbox,
        checkbox_bbox=checkbox_bbox,
        checkbox_center=center,
        relation_valid=relation_valid,
        state=state,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 离线定位（高德右缘勾选框布局启发式）
# ---------------------------------------------------------------------------

def locate_select_all_checkbox_offline(
    image: Image.Image,
    search_region: list[int] | None = None,
    label_left_band: int = 320,
) -> SelectAllTarget:
    """离线定位主勾选框：右侧勾选框列中「左侧同行文字最多」的那个。

    适用：高德打车页/选择页的右缘勾选框布局（每行文字在左、勾选框在右缘）。
    素材截图在不同滚动位置下也能定位（按文字密度而非绝对坐标）。

    Args:
        image: 整页截图。
        search_region: [x1,y1,x2,y2] 搜索区域；默认屏幕右缘 ~10% 宽度。
        label_left_band: 勾选框左侧多少像素内统计文字像素。

    Returns:
        SelectAllTarget；未找到时 target_found=False。
    """
    img = image.convert("RGB")
    w, h = img.size
    region = search_region or [int(w * 0.9), 0, w, h]
    rx1, ry1, rx2, ry2 = region

    arr = np.array(img, dtype=int)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    blue = (b >= 90) & (b > r + 25) & (b > g + 25)
    gray = (np.abs(r - g) < 12) & (np.abs(g - b) < 12) & (r >= 110) & (r <= 215) & ~blue
    dark = (r < 90) & (g < 90) & (b < 90)

    mask = (blue | gray).copy()
    mask[:, :rx1] = False
    mask[:, rx2:] = False
    mask[:ry1, :] = False
    mask[ry2:, :] = False

    visited = np.zeros_like(mask, dtype=bool)
    best = None  # (text_px, x1, y1, x2, y2, state)

    for y in range(mask.shape[0]):
        for x in range(mask.shape[1]):
            if not mask[y, x] or visited[y, x]:
                continue
            q = deque([(x, y)])
            visited[y, x] = True
            xs, ys = [], []
            while q:
                cx, cy = q.popleft()
                xs.append(cx)
                ys.append(cy)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        q.append((nx, ny))
            area = len(xs)
            x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
            bw, bh = x2 - x1, y2 - y1
            # 勾选框尺寸过滤（含灰色圆环面积较小的情形）
            if not (350 <= area <= 6000 and 38 <= bw <= 75 and 38 <= bh <= 75
                    and 0.7 <= bw / bh <= 1.4):
                continue
            bcnt = blue[y1:y2 + 1, x1:x2 + 1].sum()
            state = "checked" if bcnt > (bh + 1) * (bw + 1) * 0.25 else "unchecked"
            # 左侧同行文字密度
            lx1 = max(0, x1 - label_left_band)
            lx2 = max(0, x1 - 20)
            text_px = int(dark[y1:y2 + 1, lx1:lx2].sum()) if lx2 > lx1 else 0
            if best is None or text_px > best[0]:
                best = (text_px, x1, y1, x2, y2, state)

    if best is None:
        return SelectAllTarget(
            target_found=False, target_label="",
            state=CheckboxState.UNKNOWN.value, reason="离线定位未找到勾选框",
        )
    text_px, x1, y1, x2, y2, state = best
    return SelectAllTarget(
        target_found=True,
        target_label="",
        checkbox_bbox=[x1, y1, x2, y2],
        checkbox_center=((x1 + x2) // 2, (y1 + y2) // 2),
        relation_valid=True,
        state=state,
        reason=f"offline heuristic (left_text={text_px})",
    )


# ---------------------------------------------------------------------------
# 幂等全选
# ---------------------------------------------------------------------------

def detect_select_all_state(
    image_path: str,
    label: str,
    screen_size: tuple[int, int],
    expected_region: list[int] | None = None,
    grounder=None,
    stats: dict | None = None,
    verbose: bool = False,
) -> tuple[CheckboxState, SelectAllTarget]:
    """定位 + 裁剪 + 分类：返回 (state, target)。grounder 为 None 时无法定位 → UNKNOWN。"""
    if grounder is None:
        return CheckboxState.UNKNOWN, SelectAllTarget(
            target_found=False, target_label=label,
            state=CheckboxState.UNKNOWN.value, reason="未提供定位器(grounder)",
        )

    target = locate_select_all_target_vlm(
        grounder, image_path, label, screen_size, expected_region, stats, verbose,
    )
    if not target.target_found or not target.relation_valid or not target.checkbox_bbox:
        return CheckboxState.UNKNOWN, target

    img = Image.open(image_path)
    roi = crop_roi(img, target.checkbox_bbox)
    state = classify_checkbox_roi(roi)
    target.state = state.value
    return state, target


def ensure_all_selected(
    adb,
    grounder,
    *,
    label: str,
    screen_size: tuple[int, int],
    screenshot: Callable[[str], str],
    expected_region: list[int] | None = None,
    stats: dict | None = None,
    verbose: bool = True,
    wait_after_click: float = 1.0,
    locator: Callable | None = None,
    classifier: Callable | None = None,
) -> SelectAllTarget:
    """确保「label」右侧主勾选框为已勾选（幂等）。

    - 已勾选 → 直接成功（不点击）
    - 未勾选 → 点击一次 → 重新截图 → 重新定位同一勾选框 → 必须变 CHECKED
    - UNKNOWN / 无法定位 / 点击后未变 → 抛 SelectAllError（禁止盲点）
    """
    locator = locator or locate_select_all_target_vlm
    classifier = classifier or classify_checkbox_roi

    def _classify(path: str, target: SelectAllTarget) -> CheckboxState:
        img = Image.open(path)
        roi = crop_roi(img, target.checkbox_bbox)
        return classifier(roi)

    def _log(msg: str) -> None:
        if verbose:
            print(f"[SelectAll] {msg}")

    # ── 第 1 轮：定位 + 判定 ──
    before = screenshot("select_all_before")
    target = locator(grounder, before, label, screen_size, expected_region, stats, verbose)
    if not target.target_found or not target.relation_valid or not target.checkbox_bbox:
        raise SelectAllError(
            f"无法定位「{label}」主勾选框: {target.reason}"
        )
    state = _classify(before, target)
    _log(f"首轮状态: {state.value} @ {target.checkbox_bbox}")

    if state == CheckboxState.UNKNOWN:
        raise SelectAllError("无法确认全选框状态，禁止盲点")
    if state == CheckboxState.CHECKED:
        target.state = CheckboxState.CHECKED.value
        _log("已勾选，跳过点击")
        return target

    # ── 第 2 轮：点击后重验同一个主勾选框 ──
    cx, cy = target.checkbox_center
    if cx is None or cy is None:
        raise SelectAllError("缺少勾选框中心坐标，禁止盲点")
    _log(f"未勾选 → 点击 ({cx},{cy})")
    adb.click(cx, cy)
    if wait_after_click > 0:
        time.sleep(wait_after_click)

    after = screenshot("select_all_after")
    target_after = locator(grounder, after, label, screen_size, expected_region, stats, verbose)
    if not target_after.target_found or not target_after.relation_valid or not target_after.checkbox_bbox:
        raise SelectAllError(f"点击后无法重新定位「{label}」主勾选框: {target_after.reason}")

    state_after = _classify(after, target_after)
    _log(f"点击后状态: {state_after.value} @ {target_after.checkbox_bbox}")
    if state_after != CheckboxState.CHECKED:
        raise SelectAllError("点击后全选状态验证失败")

    target_after.state = CheckboxState.CHECKED.value
    return target_after
