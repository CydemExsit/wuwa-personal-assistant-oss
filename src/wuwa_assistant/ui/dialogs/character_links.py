"""Team and permanent-Echo editors with a signal-only application boundary.

These dialogs intentionally know nothing about SQLAlchemy or application
services.  Presenters provide immutable view snapshots through setters and the
dialogs emit immutable requests describing user intent.  Long-running work and
all persistence therefore remain outside the Qt GUI thread.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox

type TeamMembers = tuple[str | None, str | None, str | None]


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


def _validate_members(members: TeamMembers) -> None:
    if len(members) != 3:
        raise ValueError("exactly three ordered team member slots are required")
    present = [identifier for identifier in members if identifier is not None]
    if any(not identifier.strip() for identifier in present):
        raise ValueError("team member identifier cannot be blank")
    if len(present) != len(set(present)):
        raise ValueError("a character cannot occupy multiple team slots")


@dataclass(frozen=True, slots=True)
class CharacterChoiceView:
    """One character option supplied by a team presenter."""

    identifier: str
    name: str
    detail: str = ""

    def __post_init__(self) -> None:
        _require_text(self.identifier, "character identifier")
        _require_text(self.name, "character name")

    @property
    def display_text(self) -> str:
        return self.name if not self.detail.strip() else f"{self.name} · {self.detail.strip()}"


@dataclass(frozen=True, slots=True)
class TeamSummaryView:
    """Compact team row rendered in the team editor's list."""

    identifier: str
    name: str
    priority: int
    member_names: tuple[str | None, str | None, str | None] = (None, None, None)

    def __post_init__(self) -> None:
        _require_text(self.identifier, "team identifier")
        _require_text(self.name, "team name")
        if self.priority < 0:
            raise ValueError("team priority cannot be negative")
        if len(self.member_names) != 3:
            raise ValueError("exactly three member labels are required")


@dataclass(frozen=True, slots=True)
class TeamDetailView:
    """Complete editable team snapshot supplied by a presenter."""

    identifier: str
    name: str
    priority: int
    notes: str
    member_ids: TeamMembers

    def __post_init__(self) -> None:
        _require_text(self.identifier, "team identifier")
        _require_text(self.name, "team name")
        if self.priority < 0:
            raise ValueError("team priority cannot be negative")
        _validate_members(self.member_ids)


@dataclass(frozen=True, slots=True)
class TeamSaveRequest:
    """Create/update request emitted by :class:`TeamEditorDialog`."""

    identifier: str | None
    name: str
    priority: int
    notes: str
    member_ids: TeamMembers

    def __post_init__(self) -> None:
        if self.identifier is not None:
            _require_text(self.identifier, "team identifier")
        _require_text(self.name, "team name")
        if self.priority < 0:
            raise ValueError("team priority cannot be negative")
        _validate_members(self.member_ids)


@dataclass(frozen=True, slots=True)
class EchoAssignmentChoiceView:
    """Human-readable permanent-Echo choice and its current assignment."""

    identifier: str
    name: str | None
    set_name: str | None
    cost: int | None
    level: int | None
    score: float | None
    assigned_character_id: str | None = None
    assigned_character_name: str | None = None
    assigned_slot_position: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.identifier, "Echo identifier")
        if self.cost is not None and self.cost not in {1, 3, 4}:
            raise ValueError("Echo cost must be 1, 3, 4, or null")
        if self.level is not None and not 0 <= self.level <= 25:
            raise ValueError("Echo level must be within 0..25 or null")
        if self.score is not None and (not isfinite(self.score) or not 0 <= self.score <= 100):
            raise ValueError("Echo score must be finite and within 0..100 or null")
        has_character = self.assigned_character_id is not None
        if has_character:
            _require_text(self.assigned_character_id or "", "assigned character identifier")
        elif self.assigned_slot_position is not None or self.assigned_character_name is not None:
            raise ValueError("assignment details require an assigned character identifier")
        if self.assigned_slot_position is not None and not 1 <= self.assigned_slot_position <= 5:
            raise ValueError("assigned slot position must be within 1..5")

    @property
    def assignment_text(self) -> str:
        if self.assigned_character_id is None:
            return "未指派"
        character = self.assigned_character_name or "未知角色"
        if self.assigned_slot_position is None:
            return f"已關聯: {character} / 未指定位置"
        return f"已指派: {character} / 位置 {self.assigned_slot_position}"

    @property
    def display_text(self) -> str:
        pieces = [self.name or "未知聲骸", self.set_name or "未知套裝"]
        if self.cost is not None:
            pieces.append(f"COST {self.cost}")
        if self.level is not None:
            pieces.append(f"+{self.level}")
        if self.score is not None:
            pieces.append(f"{self.score:.1f} 分")
        pieces.append(self.assignment_text)
        return " · ".join(pieces)


@dataclass(frozen=True, slots=True)
class EchoSlotContextView:
    """Target character slot currently being edited."""

    character_id: str
    character_name: str
    slot_position: int
    current_echo_id: str | None = None
    current_echo_label: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.character_id, "character identifier")
        _require_text(self.character_name, "character name")
        if not 1 <= self.slot_position <= 5:
            raise ValueError("slot position must be within 1..5")
        if self.current_echo_id is not None:
            _require_text(self.current_echo_id, "current Echo identifier")
        elif self.current_echo_label is not None:
            raise ValueError("a current Echo label requires a current Echo identifier")


@dataclass(frozen=True, slots=True)
class EchoSlotAssignmentRequest:
    """Assign or unassign intent emitted by the permanent-Echo picker."""

    character_id: str
    slot_position: int
    echo_id: str | None
    replace_confirmed: bool

    def __post_init__(self) -> None:
        _require_text(self.character_id, "character identifier")
        if not 1 <= self.slot_position <= 5:
            raise ValueError("slot position must be within 1..5")
        if self.echo_id is not None:
            _require_text(self.echo_id, "Echo identifier")
        if not isinstance(self.replace_confirmed, bool):
            raise ValueError("replace_confirmed must be a boolean")

    @property
    def is_unassign(self) -> bool:
        return self.echo_id is None


class TeamEditorDialog(QDialog):
    """Manage a team list and edit one exactly-three-slot lineup."""

    team_selected = Signal(str)
    create_started = Signal()
    save_requested = Signal(object)
    delete_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TeamEditorDialog")
        self.setWindowTitle("隊伍管理")
        self.resize(820, 560)
        self._team_id: str | None = None
        self._loading = False
        self._editor_open = False

        self.team_list = QListWidget()
        self.team_list.setObjectName("TeamList")
        self.team_list.setMinimumWidth(250)
        self.team_list.currentItemChanged.connect(self._on_team_changed)

        self.create_button = QPushButton("新增隊伍")
        self.create_button.setObjectName("TeamCreateButton")
        self.create_button.clicked.connect(self.begin_create)

        list_group = QGroupBox("既有隊伍")
        list_layout = QVBoxLayout(list_group)
        list_layout.addWidget(self.team_list, 1)
        list_layout.addWidget(self.create_button)

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("TeamNameEdit")
        self.name_edit.setMaxLength(120)
        self.priority_spin = QSpinBox()
        self.priority_spin.setObjectName("TeamPrioritySpin")
        self.priority_spin.setRange(0, 9999)
        self.priority_spin.setValue(100)
        self.member_combos = tuple(self._member_combo(position) for position in range(1, 4))
        self.notes_edit = QTextEdit()
        self.notes_edit.setObjectName("TeamNotesEdit")
        self.notes_edit.setPlaceholderText("可記錄輪轉、適用內容或替代角色。")

        form = QFormLayout()
        form.addRow("隊伍名稱", self.name_edit)
        form.addRow("優先級", self.priority_spin)
        for position, combo in enumerate(self.member_combos, start=1):
            form.addRow(f"成員 {position}", combo)
        form.addRow("備註", self.notes_edit)

        self.status_label = QLabel("請選擇既有隊伍, 或新增一個隊伍。")
        self.status_label.setObjectName("TeamEditorStatus")
        self.status_label.setWordWrap(True)
        self.save_button = QPushButton("儲存隊伍")
        self.save_button.setObjectName("TeamSaveButton")
        self.save_button.clicked.connect(self._emit_save)
        self.delete_button = QPushButton("刪除隊伍")
        self.delete_button.setObjectName("TeamDeleteButton")
        self.delete_button.clicked.connect(self._emit_delete)
        self.close_button = QPushButton("關閉")
        self.close_button.setObjectName("TeamCloseButton")
        self.close_button.clicked.connect(self.reject)

        actions = QHBoxLayout()
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.save_button)
        actions.addWidget(self.close_button)

        editor_group = QGroupBox("隊伍內容")
        editor_layout = QVBoxLayout(editor_group)
        editor_layout.addLayout(form)
        editor_layout.addStretch(1)
        editor_layout.addLayout(actions)

        layout = QHBoxLayout(self)
        layout.addWidget(list_group, 2)
        layout.addWidget(editor_group, 5)
        self._set_editor_enabled(False)

    def _member_combo(self, position: int) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(f"TeamMember{position}Combo")
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(24)
        combo.addItem("(空位)", None)
        return combo

    @Slot(object)
    def set_teams(self, teams: Sequence[TeamSummaryView]) -> None:
        selected_id = self.current_team_id()
        blocker = QSignalBlocker(self.team_list)
        self.team_list.clear()
        selected_row = -1
        for row, team in enumerate(teams):
            members = " / ".join(name or "空位" for name in team.member_names)
            item = QListWidgetItem(f"{team.name}  ·  優先 {team.priority}\n{members}")
            item.setData(Qt.ItemDataRole.UserRole, team.identifier)
            item.setToolTip(members)
            self.team_list.addItem(item)
            if team.identifier == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.team_list.setCurrentRow(selected_row)
        del blocker
        if not teams and self._team_id is None:
            self.status_label.setText("尚未建立隊伍; 按下「新增隊伍」開始。")

    @Slot(object)
    def set_character_choices(self, choices: Sequence[CharacterChoiceView]) -> None:
        current_values = tuple(self._combo_identifier(combo) for combo in self.member_combos)
        for combo, current in zip(self.member_combos, current_values, strict=True):
            blocker = QSignalBlocker(combo)
            combo.clear()
            combo.addItem("(空位)", None)
            for choice in choices:
                combo.addItem(choice.display_text, choice.identifier)
            self._select_combo(combo, current)
            del blocker

    @Slot(object)
    def apply_team(self, team: TeamDetailView) -> None:
        self._team_id = team.identifier
        self._editor_open = True
        self.name_edit.setText(team.name)
        self.priority_spin.setValue(team.priority)
        self.notes_edit.setPlainText(team.notes)
        for combo, member_id in zip(self.member_combos, team.member_ids, strict=True):
            self._select_combo(combo, member_id)
        self._set_editor_enabled(True)
        self.delete_button.setEnabled(not self._loading)
        self.status_label.setProperty("error", False)
        self.status_label.setText(f"正在編輯「{team.name}」。")
        _repolish(self.status_label)
        self.select_team(team.identifier)

    @Slot()
    def begin_create(self) -> None:
        blocker = QSignalBlocker(self.team_list)
        self.team_list.clearSelection()
        self.team_list.setCurrentRow(-1)
        del blocker
        self._team_id = None
        self._editor_open = True
        self.name_edit.clear()
        self.priority_spin.setValue(100)
        self.notes_edit.clear()
        for combo in self.member_combos:
            combo.setCurrentIndex(0)
        self._set_editor_enabled(True)
        self.delete_button.setEnabled(False)
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在新增隊伍; 空白成員欄位會保留為空位。")
        _repolish(self.status_label)
        self.name_edit.setFocus()
        self.create_started.emit()

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在處理隊伍資料…") -> None:
        self._loading = loading
        self.team_list.setEnabled(not loading)
        self.create_button.setEnabled(not loading)
        self._set_editor_enabled(self._editor_open)
        if loading:
            self.status_label.setProperty("error", False)
            self.status_label.setText(message)
            _repolish(self.status_label)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", True)
        self.status_label.setText(message)
        _repolish(self.status_label)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setProperty("error", False)
        self.status_label.setText(message)
        _repolish(self.status_label)

    def current_team_id(self) -> str | None:
        item = self.team_list.currentItem()
        value = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, str) and value else self._team_id

    def select_team(self, identifier: str) -> bool:
        for row in range(self.team_list.count()):
            item = self.team_list.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == identifier:
                blocker = QSignalBlocker(self.team_list)
                self.team_list.setCurrentRow(row)
                del blocker
                return True
        return False

    def build_save_request(self) -> TeamSaveRequest:
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("隊伍名稱不可空白。")
        members: TeamMembers = (
            self._combo_identifier(self.member_combos[0]),
            self._combo_identifier(self.member_combos[1]),
            self._combo_identifier(self.member_combos[2]),
        )
        if len([member for member in members if member is not None]) != len(
            {member for member in members if member is not None}
        ):
            raise ValueError("同一名角色不可重複出現在隊伍中。")
        return TeamSaveRequest(
            identifier=self._team_id,
            name=name,
            priority=self.priority_spin.value(),
            notes=self.notes_edit.toPlainText().strip(),
            member_ids=members,
        )

    def _set_editor_enabled(self, enabled: bool) -> None:
        active = enabled and not self._loading
        self.name_edit.setEnabled(active)
        self.priority_spin.setEnabled(active)
        self.notes_edit.setEnabled(active)
        for combo in self.member_combos:
            combo.setEnabled(active)
        self.save_button.setEnabled(active)
        self.delete_button.setEnabled(active and self._team_id is not None)

    @staticmethod
    def _combo_identifier(combo: QComboBox) -> str | None:
        value = combo.currentData()
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _select_combo(combo: QComboBox, identifier: str | None) -> None:
        index = combo.findData(identifier)
        if identifier is not None and index < 0:
            combo.addItem(f"未知角色 ({identifier})", identifier)
            index = combo.count() - 1
        combo.setCurrentIndex(max(index, 0))

    def _on_team_changed(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        del previous
        if current is None:
            return
        value = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, str) and value:
            self.team_selected.emit(value)

    def _emit_save(self) -> None:
        try:
            request = self.build_save_request()
        except ValueError as error:
            self.status_label.setProperty("error", True)
            self.status_label.setText(str(error))
            _repolish(self.status_label)
            return
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在儲存隊伍…")
        _repolish(self.status_label)
        self.save_requested.emit(request)

    def _emit_delete(self) -> None:
        if self._team_id is None:
            return
        answer = QMessageBox.question(
            self,
            "刪除隊伍",
            "確定要刪除此隊伍嗎? 此操作只刪除工具內的隊伍編排。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(self._team_id)


class EchoSlotAssignmentDialog(QDialog):
    """Assign one persistent Echo without silently replacing existing links."""

    save_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EchoSlotAssignmentDialog")
        self.setWindowTitle("永久聲骸指派")
        self.resize(760, 330)
        self._context: EchoSlotContextView | None = None
        self._choices: dict[str, EchoAssignmentChoiceView] = {}
        self._loading = False

        self.target_label = QLabel("尚未選擇角色位置")
        self.target_label.setObjectName("EchoAssignmentTarget")
        self.current_label = QLabel("目前: —")
        self.current_label.setObjectName("EchoAssignmentCurrent")
        self.current_label.setWordWrap(True)
        self.echo_combo = QComboBox()
        self.echo_combo.setObjectName("EchoAssignmentChoice")
        self.echo_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.echo_combo.setMinimumContentsLength(60)
        self.echo_combo.currentIndexChanged.connect(self._update_replacement_state)

        self.replace_check = QCheckBox("我確認要取代或解除既有指派")
        self.replace_check.setObjectName("EchoAssignmentReplaceCheck")
        self.replace_check.toggled.connect(self._update_replacement_state)
        self.replace_check.setVisible(False)
        self.replacement_reason_label = QLabel()
        self.replacement_reason_label.setObjectName("EchoAssignmentReplacementReason")
        self.replacement_reason_label.setWordWrap(True)
        self.replacement_reason_label.setVisible(False)

        self.status_label = QLabel("請先載入可指派聲骸。")
        self.status_label.setObjectName("EchoAssignmentStatus")
        self.status_label.setWordWrap(True)
        self.save_button = QPushButton("套用指派")
        self.save_button.setObjectName("EchoAssignmentSaveButton")
        self.save_button.clicked.connect(self._emit_save)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("EchoAssignmentCancelButton")
        self.cancel_button.clicked.connect(self.reject)

        form = QFormLayout()
        form.addRow("目標", self.target_label)
        form.addRow("目前聲骸", self.current_label)
        form.addRow("選擇聲骸", self.echo_combo)
        form.addRow("替換確認", self.replace_check)
        form.addRow("", self.replacement_reason_label)

        actions = QHBoxLayout()
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.save_button)
        actions.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(actions)
        self._update_replacement_state()

    @Slot(object)
    def set_context(self, context: EchoSlotContextView) -> None:
        self._context = context
        self.target_label.setText(f"{context.character_name} / 位置 {context.slot_position}")
        self.replace_check.setChecked(False)
        self._render_current_echo()
        if context.current_echo_id is not None:
            self.set_selected_echo(context.current_echo_id)
        else:
            self.echo_combo.setCurrentIndex(0)
        self._update_replacement_state()

    @Slot(object)
    def set_echo_choices(self, choices: Sequence[EchoAssignmentChoiceView]) -> None:
        selected = self.selected_echo_id()
        self._choices = {choice.identifier: choice for choice in choices}
        blocker = QSignalBlocker(self.echo_combo)
        self.echo_combo.clear()
        self.echo_combo.addItem("不指派聲骸 (解除此位置)", None)
        for choice in choices:
            self.echo_combo.addItem(choice.display_text, choice.identifier)
        preferred = (
            self._context.current_echo_id
            if self._context is not None and self._context.current_echo_id is not None
            else selected
        )
        self._select_echo(preferred)
        del blocker
        self.replace_check.setChecked(False)
        self._render_current_echo()
        self._update_replacement_state()

    def set_selected_echo(self, identifier: str | None) -> bool:
        found = self._select_echo(identifier)
        self._update_replacement_state()
        return found

    def selected_echo_id(self) -> str | None:
        value = self.echo_combo.currentData()
        return value if isinstance(value, str) and value else None

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在處理聲骸指派…") -> None:
        self._loading = loading
        self.echo_combo.setEnabled(not loading)
        self.replace_check.setEnabled(not loading)
        self._update_replacement_state()
        if loading:
            self.status_label.setProperty("error", False)
            self.status_label.setText(message)
            _repolish(self.status_label)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self._loading = False
        self.echo_combo.setEnabled(True)
        self.replace_check.setEnabled(True)
        self.status_label.setProperty("error", True)
        self.status_label.setText(message)
        _repolish(self.status_label)
        self._update_replacement_state(preserve_status=True)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self._loading = False
        self.echo_combo.setEnabled(True)
        self.replace_check.setEnabled(True)
        self.status_label.setProperty("error", False)
        self.status_label.setText(message)
        _repolish(self.status_label)
        self._update_replacement_state(preserve_status=True)

    def build_request(self) -> EchoSlotAssignmentRequest:
        context = self._context
        if context is None:
            raise RuntimeError("尚未指定角色與永久聲骸位置。")
        reasons = self._replacement_reasons()
        if reasons and not self.replace_check.isChecked():
            raise ValueError("此操作會替換或解除既有指派, 請先勾選明確確認。")
        return EchoSlotAssignmentRequest(
            character_id=context.character_id,
            slot_position=context.slot_position,
            echo_id=self.selected_echo_id(),
            replace_confirmed=self.replace_check.isChecked(),
        )

    def _select_echo(self, identifier: str | None) -> bool:
        index = self.echo_combo.findData(identifier)
        if index < 0:
            return False
        self.echo_combo.setCurrentIndex(index)
        return True

    def _render_current_echo(self) -> None:
        context = self._context
        if context is None:
            self.current_label.setText("目前: —")
            return
        if context.current_echo_id is None:
            self.current_label.setText("目前未指派聲骸")
            return
        choice = self._choices.get(context.current_echo_id)
        label = (
            choice.display_text
            if choice is not None
            else context.current_echo_label or context.current_echo_id
        )
        self.current_label.setText(label)

    def _replacement_reasons(self) -> tuple[str, ...]:
        context = self._context
        if context is None:
            return ()
        selected_id = self.selected_echo_id()
        reasons: list[str] = []
        if context.current_echo_id is not None and selected_id != context.current_echo_id:
            if selected_id is None:
                reasons.append("將解除目前位置的既有聲骸")
            else:
                reasons.append("目前位置已有其他聲骸")
        selected = self._choices.get(selected_id or "")
        if selected is not None and selected.assigned_character_id is not None:
            same_target = (
                selected.assigned_character_id == context.character_id
                and selected.assigned_slot_position == context.slot_position
            )
            if not same_target:
                reasons.append(selected.assignment_text)
        return tuple(reasons)

    def _update_replacement_state(self, *args: object, preserve_status: bool = False) -> None:
        del args
        reasons = self._replacement_reasons()
        required = bool(reasons)
        self.replace_check.setVisible(required)
        self.replacement_reason_label.setVisible(required)
        self.replacement_reason_label.setText("; ".join(reasons))
        no_change = (
            self._context is not None
            and self._context.current_echo_id is None
            and self.selected_echo_id() is None
        )
        enabled = (
            self._context is not None
            and not self._loading
            and not no_change
            and (not required or self.replace_check.isChecked())
        )
        self.save_button.setEnabled(enabled)
        if not preserve_status and not self._loading:
            self.status_label.setProperty("error", False)
            if required and not self.replace_check.isChecked():
                self.status_label.setText("這項操作會變更既有指派; 確認後才能套用。")
            elif no_change:
                self.status_label.setText("目前位置沒有聲骸, 無需解除。")
            else:
                self.status_label.setText("請確認選擇後套用; 工具不會操作遊戲。")
            _repolish(self.status_label)

    def _emit_save(self) -> None:
        try:
            request = self.build_request()
        except (RuntimeError, ValueError) as error:
            self.status_label.setProperty("error", True)
            self.status_label.setText(str(error))
            _repolish(self.status_label)
            return
        self.status_label.setProperty("error", False)
        self.status_label.setText("正在儲存永久聲骸指派…")
        _repolish(self.status_label)
        self.save_requested.emit(request)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


__all__ = [
    "CharacterChoiceView",
    "EchoAssignmentChoiceView",
    "EchoSlotAssignmentDialog",
    "EchoSlotAssignmentRequest",
    "EchoSlotContextView",
    "TeamDetailView",
    "TeamEditorDialog",
    "TeamSaveRequest",
    "TeamSummaryView",
]
