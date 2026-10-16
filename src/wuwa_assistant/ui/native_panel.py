"""Narrow Windows capture-exclusion boundary for our own Qt panel only.

No caller-supplied handles, discovery, game attachment, hooks or input generation.
The safety scanner permits only these two user32 functions in this exact module.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtWidgets import QWidget


class PanelCaptureExclusion:
    def __init__(self, panel: QWidget) -> None:
        self._panel = panel

    def enable(self) -> None:
        if sys.platform != "win32" or sys.getwindowsversion().build < 19041:
            raise RuntimeError("小面板需要 Windows 10 2004 或更新版本。")
        if not self._panel.isWindow():
            raise RuntimeError("小面板必須是工具自身的獨立視窗。")
        # Obtain only the window created by this QWidget; never discover a game.
        handle = wintypes.HWND(int(self._panel.winId()))
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
        if not user32.SetWindowDisplayAffinity(handle, 0x11):
            raise RuntimeError("無法將小面板排除於畫面擷取, 請改用雙螢幕模式。")
        affinity = wintypes.DWORD()
        if (
            not user32.GetWindowDisplayAffinity(handle, ctypes.byref(affinity))
            or affinity.value != 0x11
        ):
            raise RuntimeError("小面板的擷取排除設定未生效, 請改用雙螢幕模式。")
