from __future__ import annotations

from dataclasses import dataclass

try:
    import win32gui
except ImportError:
    win32gui = None  # type: ignore[misc, assignment]


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def find_window_rect(title_contains: str) -> WindowRect | None:
    """Return client-area screen rect for the first top-level window whose title matches."""
    if win32gui is None:
        return None

    target = title_contains.casefold()
    found: list[int] = []

    def callback(hwnd: int, _: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if target in title.casefold():
            found.append(hwnd)

    win32gui.EnumWindows(callback, None)
    if not found:
        return None

    hwnd = found[0]
    rect = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (rect[0], rect[1]))
    right, bottom = win32gui.ClientToScreen(hwnd, (rect[2], rect[3]))
    return WindowRect(left=left, top=top, right=right, bottom=bottom)
