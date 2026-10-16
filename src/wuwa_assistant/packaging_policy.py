"""Explicitly unsupported optional native payload in the desktop release."""

from __future__ import annotations

from pathlib import PurePosixPath


def excluded_binary(relative: str) -> bool:
    name = PurePosixPath(relative.replace("\\", "/")).name.lower()
    return name in {
        "qt6virtualkeyboard.dll",
        "qtvirtualkeyboardplugin.dll",
        "qpdf.dll",
        "qt6pdf.dll",
    } or name.startswith(("qt6qml", "qt6quick", "opencv_videoio_ffmpeg"))
