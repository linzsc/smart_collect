"""
ADB Utilities — extracted from Mobile-Agent-v3.5/mobile_use/utils.py
===========================================================================

Pure ADB wrapper for device interaction. No Agent logic.
All methods sourced from Ali's AdbTools class (utils.py:70-216).

Setup Prerequisites:
  1. Enable USB Debugging on the Android device.
  2. Connect via USB, verify with `adb devices`.
  3. Install ADB Keyboard APK and set it as default input method.
  4. Grant execute permission to the adb binary (macOS/Linux).

Usage:
    adb = AdbTools("/path/to/adb")
    adb.get_screenshot("screenshot.png")
    adb.click(500, 800)
    adb.type("北京西站")
"""

import os
import subprocess
import time

from PIL import Image


class AdbTools:
    """Wrapper around ADB commands for device interaction.

    Directly extracted from Mobile-Agent-v3.5/mobile_use/utils.py:70-216.
    Removed: get_package_name (not needed for Demo).
    """

    def __init__(self, adb_path: str, device: str | None = None):
        """Initialize ADB wrapper.

        Args:
            adb_path: Absolute path to the adb binary.
            device: Optional device serial number (when multiple devices connected).
        """
        self.adb_path = adb_path
        self.device = device
        self._device_flag = f" -s {device} " if device is not None else " "
        self.image_info: tuple[int, int] | None = None
        self.action_delay: float = 0.2  # 每个动作后等待手机响应

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self, args: str) -> None:
        """Execute an ADB command string."""
        cmd = self.adb_path + self._device_flag + args
        subprocess.run(cmd, capture_output=True, text=True, shell=True)

    def _load_image_info(self, path: str) -> None:
        """Cache screenshot dimensions (width, height)."""
        self.image_info = Image.open(path).size

    @property
    def screen_size(self) -> tuple[int, int] | None:
        """Return cached (width, height) or None if no screenshot taken yet."""
        return self.image_info

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    def get_screenshot(self, image_path: str, retry_times: int = 3) -> bool:
        """Capture device screenshot and save to *image_path*.

        Uses sdcard relay + size validation to avoid empty-file false positives
        that occur with the `exec-out screencap` method on older ADB versions.

        Returns True on success.
        """
        device_flag = f" -s {self.device}" if self.device else ""
        remote_path = "/sdcard/ma_screenshot_tmp.png"

        for attempt in range(retry_times):
            if attempt == 0:
                # Keep screen awake during USB session (one-shot).
                subprocess.run(
                    f"{self.adb_path}{device_flag} shell svc power stayon true",
                    capture_output=True, text=True, shell=True,
                )
            else:
                # Clear any stale/corrupt remote file before retry.
                subprocess.run(
                    f"{self.adb_path}{device_flag} shell rm -f {remote_path}",
                    capture_output=True, text=True, shell=True,
                )

            # Capture → sdcard → pull locally.
            subprocess.run(
                f"{self.adb_path}{device_flag} shell screencap -p {remote_path}",
                capture_output=True, text=True, shell=True,
            )
            subprocess.run(
                f"{self.adb_path}{device_flag} pull {remote_path} {image_path}",
                capture_output=True, text=True, shell=True,
            )

            # Validate: file must exist and not be (near-)empty.
            if os.path.exists(image_path) and os.path.getsize(image_path) > 100:
                self._load_image_info(image_path)
                return True
            time.sleep(0.1)

        return False

    # ------------------------------------------------------------------
    # Input actions
    # ------------------------------------------------------------------

    def click(self, x: int, y: int) -> None:
        """Tap at screen coordinate (x, y)."""
        self._run(f"shell input tap {x} {y}")
        time.sleep(self.action_delay)

    def long_press(self, x: int, y: int, duration: int = 800) -> None:
        """Long-press at (x, y) for *duration* milliseconds."""
        self._run(f"shell input swipe {x} {y} {x} {y} {duration}")
        time.sleep(self.action_delay)

    def slide(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        slide_time: int = 800,
    ) -> None:
        """Swipe from (x1, y1) to (x2, y2) over *slide_time* ms."""
        self._run(f"shell input swipe {x1} {y1} {x2} {y2} {slide_time}")
        time.sleep(self.action_delay)

    def back(self) -> None:
        """Press the Back button."""
        self._run("shell input keyevent 4")
        time.sleep(self.action_delay)

    def home(self) -> None:
        """Press the Home button."""
        self._run(
            "shell am start -a android.intent.action.MAIN "
            "-c android.intent.category.HOME"
        )
        time.sleep(self.action_delay)

    def type(self, text: str) -> None:
        """Type text via ADB Keyboard (supports CJK + Latin).

        Requires the ADB Keyboard APK to be installed and set as
        the default input method on the device.
        """
        escaped_text = text.replace('"', '\\"').replace("'", "\\'")
        command_sequence = [
            "shell ime enable com.android.adbkeyboard/.AdbIME",
            "shell ime set com.android.adbkeyboard/.AdbIME",
            0.1,
            f'shell am broadcast -a ADB_INPUT_TEXT --es msg "{escaped_text}"',
            0.1,
            "shell ime disable com.android.adbkeyboard/.AdbIME",
        ]

        for item in command_sequence:
            if isinstance(item, (int, float)):
                time.sleep(item)
            else:
                self._run(item.strip())
        time.sleep(self.action_delay)

    def clear_text(self) -> None:
        """Clear the current input field via ADB Keyboard CLEAR broadcast."""
        command_sequence = [
            "shell ime enable com.android.adbkeyboard/.AdbIME",
            "shell ime set com.android.adbkeyboard/.AdbIME",
            0.1,
            "shell am broadcast -a ADB_CLEAR_TEXT",
            0.1,
            "shell ime disable com.android.adbkeyboard/.AdbIME",
        ]
        for item in command_sequence:
            if isinstance(item, (int, float)):
                time.sleep(item)
            else:
                self._run(item.strip())
        time.sleep(self.action_delay)

    def open_app(self, package_name: str) -> None:
        """Launch an app by its Android package name."""
        self._run(
            f"shell monkey -p {package_name} "
            "-c android.intent.category.LAUNCHER 1"
        )
        time.sleep(self.action_delay)

    def key_enter(self) -> None:
        """Press the Enter/Return key."""
        self._run("shell input keyevent 66")
        time.sleep(self.action_delay)


# ---------------------------------------------------------------------------
# Mock ADB Tools — for testing without a real device
# ---------------------------------------------------------------------------

class MockAdbTools(AdbTools):
    """Mock ADB wrapper that records actions instead of executing them.

    Useful for:
      - Testing the FSM logic without a connected device
      - Debugging ground-truth annotation workflows
      - CI/CD integration tests

    Instead of running real ADB commands, all actions are logged and
    screenshot requests return a placeholder image.
    """

    def __init__(self, mock_screenshots_dir: str | None = None):
        """Initialize mock ADB tools.

        Args:
            mock_screenshots_dir: If set, screenshots will be read from
                this directory (e.g. for testing with pre-captured images).
                If None, a blank placeholder PNG is written instead.
        """
        # Skip real AdbTools.__init__ — no real adb_path needed.
        self.adb_path = "(mock)"
        self.device = None
        self._device_flag = " "
        self.image_info: tuple[int, int] | None = None
        self.action_delay: float = 0.0  # mock 不需要等待
        self.mock_screenshots_dir = mock_screenshots_dir
        self.action_log: list[dict] = []

    def _run(self, args: str) -> None:
        """Log the command instead of running it."""
        self.action_log.append({"type": "adb_cmd", "args": args})
        print(f"  [MOCK ADB] {args}")

    def get_screenshot(self, image_path: str, retry_times: int = 3) -> bool:
        """Return a placeholder screenshot or read from mock dir."""
        import os
        from PIL import Image

        fname = os.path.basename(image_path)

        # If we have a mock screenshots directory with a matching file, use it.
        if self.mock_screenshots_dir:
            src = os.path.join(self.mock_screenshots_dir, fname)
            if os.path.exists(src):
                import shutil
                shutil.copy(src, image_path)
                self._load_image_info(image_path)
                self.action_log.append({
                    "type": "screenshot",
                    "path": image_path,
                    "source": src,
                })
                return True

        # Otherwise generate a placeholder (size irrelevant for mock).
        img = Image.new("RGB", (1, 1), color=(240, 240, 240))
        img.save(image_path, "PNG")
        self._load_image_info(image_path)
        self.action_log.append({
            "type": "screenshot",
            "path": image_path,
            "source": "placeholder",
        })
        print(f"  [MOCK ADB] Screenshot → {image_path} (placeholder)")
        return True

    def click(self, x: int, y: int) -> None:
        self.action_log.append({"type": "click", "x": x, "y": y})
        print(f"  [MOCK ADB] Click ({x}, {y})")

    def type(self, text: str) -> None:
        self.action_log.append({"type": "type", "text": text})
        print(f"  [MOCK ADB] Type '{text}'")

    def clear_text(self) -> None:
        self.action_log.append({"type": "clear_text"})
        print(f"  [MOCK ADB] Clear text")

    def open_app(self, package_name: str) -> None:
        self.action_log.append({"type": "open_app", "package": package_name})
        print(f"  [MOCK ADB] Open app '{package_name}'")

    def back(self) -> None:
        self.action_log.append({"type": "back"})
        print(f"  [MOCK ADB] Back")

    def home(self) -> None:
        self.action_log.append({"type": "home"})
        print(f"  [MOCK ADB] Home")
