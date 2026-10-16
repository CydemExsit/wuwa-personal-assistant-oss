from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QStyle,
    QStyleOptionButton,
    QWidget,
)

from wuwa_assistant.ui.score_colors import apply_score_color
from wuwa_assistant.ui.theme import apply_dark_theme, load_dark_stylesheet


def _indicator_colors(widget: QCheckBox | QRadioButton) -> set[tuple[int, int, int]]:
    option = QStyleOptionButton()
    option.initFrom(widget)
    rect = widget.style().subElementRect(
        QStyle.SubElement.SE_CheckBoxIndicator
        if isinstance(widget, QCheckBox)
        else QStyle.SubElement.SE_RadioButtonIndicator,
        option,
        widget,
    )
    image = widget.grab(rect).toImage()
    return {
        (
            image.pixelColor(x, y).red(),
            image.pixelColor(x, y).green(),
            image.pixelColor(x, y).blue(),
        )
        for y in range(image.height())
        for x in range(image.width())
    }


def test_dark_stylesheet_has_no_invalid_selector_or_unresolved_asset() -> None:
    stylesheet = load_dark_stylesheet()

    assert ":not(" not in stylesheet
    assert "__WUWA_CHECKMARK_ASSET__" not in stylesheet
    assert "checkbox-check.svg" in stylesheet
    assert "QCheckBox::indicator:checked" in stylesheet
    assert "QRadioButton::indicator:checked" in stylesheet


def test_checkbox_and_radio_indicators_are_visibly_distinct_offscreen(
    qapp: QApplication,
) -> None:
    container = QWidget()
    apply_dark_theme(container)
    unchecked = QCheckBox("未勾選")
    checked = QCheckBox("已勾選")
    checked.setChecked(True)
    radio = QRadioButton("已選取")
    radio.setChecked(True)
    layout = QHBoxLayout(container)
    layout.addWidget(unchecked)
    layout.addWidget(checked)
    layout.addWidget(radio)
    container.show()
    qapp.processEvents()
    try:
        unchecked_colors = _indicator_colors(unchecked)
        checked_colors = _indicator_colors(checked)
        radio_colors = _indicator_colors(radio)

        assert unchecked_colors != checked_colors
        assert any(sum(color) > 320 for color in unchecked_colors)
        assert any(red > 190 and blue > 190 and red > green for red, green, blue in checked_colors)
        assert any(red > 190 and blue > 190 and red > green for red, green, blue in radio_colors)
    finally:
        container.close()


def _luminance(color: QColor) -> float:
    channels = (color.redF(), color.greenF(), color.blueF())
    linear = tuple(
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def test_dark_theme_surfaces_and_score_text_have_readable_contrast(qapp: QApplication) -> None:
    container = QWidget()
    apply_dark_theme(container)
    layout = QHBoxLayout(container)
    widgets = [QLineEdit("文字輸入"), QPushButton("主要操作")]
    widgets[1].setProperty("role", "primary")
    for score in (0, 40, 55, 65, 75, 85, 92, 100, None):
        label = QLabel("55.5")
        label.setObjectName("EquipmentSlotScore")
        apply_score_color(label, score)
        widgets.append(label)
    for widget in widgets:
        layout.addWidget(widget)
    container.show()
    qapp.processEvents()
    try:
        assert _luminance(container.palette().window().color()) < 0.04
        assert _luminance(widgets[0].palette().base().color()) < 0.04
        for widget in widgets:
            palette = widget.palette()
            foreground = _luminance(palette.color(widget.foregroundRole()))
            background = _luminance(palette.color(widget.backgroundRole()))
            contrast = (max(foreground, background) + 0.05) / (min(foreground, background) + 0.05)
            assert contrast >= 4.5, (widget.objectName(), contrast)
    finally:
        container.close()


def test_score_and_secondary_text_stay_readable_on_selected_equipment_cards(
    qapp: QApplication,
) -> None:
    container = QWidget()
    apply_dark_theme(container)
    layout = QHBoxLayout(container)
    cards = []
    for selected in (False, True):
        card = QFrame()
        card.setObjectName("EquipmentCharacterCard")
        card.setProperty("selected", selected)
        row = QHBoxLayout(card)
        for score in (0, 40, 55, 65, 75, 85, 92, 100, None):
            label = QLabel("55.5")
            label.setObjectName("EquipmentSlotScore")
            apply_score_color(label, score)
            row.addWidget(label)
        secondary = QLabel("總分 / 500")
        secondary.setObjectName("PageDescription")
        row.addWidget(secondary)
        layout.addWidget(card)
        cards.append(card)
    container.show()
    qapp.processEvents()
    try:
        assert cards[0].palette().window().color() != cards[1].palette().window().color()
        for card in cards:
            background = _luminance(card.palette().window().color())
            for label in card.findChildren(QLabel):
                foreground = _luminance(label.palette().windowText().color())
                assert (foreground + 0.05) / (background + 0.05) >= 4.5
    finally:
        container.close()
