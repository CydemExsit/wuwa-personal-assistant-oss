"""Regenerate the project-owned ICO from simple MIT-licensed SVG geometry."""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance() or QGuiApplication([])
    styles = Path(__file__).resolve().parents[1] / "src/wuwa_assistant/ui/styles"
    renderer = QSvgRenderer(str(styles / "app.svg"))
    if not renderer.isValid():
        raise RuntimeError("Project SVG icon is invalid")
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Could not encode project icon")
    with Image.open(io.BytesIO(bytes(data))) as bitmap:
        bitmap.save(styles / "app.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    app.processEvents()


if __name__ == "__main__":
    main()
