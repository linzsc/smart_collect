"""
Bounding Box Constraint Validation
===========================================================================

Validates VLM grounding results against screen geometry and per-element
constraint rules defined in the Platform Profile.

Does NOT require an LLM — pure arithmetic / geometry checks.
"""

from typing import Any


def validate_bbox(
    bbox: list[int] | None,
    screen_size: tuple[int, int] | None,
    constraints: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate a VLM-predicted bbox against screen bounds and rules.

    Args:
        bbox: [x1, y1, x2, y2] from the VLM (or None).
        screen_size: (width, height) of the screenshot.
        constraints: Optional per-element constraints from gaode.json:
            - expected_region: [x1, y1, x2, y2] — bbox must overlap this region.
            - min_width_ratio: float — bbox width >= screen_w * ratio.
            - max_width_ratio: float — bbox width <= screen_w * ratio.
            - min_height_ratio: float — bbox height >= screen_h * ratio.
            - max_height_ratio: float — bbox height <= screen_h * ratio.
            - min_area_px: int — minimum absolute area in pixels.
            - max_area_ratio: float — max area as fraction of screen area.

    Returns:
        (is_valid, reason) tuple.
    """
    constraints = constraints or {}

    # --- null check ---
    if bbox is None:
        return False, "bbox is None (VLM returned no coordinates)"

    if len(bbox) != 4:
        return False, f"bbox has {len(bbox)} values, expected 4"

    x1, y1, x2, y2 = bbox

    # --- screen bounds ---
    if screen_size is not None:
        sw, sh = screen_size

        if x1 < 0:
            return False, f"bbox x1={x1} < 0 (out of screen)"
        if y1 < 0:
            return False, f"bbox y1={y1} < 0 (out of screen)"
        if x2 > sw:
            return False, f"bbox x2={x2} > screen width {sw}"
        if y2 > sh:
            return False, f"bbox y2={y2} > screen height {sh}"
    else:
        sw, sh = None, None  # unknown screen size → skip ratio checks below

    w, h = x2 - x1, y2 - y1

    if w <= 0 or h <= 0:
        return False, f"bbox has non-positive dimensions: w={w}, h={h}"

    # --- minimum absolute size ---
    if w < 10 or h < 10:
        return False, f"bbox too small: {w}x{h} (min 10x10)"

    # --- max area ratio ---
    if sw is not None and sh is not None:
        screen_area = sw * sh
        area = w * h
        max_area_ratio = constraints.get("max_area_ratio", 0.8)
        if area > screen_area * max_area_ratio:
            return False, (
                f"bbox area {area}px > {max_area_ratio*100:.0f}% "
                f"of screen area {screen_area}px"
            )

    # --- ratio constraints ---
    if sw is not None:
        min_wr = constraints.get("min_width_ratio")
        if min_wr is not None and w < sw * min_wr:
            return False, (
                f"bbox width {w} < {min_wr*100:.0f}% of screen width {sw}"
            )
        max_wr = constraints.get("max_width_ratio")
        if max_wr is not None and w > sw * max_wr:
            return False, (
                f"bbox width {w} > {max_wr*100:.0f}% of screen width {sw}"
            )

    if sh is not None:
        min_hr = constraints.get("min_height_ratio")
        if min_hr is not None and h < sh * min_hr:
            return False, (
                f"bbox height {h} < {min_hr*100:.0f}% of screen height {sh}"
            )
        max_hr = constraints.get("max_height_ratio")
        if max_hr is not None and h > sh * max_hr:
            return False, (
                f"bbox height {h} > {max_hr*100:.0f}% of screen height {sh}"
            )

    # --- absolute min area ---
    min_area = constraints.get("min_area_px")
    if min_area is not None and w * h < min_area:
        return False, f"bbox area {w * h}px < min {min_area}px"

    # --- expected region overlap ---
    expected_region = constraints.get("expected_region")
    if expected_region is not None:
        erx1, ery1, erx2, ery2 = expected_region
        # At least 50% of bbox area must overlap the expected region.
        overlap_x1 = max(x1, erx1)
        overlap_y1 = max(y1, ery1)
        overlap_x2 = min(x2, erx2)
        overlap_y2 = min(y2, ery2)
        ow = max(0, overlap_x2 - overlap_x1)
        oh = max(0, overlap_y2 - overlap_y1)
        overlap_area = ow * oh
        bbox_area = w * h
        if bbox_area > 0 and overlap_area < bbox_area * 0.5:
            return False, (
                f"bbox [{x1},{y1},{x2},{y2}] has <50% overlap "
                f"with expected region {expected_region}"
            )

    return True, "OK"


def center_from_bbox(bbox: list[int]) -> tuple[int, int]:
    """Extract center point from a bbox [x1, y1, x2, y2]."""
    return (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
