"""Offline character initials; the OSS edition redistributes no game artwork.

Only separator/spacing variants are normalized. A newly recognized name never
borrows another character's identity. All resources are bundled; rendering does
not read game files, inspect screenshots, or contact the network.
"""

from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QEvent, QIODevice, QRectF, QSaveFile, Qt
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from wuwa_assistant.models.names import canonical_character_name, character_name_key

_RESOURCE_PACKAGE = "wuwa_assistant.data.characters"
_FALLBACK_COLORS = ("#31536b", "#554569", "#345d56", "#6b4c43", "#575b3e")


_local_portrait_directory: Path | None = None


def configure_local_portraits(directory: Path | None) -> None:
    """Use only this edition's explicitly supplied data root; never scan other apps."""
    global _local_portrait_directory
    _local_portrait_directory = directory
    _refresh_portraits()


def _local_file(name: str) -> Path:
    if _local_portrait_directory is None:
        raise ValueError("尚未設定本機頭像目錄")
    key = character_name_key(name)
    if not key:
        raise ValueError("請選擇角色")
    return _local_portrait_directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".png")


def import_local_portrait(name: str, source: Path) -> None:
    """Decode a bounded local raster, center-crop and atomically save pixels only."""
    target = _local_file(name)
    if source.stat().st_size > 12 * 1024 * 1024:
        raise ValueError("圖片需小於 12 MB")
    reader = QImageReader(str(source))
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or max(size.width(), size.height()) > 4096:
        raise ValueError("圖片需在 4096 x 4096 以內")
    if bytes(reader.format().data()).lower() not in (b"png", b"jpg", b"jpeg", b"webp"):
        raise ValueError("請選擇 PNG、JPEG 或 WebP 圖片")
    image = reader.read()
    if image.isNull():
        raise ValueError("無法讀取圖片")
    side = min(image.width(), image.height())
    image = image.copy((image.width() - side) // 2, (image.height() - side) // 2, side, side)
    image = image.scaled(
        256, 256, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    # Repaint into a fresh image to omit source text, EXIF and other metadata.
    clean = QImage(256, 256, QImage.Format.Format_ARGB32)
    clean.fill(Qt.GlobalColor.transparent)
    painter = QPainter(clean)
    painter.drawImage(0, 0, image)
    painter.end()
    target.parent.mkdir(parents=True, exist_ok=True)
    output = QSaveFile(str(target))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError("無法儲存頭像")
    if not clean.save(output, "PNG") or not output.commit():  # type: ignore[call-overload]
        raise OSError("頭像儲存失敗")
    _refresh_portraits()


def remove_local_portrait(name: str) -> None:
    _local_file(name).unlink(missing_ok=True)
    _refresh_portraits()


def _refresh_portraits() -> None:
    _portrait_image.cache_clear()
    _render_portrait.cache_clear()
    if QApplication.instance() is not None:
        for widget in QApplication.allWidgets():
            if isinstance(widget, CharacterPortraitLabel):
                widget._update_portrait()
            widget.update()


@lru_cache(maxsize=1)
def _portrait_files() -> dict[str, str]:
    try:
        raw = files(_RESOURCE_PACKAGE).joinpath("portraits", "manifest.json").read_text("utf-8")
        manifest = json.loads(raw)
        entries = manifest.get("portraits", [])
    except (OSError, ValueError, AttributeError):
        return {}
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        filename = entry.get("file")
        if (
            isinstance(name, str)
            and isinstance(filename, str)
            and filename.endswith(".png")
            and "/" not in filename
            and "\\" not in filename
        ):
            result[character_name_key(name)] = filename
    return result


@lru_cache(maxsize=64)
def _portrait_image(name_key: str) -> QImage:
    if _local_portrait_directory is not None and name_key:
        path = _local_file(name_key)
        if path.is_file() and path.stat().st_size <= 1024 * 1024:
            reader = QImageReader(str(path))
            if reader.size().width() == 256 and reader.size().height() == 256:
                local = reader.read()
                if not local.isNull():
                    return local
    filename = _portrait_files().get(name_key)
    if filename is None:
        return QImage()
    try:
        data = files(_RESOURCE_PACKAGE).joinpath("portraits", filename).read_bytes()
    except OSError:
        return QImage()
    return QImage.fromData(data)


def has_character_portrait(name: str) -> bool:
    """Whether the exact character has a readable, explicitly bundled portrait."""

    return not _portrait_image(character_name_key(name)).isNull()


@lru_cache(maxsize=128)
def _render_portrait(name: str, size: int, device_pixel_ratio: float) -> QPixmap:
    name_key = character_name_key(name)
    source = _portrait_image(name_key)
    physical_size = math.ceil(size * device_pixel_ratio)
    pixmap = QPixmap(physical_size, physical_size)
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    rect = QRectF(0, 0, size, size)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        shape = QPainterPath()
        shape.addRoundedRect(rect, size * 0.17, size * 0.17)
        painter.setClipPath(shape)
        if not source.isNull():
            painter.fillRect(rect, QColor("#3b294c"))
            # Prefilter the full-resolution artwork before painting. QPainter's
            # bilinear transform alone can alias fine hair/eye detail when a
            # 256px source is reduced to a small roster icon.
            scaled = source.scaled(
                physical_size,
                physical_size,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawImage(rect, scaled)
        else:
            color_index = hashlib.sha256(name_key.encode("utf-8")).digest()[0]
            color = _FALLBACK_COLORS[color_index % len(_FALLBACK_COLORS)]
            painter.fillRect(rect, QColor(color))
            font = QFont("Microsoft JhengHei")
            font.setPixelSize(max(1, round(size * 0.31)))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#f5edf8"))
            initials = name.replace("·", "")[:2] or "?"
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, initials)
    finally:
        painter.end()
    return pixmap


def character_portrait(name: str, size: int = 72, *, device_pixel_ratio: float = 1.0) -> QPixmap:
    """Return a cached rounded portrait or initials, called on the Qt GUI thread.

    The returned pixmap is a detached wrapper so callers can alter it without
    changing later cards. ``size`` is the logical layout size; the render target's
    pixel ratio selects the physical resolution. A QApplication must already exist.
    """

    if size < 1 or size > 512:
        raise ValueError("Portrait size must be between 1 and 512 pixels")
    if not math.isfinite(device_pixel_ratio) or not 0 < device_pixel_ratio <= 4:
        raise ValueError("Portrait device pixel ratio must be finite and within (0, 4]")
    return QPixmap(_render_portrait(canonical_character_name(name), size, device_pixel_ratio))


class CharacterPortraitLabel(QLabel):
    """Keep offline portraits sharp when shown or moved between scaled screens."""

    def __init__(self, name: str, size: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._portrait_name = name
        self._portrait_size = size
        self.setObjectName("CharacterPortrait")
        self.setFixedSize(size, size)
        self._update_portrait()

    def set_character_name(self, name: str) -> None:
        if name != self._portrait_name:
            self._portrait_name = name
            self._update_portrait()

    def _update_portrait(self) -> None:
        self.setPixmap(
            character_portrait(
                self._portrait_name,
                self._portrait_size,
                device_pixel_ratio=self.devicePixelRatioF(),
            )
        )

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if hasattr(self, "_portrait_name") and event.type() in {
            QEvent.Type.Show,
            QEvent.Type.ParentChange,
            QEvent.Type.DevicePixelRatioChange,
            QEvent.Type.ScreenChangeInternal,
        }:
            self._update_portrait()
        return result
