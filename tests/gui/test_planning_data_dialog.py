from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from wuwa_assistant.ui.dialogs.planning_data import (
    CultivationTargetSaveRequest,
    CultivationTargetTableModel,
    CultivationTargetView,
    MaterialSaveRequest,
    MaterialTableModel,
    MaterialView,
    PlanningCharacterChoiceView,
    PlanningDataBundle,
    PlanningDataDialog,
    RequiredMaterialRequest,
    RequiredMaterialTableModel,
    RequiredMaterialView,
)


def _materials() -> tuple[MaterialView, ...]:
    return (
        MaterialView(
            "material-skill",
            "skill-high",
            "技能素材(高級)",
            "技能素材",
            40,
            2.5,
            (1, 4),
            None,
            "manual_unverified",
            "2",
            8,
            "等待更多實際掉落紀錄",
        ),
        MaterialView(
            "material-weekly",
            "weekly-boss",
            "週本素材",
            "每週限次",
            60,
            1,
            (),
            3,
            "manual_package",
            "manual-2026-08",
            4,
        ),
    )


def _targets() -> tuple[CultivationTargetView, ...]:
    return (
        CultivationTargetView(
            "target-1",
            "character-1",
            "緋雪",
            "skill_level",
            "resonance_skill",
            '{"level": 8}',
            '{"level": 10}',
            1,
            False,
            date(2026, 8, 8),
            "核心技能優先",
            (RequiredMaterialView("material-skill", "技能素材(高級)", 20),),
        ),
        CultivationTargetView(
            "target-2",
            None,
            None,
            "custom",
            "weekly-stock",
            "4",
            "6",
            5,
            True,
            None,
        ),
    )


def _bundle() -> PlanningDataBundle:
    return PlanningDataBundle(
        materials=_materials(),
        characters=(
            PlanningCharacterChoiceView("character-1", "緋雪"),
            PlanningCharacterChoiceView("character-2", "秧秧"),
        ),
        targets=_targets(),
    )


def test_planning_models_expose_stable_ids_sort_roles_and_edit_quantities(
    qapp: QApplication,
) -> None:
    del qapp
    materials = MaterialTableModel()
    materials.set_rows(_materials())
    assert materials.rowCount() == 2
    assert materials.columnCount() == 8
    assert materials.data(materials.index(0, 0)) == "skill-high"
    assert materials.data(materials.index(0, 4)) == "一、四"
    assert materials.data(materials.index(1, 4)) == "未設定 / 未知"
    assert materials.data(materials.index(0, 2), int(Qt.ItemDataRole.UserRole)) == "material-skill"
    assert materials.data(materials.index(0, 6), int(Qt.ItemDataRole.EditRole)) == "2"
    assert materials.row_for("material-weekly") == 1
    assert materials.identifier_at(99) is None

    targets = CultivationTargetTableModel()
    targets.set_rows(_targets())
    assert targets.data(targets.index(0, 0)) == "緋雪"
    assert targets.data(targets.index(1, 0)) == "全帳號 / 未指定"
    assert targets.data(targets.index(0, 4)) == "2026-08-08"
    assert targets.data(targets.index(1, 5)) == "已完成"
    assert targets.data(targets.index(0, 0), int(Qt.ItemDataRole.UserRole)) == "target-1"

    required = RequiredMaterialTableModel()
    required.set_rows((RequiredMaterialView("material-skill", "技能素材", 10),))
    quantity = required.index(0, 1)
    assert required.flags(quantity) & Qt.ItemFlag.ItemIsEditable
    assert required.setData(quantity, "15")
    assert required.data(quantity) == 15
    assert required.setData(quantity, 0)
    assert required.setData(quantity, "12.34567")
    assert required.data(quantity) == 12.3457
    assert required.add_row(RequiredMaterialView("material-weekly", "週本素材", 3))
    assert required.rowCount() == 2
    assert required.remove_row(0)
    assert required.rows()[0].material_id == "material-weekly"


def test_dialog_is_modeless_loads_bundle_and_selects_into_forms(qapp: QApplication) -> None:
    dialog = PlanningDataDialog()
    selected_materials: list[str] = []
    selected_targets: list[str] = []
    dialog.material_selected.connect(selected_materials.append)
    dialog.target_selected.connect(selected_targets.append)
    dialog.set_bundle(_bundle())
    dialog.show()
    qapp.processEvents()

    assert dialog.isModal() is False
    assert dialog.material_model.rowCount() == 2
    assert dialog.target_model.rowCount() == 2
    assert dialog.target_character_combo.count() == 3
    assert dialog.required_material_combo.count() == 2
    assert "2 項素材" in dialog.status_label.text()

    dialog.material_table.setCurrentIndex(dialog.material_model.index(0, 0))
    qapp.processEvents()
    assert selected_materials == ["material-skill"]
    assert dialog.material_code_edit.text() == "skill-high"
    assert dialog.material_waveplate_spin.value() == 40
    assert dialog.material_weekday_checks[0].isChecked()
    assert dialog.material_weekday_checks[3].isChecked()
    assert dialog.material_inventory_spin.value() == 8

    dialog.target_table.setCurrentIndex(dialog.target_model.index(0, 0))
    qapp.processEvents()
    assert selected_targets == ["target-1"]
    assert dialog.target_character_combo.currentData() == "character-1"
    assert dialog.target_key_edit.text() == "resonance_skill"
    assert dialog.target_due_enabled.isChecked()
    assert dialog.required_material_model.rowCount() == 1

    QTest.mouseClick(dialog.target_clear_button, Qt.MouseButton.LeftButton)
    assert dialog.target_key_edit.text() == ""
    assert dialog.required_material_model.rowCount() == 0
    assert not dialog.target_archive_button.isEnabled()
    dialog.close()


def test_imported_material_provenance_round_trips_without_silent_rewrite(
    qapp: QApplication,
) -> None:
    dialog = PlanningDataDialog()
    imported = _materials()[1]
    dialog.set_bundle(_bundle())
    dialog.apply_material(imported)
    dialog.show()
    qapp.processEvents()

    request = dialog.build_material_request()

    assert request.source_status == "manual_package"
    assert request.version == "manual-2026-08"
    assert dialog.material_source_combo.currentText() == "本機匯入包"
    assert dialog.material_version_edit.text() == "manual-2026-08"
    dialog.close()


def test_material_create_and_update_emit_complete_immutable_requests(
    qapp: QApplication,
) -> None:
    dialog = PlanningDataDialog()
    saves: list[MaterialSaveRequest] = []
    dialog.material_save_requested.connect(saves.append)
    dialog.set_bundle(_bundle())
    dialog.show()
    qapp.processEvents()

    QTest.mouseClick(dialog.material_new_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog.material_save_button, Qt.MouseButton.LeftButton)
    assert saves == []
    assert dialog.status_label.property("error") is True
    assert "代碼不可空白" in dialog.status_label.text()

    dialog.material_code_edit.setText("weapon-exp")
    dialog.material_name_edit.setText("武器經驗")
    dialog.material_category_edit.setText("武器養成")
    dialog.material_waveplate_spin.setValue(40)
    dialog.material_average_spin.setValue(3.25)
    dialog.material_weekday_checks[1].setChecked(True)
    dialog.material_weekday_checks[4].setChecked(True)
    dialog.material_weekly_limit_enabled_check.setChecked(True)
    dialog.material_weekly_limit_spin.setValue(6)
    dialog.material_source_combo.setCurrentIndex(
        dialog.material_source_combo.findData("user_observed")
    )
    dialog.material_version_edit.setText("manual-2026-08")
    dialog.material_inventory_confirmed_check.setChecked(True)
    dialog.material_inventory_spin.setValue(12)
    dialog.material_notes_edit.setPlainText("本機手動維護")
    QTest.mouseClick(dialog.material_save_button, Qt.MouseButton.LeftButton)

    assert saves == [
        MaterialSaveRequest(
            None,
            "weapon-exp",
            "武器經驗",
            "武器養成",
            40,
            3.25,
            (2, 5),
            6,
            "user_observed",
            "manual-2026-08",
            12,
            "本機手動維護",
        )
    ]

    dialog.apply_material(_materials()[0])
    dialog.material_inventory_spin.setValue(15)
    assert dialog.build_material_request().identifier == "material-skill"
    assert dialog.build_material_request().inventory_count == 15

    unknown = MaterialView(
        "unknown-material",
        "unknown",
        "尚未盤點",
        "測試",
        40,
        2,
        (1,),
        None,
        "manual_package",
        "manual-2026-08",
        None,
    )
    dialog.apply_material(unknown)
    assert not dialog.material_inventory_confirmed_check.isChecked()
    assert not dialog.material_inventory_spin.isEnabled()
    assert dialog.build_material_request().inventory_count is None
    dialog.close()


def test_material_form_round_trips_zero_limits_fractional_values_and_custom_provenance(
    qapp: QApplication,
) -> None:
    dialog = PlanningDataDialog()
    material = MaterialView(
        "material-zero-limit",
        "zero-limit",
        "零上限素材",
        "測試",
        0,
        3.1416,
        (),
        0,
        "local-curation-v2",
        None,
        12.3457,
    )
    dialog.set_bundle(PlanningDataBundle(materials=(material,)))
    dialog.apply_material(material)
    dialog.show()
    qapp.processEvents()

    assert dialog.material_average_spin.value() == 3.1416
    assert dialog.material_weekly_limit_enabled_check.isChecked()
    assert dialog.material_weekly_limit_spin.value() == 0
    assert dialog.material_inventory_spin.value() == 12.3457
    request = dialog.build_material_request()
    assert request.weekly_limit == 0
    assert request.average_output == 3.1416
    assert request.inventory_count == 12.3457
    assert request.source_status == "local-curation-v2"
    assert request.version is None
    dialog.close()


def test_target_create_emits_character_values_due_date_and_material_requirements(
    qapp: QApplication,
) -> None:
    dialog = PlanningDataDialog()
    saves: list[CultivationTargetSaveRequest] = []
    dialog.target_save_requested.connect(saves.append)
    dialog.set_bundle(_bundle())
    dialog.show()
    qapp.processEvents()
    dialog.tabs.setCurrentIndex(1)
    QTest.mouseClick(dialog.target_new_button, Qt.MouseButton.LeftButton)

    dialog.target_character_combo.setCurrentIndex(
        dialog.target_character_combo.findData("character-2")
    )
    dialog.target_type_combo.setCurrentIndex(dialog.target_type_combo.findData("weapon_level"))
    dialog.target_key_edit.setText("main_weapon")
    dialog.target_current_edit.setPlainText("80")
    dialog.target_value_edit.setPlainText("90")
    dialog.target_priority_spin.setValue(2)
    dialog.target_due_enabled.setChecked(True)
    dialog.target_due_date_edit.setDate(dialog.target_due_date_edit.date().addDays(5))
    dialog.target_notes_edit.setPlainText("先完成主力武器")
    dialog.required_material_combo.setCurrentIndex(
        dialog.required_material_combo.findData("material-skill")
    )
    dialog.required_material_quantity_spin.setValue(20)
    QTest.mouseClick(dialog.required_material_add_button, Qt.MouseButton.LeftButton)
    assert dialog.required_material_model.rowCount() == 1
    QTest.mouseClick(dialog.target_save_button, Qt.MouseButton.LeftButton)

    assert len(saves) == 1
    request = saves[0]
    assert request.identifier is None
    assert request.character_id == "character-2"
    assert request.target_type == "weapon_level"
    assert request.target_key == "main_weapon"
    assert request.current_value == "80"
    assert request.target_value == "90"
    assert request.priority == 2
    assert request.due_date is not None
    assert request.notes == "先完成主力武器"
    assert request.required_materials == (RequiredMaterialRequest("material-skill", 20),)

    dialog.target_type_combo.setEditText("custom-versioned-target")
    assert dialog.build_target_request().target_type == "custom-versioned-target"

    dialog.clear_target_form()
    QTest.mouseClick(dialog.target_save_button, Qt.MouseButton.LeftButton)
    assert len(saves) == 1
    assert dialog.status_label.property("error") is True
    assert "目標值不可空白" in dialog.status_label.text()
    dialog.close()


def test_archive_intents_require_explicit_confirmation(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = PlanningDataDialog()
    material_archives: list[str] = []
    target_archives: list[str] = []
    dialog.material_archive_requested.connect(material_archives.append)
    dialog.target_archive_requested.connect(target_archives.append)
    dialog.set_bundle(_bundle())
    dialog.apply_material(_materials()[0])
    dialog.apply_target(_targets()[0])
    dialog.show()
    qapp.processEvents()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )
    QTest.mouseClick(dialog.material_archive_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog.target_archive_button, Qt.MouseButton.LeftButton)
    assert material_archives == []
    assert target_archives == []

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    QTest.mouseClick(dialog.material_archive_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog.target_archive_button, Qt.MouseButton.LeftButton)
    assert material_archives == ["material-skill"]
    assert target_archives == ["target-1"]
    dialog.close()


def test_loading_error_and_dto_validation_are_presenter_friendly(
    qapp: QApplication,
) -> None:
    dialog = PlanningDataDialog()
    dialog.set_bundle(_bundle())
    dialog.show()
    qapp.processEvents()

    dialog.set_loading(True)
    assert not dialog.tabs.isEnabled()
    assert "正在處理" in dialog.status_label.text()
    dialog.show_error("資料庫忙碌, 請稍後再試")
    assert dialog.tabs.isEnabled()
    assert dialog.status_label.property("error") is True
    assert "資料庫忙碌" in dialog.status_label.text()
    dialog.show_status("本機資料已保存。")
    assert dialog.status_label.property("error") is False
    dialog.close()

    with pytest.raises(ValueError, match="sorted and unique"):
        MaterialView("id", "code", "name", "category", 40, 1, (4, 1), None, "manual", "1")
    assert RequiredMaterialRequest("material", 0).quantity == 0
    with pytest.raises(ValueError, match="nonnegative"):
        RequiredMaterialRequest("material", -0.1)
    with pytest.raises(ValueError, match="required materials"):
        required = RequiredMaterialView("material", "素材", 1)
        CultivationTargetView(
            "target",
            None,
            None,
            "custom",
            "key",
            "0",
            "1",
            1,
            False,
            None,
            required_materials=(required, required),
        )
    with pytest.raises(ValueError, match="material codes"):
        first, second = _materials()
        PlanningDataBundle(materials=(first, replace_material_code(second, first.code)))


def replace_material_code(material: MaterialView, code: str) -> MaterialView:
    return MaterialView(
        material.identifier,
        code,
        material.name,
        material.category,
        material.waveplate_cost_per_run,
        material.average_output,
        material.open_weekdays,
        material.weekly_limit,
        material.source_status,
        material.version,
        material.inventory_count,
        material.notes,
        material.archived,
    )
