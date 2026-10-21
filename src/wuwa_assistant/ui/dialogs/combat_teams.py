"""Per-character independent team scenarios with concise, inspectable buffs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.application.characters import CharacterDetail
from wuwa_assistant.application.combat_build import (
    CombatTeam,
    evaluate_build,
    load_combat_catalog,
    team_to_json,
    teams_from_goal,
)
from wuwa_assistant.application.combat_service import CombatSettings
from wuwa_assistant.scoring.character_guides import guide_for_character
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector
from wuwa_assistant.ui.widgets.text_details import TextDetailsButton

_DAMAGE_LABELS = {
    "basic_attack": "普攻",
    "heavy_attack": "重擊",
    "resonance_skill": "共鳴技能",
    "resonance_liberation": "共鳴解放",
}


class _EnergySpin(QDoubleSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class CombatTeamsDialog(QDialog):
    save_requested = Signal(object)

    def __init__(
        self,
        detail: CharacterDetail,
        roster: Mapping[str, CharacterDetail],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{detail.summary.name} · 戰鬥隊伍")
        self.setObjectName("CombatTeamsDialog")
        available = self.screen().availableGeometry()
        self.resize(min(790, available.width() - 48), min(620, available.height() - 72))
        self._detail = detail
        self._roster = dict(roster)
        self._teams = list(teams_from_goal(detail.cultivation_goal))
        self._primary = detail.cultivation_goal.get("combat_primary_team")
        self._current = -1
        self._loading = False
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("共用同一套聲骸 · 所有隊伍充能須達標 · 主隊優先比較輸出"))
        top = QHBoxLayout()
        self.team_list = QListWidget()
        self.team_list.setMaximumHeight(90)
        self.team_list.currentRowChanged.connect(self._select)
        top.addWidget(self.team_list, 1)
        buttons = QVBoxLayout()
        add = QPushButton("新增隊伍")
        add.clicked.connect(self._add)
        remove = QPushButton("移除此隊")
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        top.addLayout(buttons)
        layout.addLayout(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        self.form = QFormLayout()
        self.name_edit = QLineEdit()
        self.primary_check = QCheckBox("主要隊伍")
        self.primary_check.toggled.connect(self._primary_changed)
        self.member_combos = [ChoiceSelector(), ChoiceSelector()]
        for combo in self.member_combos:
            combo.addItem("不指定", "")
            for identifier, actor in self._roster.items():
                if identifier != detail.summary.id:
                    combo.addItem(actor.summary.name, identifier)
        self.outro_combo = ChoiceSelector()
        self.energy_spin = _EnergySpin()
        self.energy_spin.setRange(0, 1000)
        self.energy_spin.setDecimals(1)
        self.energy_spin.setSpecialValueText("依角色目標")
        self.energy_spin.setSuffix("%")
        self.energy_spin.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.damage_combo = ChoiceSelector()
        for key, label in _DAMAGE_LABELS.items():
            self.damage_combo.addItem(label, key)
        self.form.addRow("隊伍名稱", self.name_edit)
        self.form.addRow("比較優先", self.primary_check)
        for i, combo in enumerate(self.member_combos, 1):
            self.form.addRow(f"隊友{i}", combo)
        self.form.addRow("直接延奏隊友", self.outro_combo)
        self.form.addRow("戰鬥充能目標", self.energy_spin)
        self.form.addRow("主要輸出窗口", self.damage_combo)
        self.mode_combos: dict[str, ChoiceSelector] = {}
        self.mode_rows: list[tuple[QLabel, ChoiceSelector]] = []
        for identifier, actor in self._roster.items():
            modes = load_combat_catalog()["modes"].get(actor.summary.name)
            if modes:
                combo = ChoiceSelector()
                for mode in modes:
                    combo.addItem(mode, mode)
                mode_label = QLabel(f"{actor.summary.name}模態")
                self.form.addRow(mode_label, combo)
                self.mode_combos[identifier] = combo
                self.mode_rows.append((mode_label, combo))
                combo.currentIndexChanged.connect(self._refresh_effects)
        body_layout.addLayout(self.form)
        self.effects = QTreeWidget()
        self.effects.setHeaderLabels(["循環增益", "觸發條件"])
        self.effects.setMinimumHeight(160)
        self.effects.setColumnWidth(0, 330)
        self.effects.itemChanged.connect(self._effect_changed)
        body_layout.addWidget(self.effects)
        self.confirm_check = QCheckBox("已確認所選增益符合正常輪轉與主要輸出窗口")
        self.status = QLabel()
        self.status.setWordWrap(True)
        body_layout.addWidget(self.status)
        self.details = TextDetailsButton("查看戰鬥數值與來源", body)
        body_layout.addWidget(self.details)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.confirm_check)
        actions = QHBoxLayout()
        self.error = QLabel()
        self.error.setWordWrap(True)
        actions.addWidget(self.error, 1)
        self.save_button = QPushButton("儲存隊伍")
        self.save_button.clicked.connect(self._save)
        close = QPushButton("關閉")
        close.clicked.connect(self.close)
        actions.addWidget(self.save_button)
        actions.addWidget(close)
        layout.addLayout(actions)
        for combo in [*self.member_combos, self.outro_combo, self.damage_combo]:
            combo.currentIndexChanged.connect(self._refresh_effects)
        self.energy_spin.valueChanged.connect(self._refresh_effects)
        self._relist()
        if self._teams:
            self.team_list.setCurrentRow(0)
        else:
            self._add()

    def _read(self) -> CombatTeam:
        old = self._teams[self._current]
        members = tuple(str(c.currentData()) for c in self.member_combos if c.currentData())
        excluded = tuple(
            str(item.data(0, Qt.ItemDataRole.UserRole))
            for i in range(self.effects.topLevelItemCount())
            if (item := self.effects.topLevelItem(i)) is not None
            and item.checkState(0) == Qt.CheckState.Unchecked
        )
        return replace(
            old,
            name=self.name_edit.text().strip() or old.name,
            member_ids=members,
            energy_target=self.energy_spin.value() or None,
            damage_type=str(self.damage_combo.currentData()),
            excluded_effects=excluded,
            modes=tuple(
                (key, str(c.currentData()))
                for key, c in self.mode_combos.items()
                if key in {self._detail.summary.id, *members}
            ),
            confirmed=self.confirm_check.isChecked(),
            outro_id=self.outro_combo.currentData() or None,
        )

    def _store(self) -> None:
        if self._current >= 0:
            self._teams[self._current] = self._read()
            if self.primary_check.isChecked():
                self._primary = self._teams[self._current].identifier

    def _primary_changed(self, checked: bool) -> None:
        if self._loading or self._current < 0:
            return
        identifier = self._teams[self._current].identifier
        if checked:
            self._primary = identifier
            for index, team in enumerate(self._teams):
                item = self.team_list.item(index)
                if item is not None:
                    item.setText(("★ " if team.identifier == identifier else "") + team.name)
        elif identifier == self._primary:
            self.primary_check.setChecked(True)

    def _relist(self) -> None:
        self.team_list.blockSignals(True)
        self.team_list.clear()
        for team in self._teams:
            self.team_list.addItem(("★ " if team.identifier == self._primary else "") + team.name)
        self.team_list.blockSignals(False)

    def _select(self, index: int) -> None:
        if index < 0:
            return
        try:
            self._store()
        except ValueError as exc:
            self.error.setText(str(exc))
            self.team_list.blockSignals(True)
            self.team_list.setCurrentRow(self._current)
            self.team_list.blockSignals(False)
            return
        self._current = index
        team = self._teams[index]
        self._loading = True
        self.name_edit.setText(team.name)
        self.primary_check.setChecked(team.identifier == self._primary)
        for i, combo in enumerate(self.member_combos):
            combo.setCurrentIndex(
                max(0, combo.findData(team.member_ids[i] if i < len(team.member_ids) else ""))
            )
        self.energy_spin.setValue(team.energy_target or 0)
        self.damage_combo.setCurrentIndex(self.damage_combo.findData(team.damage_type))
        self.confirm_check.setChecked(team.confirmed)
        for identifier, combo in self.mode_combos.items():
            actor = self._roster[identifier]
            value = dict(team.modes).get(
                identifier, load_combat_catalog()["default_modes"].get(actor.summary.name)
            )
            combo.setCurrentIndex(max(0, combo.findData(value)))
        self._update_outro(team.outro_id)
        self._loading = False
        self._populate_effects(team)

    def _update_outro(self, identifier: str | None) -> None:
        self.outro_combo.blockSignals(True)
        self.outro_combo.clear()
        self.outro_combo.addItem("不計限切人延奏", "")
        for combo in self.member_combos:
            if combo.currentData():
                self.outro_combo.addItem(combo.currentText(), combo.currentData())
        self.outro_combo.setCurrentIndex(max(0, self.outro_combo.findData(identifier)))
        self.outro_combo.blockSignals(False)

    def _refresh_effects(self, *_: object) -> None:
        if self._loading or self._current < 0:
            return
        try:
            team = self._read()
            self._update_outro(team.outro_id)
            self._populate_effects(team)
        except ValueError as exc:
            self.error.setText(str(exc))

    def _populate_effects(self, team: CombatTeam) -> None:
        self.effects.blockSignals(True)
        self.effects.clear()
        members = {self._detail.summary.id, *team.member_ids}
        for label, combo in self.mode_rows:
            identifier = next(key for key, value in self.mode_combos.items() if value is combo)
            label.setVisible(identifier in members)
            combo.setVisible(identifier in members)
        preview_goal = {
            **self._detail.cultivation_goal,
            "combat_teams": [team_to_json(replace(team, confirmed=True))],
            "combat_primary_team": team.identifier,
        }
        preview = evaluate_build(replace(self._detail, cultivation_goal=preview_goal), self._roster)
        for effect in preview.teams[0].effects:
            item = QTreeWidgetItem([effect.text, effect.condition])
            item.setData(0, Qt.ItemDataRole.UserRole, effect.identifier)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0, Qt.CheckState.Checked if effect.active else Qt.CheckState.Unchecked
            )
            item.setToolTip(0, effect.source)
            item.setToolTip(1, effect.condition)
            self.effects.addTopLevelItem(item)
        self.effects.blockSignals(False)
        self._preview(team)

    def _effect_changed(self, *_: object) -> None:
        try:
            self._preview(self._read())
        except ValueError as exc:
            self.error.setText(str(exc))

    def _preview(self, team: CombatTeam) -> None:
        goal = {
            **self._detail.cultivation_goal,
            "combat_teams": [team_to_json(replace(team, confirmed=True))],
            "combat_primary_team": team.identifier,
        }
        build = evaluate_build(replace(self._detail, cultivation_goal=goal), self._roster)
        self.status.setText("預覽 · " + build.summary)
        self.details.set_details("戰鬥數值與來源", build.details())

    def _add(self) -> None:
        try:
            self._store()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        guide = guide_for_character(self._detail.summary.name)
        damage = "resonance_liberation"
        if guide:
            tiers = [str(key) for tier in guide.tiers for key in tier]
            damage = next(
                (
                    key.removesuffix("_damage")
                    for key in tiers
                    if key.removesuffix("_damage") in _DAMAGE_LABELS
                ),
                damage,
            )
        team = CombatTeam(uuid4().hex, f"隊伍{len(self._teams) + 1}", damage_type=damage)
        self._teams.append(team)
        if self._primary is None:
            self._primary = team.identifier
        self._current = -1
        self._relist()
        self.team_list.setCurrentRow(len(self._teams) - 1)

    def _remove(self) -> None:
        if self._current < 0:
            return
        removed = self._teams.pop(self._current)
        if removed.identifier == self._primary:
            self._primary = self._teams[0].identifier if self._teams else None
        self._current = -1
        self._relist()
        if self._teams:
            self.team_list.setCurrentRow(0)
        else:
            self.effects.clear()
            self.status.setText("儲存後停用此角色的戰鬥配裝比較")

    def _save(self) -> None:
        try:
            self._store()
            self.save_requested.emit(
                CombatSettings(self._detail.summary.id, tuple(self._teams), self._primary)
            )
        except ValueError as exc:
            self.error.setText(str(exc))

    def set_result(self, error: str | None = None) -> None:
        self.error.setText(error or "已儲存")
        if error is None:
            self.accept()
