from __future__ import annotations

import json
from importlib.resources import files

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRect, QSize
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QStyleOptionViewItem

from wuwa_assistant.ui.character_portraits import (
    CharacterPortraitLabel,
    character_portrait,
    has_character_portrait,
)
from wuwa_assistant.ui.character_roster import CharacterRosterDelegate


def test_oss_distribution_omits_game_art_and_renders_known_names_as_initials() -> None:
    resources = files("wuwa_assistant.data.characters").joinpath("portraits")
    manifest = json.loads(resources.joinpath("manifest.json").read_text("utf-8"))
    assert manifest["portraits"] == []
    assert not any(path.name.endswith(".png") for path in resources.iterdir())
    for name in {
        "愛彌斯",
        "清宵",
        "秧秧·玄翎",
        "穗穗",
        "緋雪",
        "莫寧",
        "達妮婭",
        "洛瑟菈",
        "琳奈",
        "露西",
        "蕾貝卡",
        "嘉貝莉娜",
        "露帕",
        "卡提希婭",
        "珂萊塔",
        "守岸人",
        "折枝",
        "維里奈",
        "千咲",
        "心",
    }:
        assert not has_character_portrait(name)
        assert not character_portrait(name).isNull()


@pytest.mark.parametrize("name", ["秧秧·玄翎", "秧秧・玄翎", "秧秧玄翎", " 秧秧 玄翎 "])
def test_portrait_normalizes_separators_without_guessing_character_identity(name: str) -> None:
    assert not has_character_portrait(name)
    assert character_portrait(name).toImage() == character_portrait("秧秧·玄翎").toImage()
    assert not has_character_portrait("秧秧")
    assert not has_character_portrait("秧秧·其他")
    assert not has_character_portrait("愛彌")


@pytest.mark.parametrize("size", [48, 72, 76])
def test_unknown_character_has_consistent_nonempty_initials_fallback(size: int) -> None:
    unknown = character_portrait("未來角色", size)
    assert not unknown.isNull()
    assert unknown.size() == QSize(size, size)
    assert unknown.toImage() == character_portrait("未來角色", size).toImage()
    assert unknown.toImage() != character_portrait("另一角色", size).toImage()
    assert unknown.toImage() != character_portrait("愛彌斯", size).toImage()


def test_mutating_returned_pixmap_does_not_change_cached_portrait() -> None:
    first = character_portrait("緋雪", 76)
    expected = first.toImage()
    first.fill(QColor("red"))
    assert character_portrait("緋雪", 76).toImage() == expected
    assert first.toImage() != expected


@pytest.mark.parametrize("ratio", [1.0, 1.25, 1.5, 1.75, 2.0, 3.0])
@pytest.mark.parametrize("name", ["千咲", "未來角色"])
def test_scaled_screens_get_more_pixels_without_enlarging_portrait_layout(
    name: str, ratio: float
) -> None:
    portrait = character_portrait(name, 48, device_pixel_ratio=ratio)
    assert portrait.size() == QSize(round(48 * ratio), round(48 * ratio))
    assert portrait.devicePixelRatioF() == ratio
    assert portrait.deviceIndependentSize().width() == 48
    assert portrait.deviceIndependentSize().height() == 48
    assert portrait.toImage().pixelColor(0, 0).alpha() == 0
    assert portrait.toImage().pixelColor(round(24 * ratio), round(24 * ratio)).alpha() == 255
    assert character_portrait(name, 48).devicePixelRatioF() == 1.0


@pytest.mark.parametrize("ratio", [0, -1, float("inf"), float("nan"), 4.1])
def test_portrait_pixel_ratio_rejects_invalid_or_unbounded_render_targets(ratio: float) -> None:
    with pytest.raises(ValueError, match="device pixel ratio"):
        character_portrait("千咲", device_pixel_ratio=ratio)


def test_portrait_label_refreshes_pixels_when_screen_scale_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratio = 1.0
    sent: list[QPixmap] = []
    original_set_pixmap = CharacterPortraitLabel.setPixmap

    def record_pixmap(label: CharacterPortraitLabel, pixmap: QPixmap) -> None:
        sent.append(QPixmap(pixmap))
        original_set_pixmap(label, pixmap)

    monkeypatch.setattr(CharacterPortraitLabel, "devicePixelRatioF", lambda self: ratio)
    monkeypatch.setattr(CharacterPortraitLabel, "setPixmap", record_pixmap)
    label = CharacterPortraitLabel("千咲", 72)
    assert label.pixmap().size() == QSize(72, 72)
    ratio = 2.0
    QCoreApplication.sendEvent(label, QEvent(QEvent.Type.DevicePixelRatioChange))
    # The mocked Python ratio does not change the native offscreen screen. Check
    # the new backing pixels delivered to Qt, before QLabel selects screen DPI.
    assert sent[-1].size() == QSize(144, 144)
    assert sent[-1].devicePixelRatioF() == 2.0
    assert label.size() == QSize(72, 72)
    label.close()


def test_roster_paints_native_resolution_portrait_on_a_scaled_target() -> None:
    model = QStandardItemModel()
    model.appendRow([QStandardItem("千咲"), QStandardItem("湮滅")])
    image = QImage(640, 152, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(2)
    image.fill(QColor("black"))
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 320, 76)
    painter = QPainter(image)
    try:
        CharacterRosterDelegate().paint(painter, option, model.index(0, 0))
    finally:
        painter.end()
    # Compare the actual painted interior to full-resolution pixels. Rendering a
    # 48px pixmap and magnifying it would lose detail even if its layout fits.
    expected = character_portrait("千咲", 48, device_pixel_ratio=2).toImage()
    interior = image.copy(QRect(42, 42, 64, 64)).convertToFormat(QImage.Format.Format_ARGB32)
    native = expected.copy(QRect(16, 16, 64, 64)).convertToFormat(QImage.Format.Format_ARGB32)
    assert interior == native


def test_small_portrait_prefilters_fine_detail_instead_of_aliasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wuwa_assistant.ui import character_portraits

    source = QImage(256, 256, QImage.Format.Format_ARGB32)
    source.fill(QColor("black"))
    painter = QPainter(source)
    try:
        for x in range(0, 256, 2):
            painter.fillRect(QRect(x, 0, 1, 256), QColor("white"))
    finally:
        painter.end()
    monkeypatch.setattr(character_portraits, "_portrait_image", lambda _key: source)
    character_portraits._render_portrait.cache_clear()
    try:
        result = character_portrait("千咲", 48).toImage()
        # Fine alternating lines should average to gray, rather than produce
        # high-contrast false stripes when reduced by more than five times.
        assert all(100 <= result.pixelColor(x, 24).red() <= 155 for x in range(8, 40))
    finally:
        character_portraits._render_portrait.cache_clear()


@pytest.mark.parametrize("size", [0, -1, 513])
def test_portrait_size_is_bounded(size: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 512"):
        character_portrait("緋雪", size)


def test_missing_portrait_asset_uses_initials_instead_of_a_broken_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wuwa_assistant.ui import character_portraits

    character_portraits._render_portrait.cache_clear()
    monkeypatch.setattr(character_portraits, "_portrait_image", lambda _key: QImage())
    try:
        fallback = character_portrait("緋雪", 76)
        assert not fallback.isNull()
        assert not has_character_portrait("緋雪")
    finally:
        character_portraits._render_portrait.cache_clear()
