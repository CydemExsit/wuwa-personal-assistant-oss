from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QColor, QImage

from tests.gui.test_echo_live_page import _observation, _page
from wuwa_assistant.ui import character_portraits as portraits
from wuwa_assistant.ui.pages.echo_live import CorrectionDialog, RecommendationView
from wuwa_assistant.ui.widgets.expandable_note import ExpandableNote


def test_review_drafts_survive_field_switch(qapp):
    dialog = CorrectionDialog({"/name": "original", "/level": "20"})
    dialog.corrected_value_edit.setText("draft")
    dialog.field_combo.setCurrentIndex(1)
    dialog.corrected_value_edit.setText("21")
    dialog.field_combo.setCurrentIndex(0)
    assert dialog.corrected_value == "draft"
    assert dialog.previous_value_label.text() == "original"
    dialog.field_combo.setCurrentIndex(1)
    assert dialog.corrected_value == "21"
    dialog.close()


def test_review_save_stays_bound_to_inspected_observation(qapp):
    page = _page()
    original = _observation()
    page.apply_observation(original)
    requests = []
    page.correction_requested.connect(requests.append)

    def save_after_new_scan():
        dialog = page.findChild(CorrectionDialog)
        assert dialog.field_path == "/sub_stats/0/value"
        page.apply_observation(replace(original, observation_id="new-scan"))
        dialog.corrected_value_edit.setText("8.1")
        dialog.accept()

    QTimer.singleShot(0, save_after_new_scan)
    page._open_correction_dialog("/sub_stats/0/value")
    assert len(requests) == 1
    assert requests[0].observation_id == original.observation_id
    assert requests[0].previous_value == "7.5"
    assert requests[0].corrected_value == "8.1"
    page.close()


def test_pending_scan_cannot_open_correction_by_table_shortcut(qapp):
    page = _page()
    page.apply_observation(_observation())
    page.set_analysis_pending(True)
    page._open_correction_dialog("/name")
    assert page.findChild(CorrectionDialog) is None
    page.close()


def test_condition_is_cleared_with_observation(qapp):
    page = _page()
    page.apply_observation(_observation())
    page.apply_recommendation(RecommendationView(target_condition="condition"))
    assert page.target_condition_label.text() == "condition"
    page.clear_observation()
    assert not page.target_condition_label.text()
    assert page.target_condition_label.isHidden()
    page.close()


def test_help_expands_without_losing_details(qapp):
    note = ExpandableNote("summary", "complete instructions")
    assert note.body.isHidden()
    note.button.click()
    assert not note.body.isHidden()
    assert note.text() == "complete instructions"
    note.setText("new status\ndetail")
    assert note.summary.text() == "new status"
    assert note.text() == "new status\ndetail"
    note.close()


@pytest.fixture
def local_portraits(tmp_path, qapp):
    portraits.configure_local_portraits(tmp_path / "edition-a")
    yield tmp_path
    portraits.configure_local_portraits(None)


def synthetic_image(path: Path, width=400, height=200):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#2244aa"))
    image.setText("private metadata", "must not be stored")
    assert image.save(str(path))


def test_local_portrait_persists_and_refreshes_existing_labels(local_portraits):
    root = local_portraits
    source = root / "synthetic.png"
    synthetic_image(source)
    label = portraits.CharacterPortraitLabel("測試角色", 48)
    before = label.pixmap().toImage()
    portraits.import_local_portrait("測試角色", source)
    assert portraits.has_character_portrait("測試角色")
    assert label.pixmap().toImage() != before
    saved = next((root / "edition-a").glob("*.png"))
    image = QImage(str(saved))
    assert image.size() == QSize(256, 256)
    assert not image.textKeys()
    assert source.exists()
    portraits.configure_local_portraits(root / "edition-b")
    assert not portraits.has_character_portrait("測試角色")
    portraits.configure_local_portraits(root / "edition-a")
    assert portraits.has_character_portrait("測試角色")
    assert not portraits.has_character_portrait("測試角色二")
    portraits.remove_local_portrait("測試角色")
    assert label.pixmap().toImage() == before
    label.close()


@pytest.mark.parametrize("kind", ["invalid", "huge", "svg"])
def test_rejected_portrait_does_not_replace_current_image(local_portraits, kind):
    root = local_portraits
    good = root / "good.png"
    synthetic_image(good)
    portraits.import_local_portrait("測試角色", good)
    expected = portraits.character_portrait("測試角色").toImage()
    source = root / "bad.png"
    if kind == "huge":
        synthetic_image(source, 4097, 1)
    elif kind == "svg":
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    else:
        source.write_text("invalid image")
    with pytest.raises(ValueError):
        portraits.import_local_portrait("測試角色", source)
    assert portraits.character_portrait("測試角色").toImage() == expected
