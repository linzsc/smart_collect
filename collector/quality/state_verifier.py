"""
Page State Verification (no LLM, no OCR)
===========================================================================

Lightweight verifiers:
  - Screen diff ratio (pixel-level change detection)
  - Activity name check (adb dumpsys)
"""

import subprocess
import time

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Screen diff (pixel-level change detection)
# ---------------------------------------------------------------------------

def verify_screen_diff(
    before_path: str,
    after_path: str,
    min_diff_ratio: float = 0.01,
) -> float:
    """Compute pixel-level difference ratio between two screenshots.

    Args:
        before_path: Screenshot before the action.
        after_path: Screenshot after the action.
        min_diff_ratio: Threshold below which we consider "no change".

    Returns:
        diff_ratio: Fraction of pixels that changed (0.0 ~ 1.0).
    """
    before = Image.open(before_path).convert("RGB")
    after = Image.open(after_path).convert("RGB")

    if before.size != after.size:
        after = after.resize(before.size, Image.LANCZOS)

    arr_before = np.array(before, dtype=np.int16)
    arr_after = np.array(after, dtype=np.int16)

    diff = np.abs(arr_before - arr_after).mean(axis=2)
    changed = (diff > 10).sum()
    total = diff.size

    return changed / total if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Activity check via adb dumpsys
# ---------------------------------------------------------------------------

def verify_activity(
    adb,
    expected_activity_prefix: str,
    timeout_s: float = 3.0,
) -> bool:
    """Check if the current foreground Activity matches the expected prefix.

    Uses `adb shell dumpsys activity activities | grep topResumedActivity`.

    Args:
        adb: AdbTools instance.
        expected_activity_prefix: e.g. "com.autonavi.minimap" for 高德.
        timeout_s: Command timeout in seconds.

    Returns:
        True if the current Activity starts with expected prefix.
    """
    cmd = (
        f"{adb.adb_path}{adb._device_flag}"
        "shell dumpsys activity activities 2>/dev/null "
        "| grep -E 'topResumedActivity|mResumedActivity' | head -1"
    )
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, shell=True, timeout=timeout_s,
        )
        output = result.stdout.strip()
        if not output:
            return False
        return expected_activity_prefix.lower() in output.lower()
    except Exception as e:
        print(f"  [Activity] Check failed: {e}")
        return False
