from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

import mss
import numpy as np

if sys.platform == "darwin":
    try:
        import mss.darwin as _mss_darwin

        # mss >= 10.1 defaults to capturing at "nominal" (1x / non-Retina)
        # resolution for speed — on a Retina display that's HALF the actual
        # pixel density, which often forces card.py's resize step to *upscale*
        # (blurry) instead of downscale (sharp) to reach the standard 420x587
        # card size. Disabling the nominal flag restores full physical-pixel
        # captures; the extra grab latency is a non-issue since we only grab
        # once per "W" press, not continuously.
        _mss_darwin.IMAGE_OPTIONS = 0
    except (ImportError, AttributeError):
        pass


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int


def grab_region(region: Region) -> np.ndarray:
    """Return BGR uint8 image (OpenCV native order)."""
    with mss.mss() as sct:
        box = {
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
        }
        shot = sct.grab(box)
        # mss's raw buffer is already BGRA (see mss.screenshot.ScreenShot.raw /
        # __array_interface__ docs), so dropping the alpha channel gives true
        # BGR directly. Reversing the channel order here (as this used to do)
        # actually flips it to RGB while every downstream consumer — the
        # thumbnail render, OCR's BGR2RGB prep, CLAHE in LAB space — assumes
        # real BGR, so that extra reversal was silently swapping red/blue
        # everywhere (e.g. red card text/art rendering cyan in the thumbnail).
        bgra = np.asarray(shot, dtype=np.uint8)
        return bgra[:, :, :3].copy()


def has_screen_recording_access() -> bool | None:
    """Check whether this process can capture *other apps'* window content.

    On macOS, without "Screen Recording" permission (System Settings ->
    Privacy & Security -> Screen Recording), CoreGraphics silently returns
    only the desktop wallpaper for any window/region it isn't allowed to
    see — there's no exception, it just looks like the app is "screenshotting
    the desktop" instead of the browser. Returns ``None`` on non-macOS
    platforms (the check doesn't apply) or if the check itself fails.
    """
    if sys.platform != "darwin":
        return None
    try:
        core = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/Versions/Current/CoreGraphics"
        )
        core.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(core.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return None  # e.g. macOS < 10.15, which has no such gate


def request_screen_recording_access() -> None:
    """Trigger macOS's native "Allow Screen Recording" permission prompt.

    Only shows a dialog the *first* time it's ever called for this app/binary;
    after that the user has to flip it on manually in System Settings, since
    macOS won't re-prompt once it's been denied.
    """
    if sys.platform != "darwin":
        return
    try:
        core = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/Versions/Current/CoreGraphics"
        )
        core.CGRequestScreenCaptureAccess()
    except (OSError, AttributeError):
        pass
