"""Background presenter for persistent character cultivation records."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from html import escape
from threading import RLock
from typing import TypeVar, cast

from PySide6.QtCore import QObject, Qt, Signal, Slot

from wuwa_assistant.application.character_catalog import (
    CharacterCatalog,
    CharacterCatalogEntry,
    load_bundled_character_catalog,
)
from wuwa_assistant.application.character_completion import evaluate_character_detail
from wuwa_assistant.application.character_scoring import character_profile
from wuwa_assistant.application.character_stats import calculate_character_panel
from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CharacterDetail,
    CreateCharacter,
    CreateTeam,
    SaveCharacter,
    SaveTeam,
    SkillDefinition,
    TeamMemberInput,
    TeamSnapshot,
    UpdateCharacter,
    UpdateSkill,
    UpdateTeam,
    UpsertWeapon,
)
from wuwa_assistant.application.combat_build import evaluate_build
from wuwa_assistant.application.combat_service import CombatSettings
from wuwa_assistant.application.equipment_alerts import equipment_fit_alert
from wuwa_assistant.application.equipment_check import equipment_check
from wuwa_assistant.application.equipment_overview import (
    CharacterEquipmentOverview,
    EquipmentOverview,
    EquippedEchoScore,
)
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.application.scoring_workflow import profile_from_snapshot
from wuwa_assistant.cultivation import CultivationCompletion
from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID, guide_for_character
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.dialogs import (
    CharacterCatalogDialog,
    CharacterCatalogItemView,
    CharacterCatalogSelectionRequest,
    CharacterChoiceView,
    EchoAssignmentChoiceView,
    EchoSlotAssignmentDialog,
    EchoSlotAssignmentRequest,
    EchoSlotContextView,
    TeamDetailView,
    TeamEditorDialog,
    TeamSaveRequest,
    TeamSummaryView,
)
from wuwa_assistant.ui.dialogs.combat_teams import CombatTeamsDialog
from wuwa_assistant.ui.pages.characters import (
    CharacterDetailView,
    CharacterEditRequest,
    CharactersPage,
    CharacterSummaryView,
    ChoiceOptionView,
    PermanentEchoSlotView,
    SkillProgressView,
    WeaponProgressView,
)
from wuwa_assistant.ui.pages.dashboard import DashboardSnapshot
from wuwa_assistant.ui.pages.echo_live import ChoiceOption
from wuwa_assistant.ui.pages.equipment_detail import (
    GearCharacterDetailView,
    GearPieceDetailView,
    SubstatLineView,
)
from wuwa_assistant.ui.pages.equipment_overview import EquipmentCharacterView, EquipmentPieceView

_T = TypeVar("_T")


class _Operation(StrEnum):
    LOAD = "load"
    LOAD_EXTERNAL = "load_external"
    RESET_CHECK = "reset_check"
    SAVE = "save"
    ARCHIVE = "archive"
    CATALOG_IMPORT = "catalog_import"
    COMBAT_SAVE = "combat_save"


class _DialogOperation(StrEnum):
    TEAM_LOAD = "team_load"
    TEAM_SAVE = "team_save"
    TEAM_DELETE = "team_delete"
    ECHO_LOAD = "echo_load"
    ECHO_SAVE = "echo_save"


@dataclass(frozen=True, slots=True)
class _WorkspaceEntry:
    record: CharacterDetail
    view: CharacterDetailView


@dataclass(frozen=True, slots=True)
class _Workspace:
    rows: tuple[CharacterSummaryView, ...]
    entries: tuple[_WorkspaceEntry, ...]
    profiles: tuple[ChoiceOptionView, ...]
    default_profile_id: str | None
    overview: EquipmentOverview | None = None


@dataclass(frozen=True, slots=True)
class _JobResult:
    operation: _Operation
    workspace: _Workspace | None = None
    selected_id: str | None = None
    message: str | None = None
    error: str | None = None
    created_names: tuple[str, ...] = ()
    skipped_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CatalogImportOutcome:
    workspace: _Workspace
    selected_id: str | None
    created_names: tuple[str, ...]
    skipped_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TeamBundle:
    teams: tuple[TeamSnapshot, ...]


@dataclass(frozen=True, slots=True)
class _EchoBundle:
    generation: int
    context: EchoSlotContextView
    choices: tuple[EchoAssignmentChoiceView, ...]


@dataclass(frozen=True, slots=True)
class _DialogJobResult:
    operation: _DialogOperation
    value: object | None = None
    selected_id: str | None = None
    error: str | None = None
    generation: int | None = None


class CharactersPresenter(QObject):
    """Keep SQLAlchemy work off Qt's UI thread and publish immutable view data."""

    _job_completed = Signal(object)
    _dialog_job_completed = Signal(object)
    character_choices_changed = Signal(object)
    dashboard_snapshot_changed = Signal(object)
    scoring_context_changed = Signal()
    equipment_recheck_ready = Signal()

    def __init__(
        self,
        *,
        service: CharacterCultivationService,
        scoring_profiles: ScoringProfileService,
        page: CharactersPage,
        data_set_id: str,
        catalog: CharacterCatalog | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._profiles = scoring_profiles
        self._page = page
        self._data_set_id = data_set_id
        self._catalog = catalog or load_bundled_character_catalog()
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-characters-db")
        self._future_lock = RLock()
        self._futures: set[Future[object]] = set()
        self._entries: dict[str, _WorkspaceEntry] = {}
        self._team_by_id: dict[str, TeamSnapshot] = {}
        self._team_dialog: TeamEditorDialog | None = None
        self._echo_dialog: EchoSlotAssignmentDialog | None = None
        self._catalog_dialog: CharacterCatalogDialog | None = None
        self._combat_dialog: CombatTeamsDialog | None = None
        self._default_profile_id: str | None = None
        self._comparison_profile_id: str | None = AUTO_PROFILE_ID
        self._echo_generation = 0
        self._active_echo_context: tuple[int, str, int] | None = None
        self._closing = False

        self._job_completed.connect(self._handle_job_result, Qt.ConnectionType.QueuedConnection)
        self._dialog_job_completed.connect(
            self._handle_dialog_job_result,
            Qt.ConnectionType.QueuedConnection,
        )
        page.refresh_requested.connect(self.refresh)
        page.catalog_requested.connect(self.open_character_catalog)
        page.character_selected.connect(self.select_character)
        page.edit_requested.connect(self.save_character)
        page.archive_requested.connect(self.toggle_archive)
        page.weapon_edit_requested.connect(self.focus_weapon_editor)
        page.team_edit_requested.connect(self.open_team_editor)
        page.combat_edit_requested.connect(self.open_combat_editor)
        page.echo_slot_edit_requested.connect(self.open_echo_editor)
        page.equipment_check_requested.connect(self.reset_equipment_check)
        page.overview_page.profile_requested.connect(self.change_comparison_profile)

    @Slot(str)
    def change_comparison_profile(self, profile_id: str) -> None:
        if self._closing:
            return
        self._comparison_profile_id = profile_id
        self.refresh_external_changes()

    def initialize(self) -> None:
        if self._closing:
            return
        self._page.set_loading(True, "正在載入角色養成資料…")
        self._submit(_Operation.LOAD, self._load_workspace)

    @Slot(str)
    def open_combat_editor(self, character_id: str) -> None:
        entry = self._entries.get(character_id)
        if self._closing or entry is None:
            return
        if self._combat_dialog is not None:
            self._combat_dialog.close()
        dialog = CombatTeamsDialog(
            entry.record,
            {
                key: value.record
                for key, value in self._entries.items()
                if value.record.summary.archived_at is None
            },
            self._page,
        )
        self._combat_dialog = dialog
        dialog.save_requested.connect(self._save_combat_settings)
        dialog.show()

    @Slot(object)
    def _save_combat_settings(self, settings: object) -> None:
        if self._closing or not isinstance(settings, CombatSettings):
            return

        def save() -> _Workspace:
            self._service.save_combat_settings(self._data_set_id, settings)
            return self._load_workspace()

        self._submit(_Operation.COMBAT_SAVE, save, selected_id=settings.character_id)

    @Slot()
    def refresh(self) -> None:
        if self._closing:
            return
        selected = self._page.current_character_id()
        self._page.set_loading(True, "正在重新整理角色…")
        self._submit(_Operation.LOAD, self._load_workspace, selected_id=selected)

    @Slot()
    def refresh_external_changes(self) -> None:
        """Refresh recognition-owned data without replacing an open editor draft."""

        if not self._closing:
            self._submit(_Operation.LOAD_EXTERNAL, self._load_workspace)

    @Slot(str)
    def reset_equipment_check(self, character_id: str) -> None:
        def reset() -> _Workspace:
            self._service.reset_equipment_check(character_id, data_set_id=self._data_set_id)
            return self._load_workspace()

        if not self._closing:
            self._submit(_Operation.RESET_CHECK, reset)

    @Slot(str)
    def select_character(self, character_id: str) -> None:
        entry = self._entries.get(character_id)
        if entry is not None:
            self._page.apply_character(entry.view)

    @Slot()
    def open_character_catalog(self) -> None:
        if self._closing:
            return
        dialog = self._ensure_catalog_dialog()
        self._apply_catalog_to_dialog()
        dialog.show()
        dialog.raise_()

    @Slot(object)
    def save_character(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, CharacterEditRequest):
            self._page.show_error("角色編輯資料格式不正確。")
            return
        existing = None if payload.identifier is None else self._entries.get(payload.identifier)
        if payload.identifier is not None and existing is None:
            self._page.show_error("角色資料已變更,請重新整理後再試。")
            return
        self._page.set_loading(True, "正在儲存角色資料…")
        self._submit(
            _Operation.SAVE,
            lambda: self._save_and_reload(payload, existing),
            selected_id=payload.identifier,
        )

    @Slot(str)
    def toggle_archive(self, character_id: str) -> None:
        if self._closing:
            return
        entry = self._entries.get(character_id)
        if entry is None:
            self._page.show_error("找不到要封存的角色。")
            return
        self._page.set_loading(True, "正在更新封存狀態…")
        archived = entry.record.summary.archived_at is not None
        self._submit(
            _Operation.ARCHIVE,
            lambda: self._archive_and_reload(character_id, restore=archived),
            selected_id=character_id if archived else None,
        )

    @Slot(str)
    def open_team_editor(self, _character_id: str) -> None:
        if self._closing:
            return
        dialog = self._ensure_team_dialog()
        dialog.set_character_choices(
            tuple(
                CharacterChoiceView(
                    entry.record.summary.id,
                    entry.record.summary.name,
                    entry.record.summary.role or "",
                )
                for entry in self._entries.values()
                if entry.record.summary.archived_at is None
            )
        )
        dialog.set_loading(True, "正在載入隊伍…")
        dialog.show()
        dialog.raise_()
        self._submit_dialog(_DialogOperation.TEAM_LOAD, self._load_team_bundle)

    @Slot(str)
    def focus_weapon_editor(self, character_id: str) -> None:
        if character_id in self._entries:
            self._page.focus_weapon_editor()

    @Slot(str, int)
    def open_echo_editor(self, character_id: str, slot_position: int) -> None:
        if self._closing:
            return
        dialog = self._ensure_echo_dialog()
        self._echo_generation += 1
        generation = self._echo_generation
        self._active_echo_context = (generation, character_id, slot_position)
        dialog.set_loading(True, "正在載入可用聲骸…")
        dialog.show()
        dialog.raise_()
        self._submit_dialog(
            _DialogOperation.ECHO_LOAD,
            lambda: self._load_echo_bundle(generation, character_id, slot_position),
            generation=generation,
        )

    def prepare_shutdown(self) -> None:
        """Quiesce UI callbacks and cancel queued reads without waiting."""

        self._closing = True
        self._executor.prepare_shutdown()

    def shutdown(self, *, timeout: float = 8.0) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        with self._future_lock:
            self._futures.clear()
        return True

    def _ensure_team_dialog(self) -> TeamEditorDialog:
        if self._team_dialog is None:
            self._team_dialog = TeamEditorDialog(self._page)
            self._team_dialog.team_selected.connect(self._select_team)
            self._team_dialog.save_requested.connect(self._save_team)
            self._team_dialog.delete_requested.connect(self._delete_team)
        return self._team_dialog

    def _ensure_echo_dialog(self) -> EchoSlotAssignmentDialog:
        if self._echo_dialog is None:
            self._echo_dialog = EchoSlotAssignmentDialog(self._page)
            self._echo_dialog.save_requested.connect(self._save_echo_assignment)
        return self._echo_dialog

    def _ensure_catalog_dialog(self) -> CharacterCatalogDialog:
        if self._catalog_dialog is None:
            self._catalog_dialog = CharacterCatalogDialog(self._page)
            self._catalog_dialog.import_requested.connect(self._import_catalog_characters)
        return self._catalog_dialog

    @Slot(object)
    def _import_catalog_characters(self, payload: object) -> None:
        dialog = self._catalog_dialog
        if self._closing or dialog is None:
            return
        if not isinstance(payload, CharacterCatalogSelectionRequest):
            dialog.show_error("角色目錄選取資料格式不正確。")
            return
        by_id = self._catalog.by_id()
        try:
            selected = tuple(by_id[identifier] for identifier in payload.entry_ids)
        except KeyError:
            dialog.show_error("角色目錄版本已變更, 請關閉後重新開啟。")
            return
        dialog.set_loading(True)
        self._page.set_loading(True, "正在加入勾選角色…")
        self._submit(
            _Operation.CATALOG_IMPORT,
            lambda: self._batch_import_and_reload(selected),
        )

    @Slot(str)
    def _select_team(self, team_id: str) -> None:
        team = self._team_by_id.get(team_id)
        if team is not None and self._team_dialog is not None:
            self._team_dialog.apply_team(_team_detail_view(team))

    @Slot(object)
    def _save_team(self, payload: object) -> None:
        if not isinstance(payload, TeamSaveRequest) or self._team_dialog is None:
            return
        self._team_dialog.set_loading(True, "正在儲存隊伍…")
        self._submit_dialog(
            _DialogOperation.TEAM_SAVE,
            lambda: self._save_team_and_reload(payload),
        )

    @Slot(str)
    def _delete_team(self, team_id: str) -> None:
        if self._team_dialog is None:
            return
        self._team_dialog.set_loading(True, "正在刪除隊伍…")
        self._submit_dialog(
            _DialogOperation.TEAM_DELETE,
            lambda: self._delete_team_and_reload(team_id),
        )

    @Slot(object)
    def _save_echo_assignment(self, payload: object) -> None:
        if not isinstance(payload, EchoSlotAssignmentRequest) or self._echo_dialog is None:
            return
        context = self._active_echo_context
        if context is None or (payload.character_id, payload.slot_position) != context[1:]:
            self._echo_dialog.show_error("聲骸位置已切換,請重新確認後再儲存。")
            return
        self._echo_dialog.set_loading(True, "正在更新永久聲骸…")
        self._submit_dialog(
            _DialogOperation.ECHO_SAVE,
            lambda: self._apply_echo_assignment(payload),
            generation=context[0],
        )

    def _save_and_reload(
        self,
        request: CharacterEditRequest,
        existing: _WorkspaceEntry | None,
    ) -> tuple[_Workspace, str]:
        goal = {} if existing is None else deepcopy(existing.record.cultivation_goal)
        # Save only panel inputs. Calculated values never overwrite measured scoring context.
        goal["panel_config"] = {
            "character_ascension": request.character_ascension,
            "weapon_ascension": request.weapon_ascension,
            "node_layout": 1,
        }
        if request.energy_target_override is None:
            goal.pop("scoring_energy_target", None)
        elif not 100 <= request.energy_target_override <= 1000:
            raise ValueError("效率目標需為 100% 至 1000%, 或依攻略參考")
        else:
            goal["scoring_energy_target"] = request.energy_target_override
        if request.panel_energy_regen is None:
            goal.pop("scoring_panel_energy_regen", None)
        elif not 100 <= request.panel_energy_regen <= 1000:
            raise ValueError("面板共鳴效率需為 100% 至 1000%, 或留為未知")
        else:
            goal["scoring_panel_energy_regen"] = request.panel_energy_regen
        description = request.cultivation_goal.strip()
        if description:
            goal["description"] = description
        else:
            goal.pop("description", None)
        goal["echo_slot_confirmations"] = {
            str(index): {
                "echo_id": (
                    None if existing is None else existing.record.echo_slots[index - 1].echo_id
                ),
                "set": values[0],
                "main_stat": values[1],
            }
            for index, values in enumerate(request.echo_validations, start=1)
        }
        goal["skill_order_custom"] = True
        skills = tuple(
            UpdateSkill(
                skill_key=skill.skill_key,
                name=skill.label,
                skill_type=skill.skill_key,
                current_level=skill.current_level,
                target_level=skill.target_level,
                is_core=skill.target_level >= 9,
                display_order=order,
            )
            for order, skill in enumerate(request.skills)
        )
        weapon = (
            None
            if request.weapon.name is None
            else UpsertWeapon(
                name=request.weapon.name,
                level=request.weapon.level or 0,
                resonance_rank=request.weapon.resonance_rank or 1,
            )
        )
        if existing is None:
            character: CreateCharacter | UpdateCharacter = CreateCharacter(
                name=request.name,
                element=request.element or None,
                role=request.role or None,
                is_candidate=request.is_candidate,
                priority=request.priority,
                level=request.level,
                resonance_chain=request.resonance_chain,
                scoring_profile_id=request.scoring_profile_id,
                cultivation_goal=goal,
                small_stat_nodes=request.attribute_nodes,
                notes=request.notes or None,
                skills=tuple(
                    SkillDefinition(
                        skill.skill_key,
                        skill.label,
                        skill.skill_key,
                        skill.is_core,
                        skill.target_level,
                    )
                    for skill in request.skills
                ),
            )
            character_id = None
        else:
            character = UpdateCharacter(
                name=request.name,
                element=request.element or None,
                role=request.role or None,
                is_candidate=request.is_candidate,
                priority=request.priority,
                level=request.level,
                resonance_chain=request.resonance_chain,
                scoring_profile_id=request.scoring_profile_id,
                notes=request.notes or None,
                cultivation_goal=goal,
                small_stat_nodes=request.attribute_nodes,
            )
            character_id = existing.record.summary.id
        saved = self._service.save_character(
            data_set_id=self._data_set_id,
            character_id=character_id,
            command=SaveCharacter(
                character=character,
                weapon=weapon,
                skills=skills,
                remove_weapon=weapon is None
                and existing is not None
                and existing.record.weapon is not None,
            ),
        )
        return self._load_workspace(), saved.summary.id

    def _archive_and_reload(
        self,
        character_id: str,
        *,
        restore: bool,
    ) -> tuple[_Workspace, str | None]:
        if restore:
            self._service.restore_character(
                character_id,
                data_set_id=self._data_set_id,
            )
            selected: str | None = character_id
        else:
            self._service.archive_character(character_id, data_set_id=self._data_set_id)
            selected = None
        return self._load_workspace(), selected

    def _batch_import_and_reload(
        self,
        entries: tuple[CharacterCatalogEntry, ...],
    ) -> _CatalogImportOutcome:
        result = self._service.batch_create_characters(
            data_set_id=self._data_set_id,
            commands=tuple(
                CreateCharacter(
                    name=entry.name,
                    element=entry.element,
                    is_candidate=True,
                    scoring_profile_id=None,
                )
                for entry in entries
            ),
        )
        created_names = tuple(detail.summary.name for detail in result.created)
        return _CatalogImportOutcome(
            workspace=self._load_workspace(),
            selected_id=None if not result.created else result.created[0].summary.id,
            created_names=created_names,
            skipped_names=result.skipped_names,
        )

    def _load_team_bundle(self) -> _TeamBundle:
        return _TeamBundle(self._service.list_teams(data_set_id=self._data_set_id))

    def _save_team_and_reload(self, request: TeamSaveRequest) -> tuple[_TeamBundle, str]:
        members = tuple(
            TeamMemberInput(character_id, slot_index)
            for slot_index, character_id in enumerate(request.member_ids, start=1)
            if character_id is not None
        )
        team_command: CreateTeam | UpdateTeam
        if request.identifier is None:
            team_command = CreateTeam(request.name, request.priority, request.notes or None)
        else:
            team_command = UpdateTeam(request.name, request.priority, request.notes or None)
        saved = self._service.save_team(
            data_set_id=self._data_set_id,
            team_id=request.identifier,
            command=SaveTeam(team_command, members),
        )
        return self._load_team_bundle(), saved.id

    def _delete_team_and_reload(self, team_id: str) -> _TeamBundle:
        self._service.delete_team(team_id, data_set_id=self._data_set_id)
        return self._load_team_bundle()

    def _load_echo_bundle(
        self,
        generation: int,
        character_id: str,
        slot_position: int,
    ) -> _EchoBundle:
        detail = self._service.get_character(character_id, data_set_id=self._data_set_id)
        slot = detail.echo_slots[slot_position - 1]
        context = EchoSlotContextView(
            character_id=character_id,
            character_name=detail.summary.name,
            slot_position=slot_position,
            current_echo_id=slot.echo_id,
            current_echo_label=(
                None
                if slot.echo_id is None
                else f"{slot.name or '未知聲骸'} · {slot.set_name or '未知套裝'}"
            ),
        )
        candidates = self._service.list_echoes_for_assignment(
            data_set_id=self._data_set_id,
            character_id=character_id,
        )
        choices = tuple(
            EchoAssignmentChoiceView(
                identifier=item.id,
                name=item.name,
                set_name=item.set_name,
                cost=item.cost,
                level=item.level,
                score=item.score,
                assigned_character_id=item.current_character_id,
                assigned_character_name=item.current_character_name,
                assigned_slot_position=item.current_slot_index,
            )
            for item in candidates
        )
        return _EchoBundle(generation, context, choices)

    def _apply_echo_assignment(self, request: EchoSlotAssignmentRequest) -> tuple[str, int]:
        if request.is_unassign:
            self._service.unassign_echo_slot(
                request.character_id,
                request.slot_position,
                data_set_id=self._data_set_id,
            )
        else:
            assert request.echo_id is not None
            self._service.assign_echo_slot(
                request.character_id,
                request.slot_position,
                request.echo_id,
                data_set_id=self._data_set_id,
                replace=request.replace_confirmed,
            )
        return request.character_id, request.slot_position

    def _load_workspace(self) -> _Workspace:
        profiles = self._profiles.list_profiles(data_set_id=self._data_set_id)
        default = self._profiles.get_default(data_set_id=self._data_set_id)
        profile_by_id = {profile.id: profile for profile in profiles}
        details = self._service.list_character_details(
            data_set_id=self._data_set_id, include_archived=True
        )
        overview = self._service.equipment_overview(
            data_set_id=self._data_set_id,
            profile_id=self._comparison_profile_id,
            character_details=details,
        )
        overview_by_id = {item.character_id: item for item in overview.characters}
        entries: list[_WorkspaceEntry] = []
        rows: list[CharacterSummaryView] = []
        for detail in details:
            summary = detail.summary
            selected_profile = profile_by_id.get(summary.scoring_profile_id or "", default)
            passing = 70.0 if selected_profile is None else selected_profile.thresholds.passing
            evaluated = evaluate_character_detail(
                detail,
                passing_score=passing,
                scoring_profile_id=None if selected_profile is None else selected_profile.id,
                scoring_profile_version=(
                    None if selected_profile is None else selected_profile.version
                ),
            )
            view = _detail_view(detail, evaluated.completion, evaluated.next_action)
            combat_profile = character_profile(
                self._profiles,
                data_set_id=self._data_set_id,
                name=summary.name,
                explicit_profile_id=summary.scoring_profile_id,
                profile_snapshots=tuple(profiles),
            )
            weights = (
                None
                if combat_profile is None
                else {str(w.stat): w.weight for w in profile_from_snapshot(combat_profile).weights}
            )
            combat = evaluate_build(detail, {d.summary.id: d for d in details}, weights=weights)
            view = replace(
                view,
                combat_summary=combat.summary,
                combat_details=combat.details() if combat.teams else "",
            )
            if summary.id in overview_by_id:
                view = _with_equipment_scores(
                    view, overview_by_id[summary.id], overview.profile_name
                )
            entries.append(_WorkspaceEntry(detail, view))
            rows.append(
                CharacterSummaryView(
                    identifier=summary.id,
                    name=summary.name,
                    element=summary.element or "",
                    role=summary.role or "",
                    is_candidate=summary.is_candidate,
                    priority=summary.priority,
                    completion_percent=evaluated.completion.overall_percent,
                    archived=summary.archived_at is not None,
                )
            )
        return _Workspace(
            rows=tuple(rows),
            entries=tuple(entries),
            profiles=tuple(ChoiceOptionView(profile.id, profile.name) for profile in profiles),
            default_profile_id=None if default is None else default.id,
            overview=overview,
        )

    def _submit(
        self,
        operation: _Operation,
        work: Callable[[], object],
        *,
        selected_id: str | None = None,
    ) -> None:
        if self._closing:
            return

        def run() -> _JobResult:
            try:
                value = work()
                if isinstance(value, _Workspace):
                    return _JobResult(operation, workspace=value, selected_id=selected_id)
                if isinstance(value, _CatalogImportOutcome):
                    return _JobResult(
                        operation,
                        workspace=value.workspace,
                        selected_id=value.selected_id,
                        created_names=value.created_names,
                        skipped_names=value.skipped_names,
                    )
                if (
                    isinstance(value, tuple)
                    and len(value) == 2
                    and isinstance(value[0], _Workspace)
                ):
                    return _JobResult(
                        operation, workspace=value[0], selected_id=cast(str | None, value[1])
                    )
                raise TypeError("character presenter job returned an invalid result")
            except Exception as exc:  # UI boundary turns storage/domain errors into text.
                return _JobResult(operation, error=str(exc))

        future = cast(
            Future[object],
            self._executor.submit(
                run, read_only=operation in {_Operation.LOAD, _Operation.LOAD_EXTERNAL}
            ),
        )
        with self._future_lock:
            self._futures.add(future)

        def completed(done: Future[object]) -> None:
            with self._future_lock:
                self._futures.discard(done)
            if not self._closing:
                self._job_completed.emit(done.result())

        future.add_done_callback(completed)

    def _submit_dialog(
        self,
        operation: _DialogOperation,
        work: Callable[[], object],
        *,
        generation: int | None = None,
    ) -> None:
        if self._closing:
            return

        def run() -> _DialogJobResult:
            try:
                value = work()
                selected: str | None = None
                if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], str):
                    value, selected = value
                return _DialogJobResult(
                    operation,
                    value=value,
                    selected_id=selected,
                    generation=generation,
                )
            except Exception as exc:
                return _DialogJobResult(operation, error=str(exc), generation=generation)

        future = cast(
            Future[object],
            self._executor.submit(
                run, read_only=operation in {_DialogOperation.TEAM_LOAD, _DialogOperation.ECHO_LOAD}
            ),
        )
        with self._future_lock:
            self._futures.add(future)

        def completed(done: Future[object]) -> None:
            with self._future_lock:
                self._futures.discard(done)
            if not self._closing:
                self._dialog_job_completed.emit(done.result())

        future.add_done_callback(completed)

    @Slot(object)
    def _handle_job_result(self, payload: object) -> None:
        if self._closing or not isinstance(payload, _JobResult):
            return
        if payload.error is not None or payload.workspace is None:
            if payload.operation is _Operation.COMBAT_SAVE and self._combat_dialog is not None:
                self._combat_dialog.set_result(payload.error or "儲存失敗")
            if payload.operation is _Operation.CATALOG_IMPORT and self._catalog_dialog is not None:
                self._catalog_dialog.show_error(payload.error or "角色目錄匯入失敗。")
            self._page.show_error(payload.error or "角色資料操作失敗。")
            return
        self._apply_workspace(
            payload.workspace,
            payload.selected_id,
            preserve_editor=payload.operation
            in {_Operation.LOAD_EXTERNAL, _Operation.RESET_CHECK, _Operation.COMBAT_SAVE},
            accept_stale_mutation=payload.operation
            in {
                _Operation.SAVE,
                _Operation.ARCHIVE,
                _Operation.CATALOG_IMPORT,
                _Operation.COMBAT_SAVE,
            },
        )
        if payload.operation in {
            _Operation.SAVE,
            _Operation.ARCHIVE,
            _Operation.CATALOG_IMPORT,
            _Operation.COMBAT_SAVE,
        }:
            self.scoring_context_changed.emit()
        if payload.operation is _Operation.COMBAT_SAVE and self._combat_dialog is not None:
            self._combat_dialog.set_result()
        if payload.operation is _Operation.RESET_CHECK:
            self.equipment_recheck_ready.emit()
        if payload.operation is _Operation.CATALOG_IMPORT:
            self._apply_catalog_to_dialog()
            if self._catalog_dialog is not None:
                self._catalog_dialog.show_result(payload.created_names, payload.skipped_names)
            created_count = len(payload.created_names)
            skipped_count = len(payload.skipped_names)
            self._page.show_status(
                f"角色目錄完成: 新增 {created_count} 名, 已存在略過 {skipped_count} 名。"
            )

    def _apply_workspace(
        self,
        workspace: _Workspace,
        selected_id: str | None,
        *,
        preserve_editor: bool = False,
        accept_stale_mutation: bool = False,
    ) -> None:
        comparison_stale = (
            workspace.overview is not None
            and self._comparison_profile_id is not None
            and workspace.overview.profile_id != self._comparison_profile_id
        )
        if comparison_stale and not accept_stale_mutation:
            return
        self._entries = {entry.record.summary.id: entry for entry in workspace.entries}
        self._default_profile_id = workspace.default_profile_id
        if workspace.overview is not None and not comparison_stale:
            by_id = {entry.record.summary.id: entry for entry in workspace.entries}
            overview = workspace.overview
            self._page.set_equipment_details(
                tuple(_gear_detail(item) for item in overview.characters)
            )
            self._page.overview_page.apply_data(
                tuple(
                    _equipment_card(item, by_id[item.character_id].view)
                    for item in overview.characters
                ),
                tuple(_equipment_piece(item) for item in overview.weakest_echoes if item.echo_id),
                profiles=(
                    (AUTO_PROFILE_ID, "依角色需求(自訂優先/已查核攻略)"),
                    *((profile.identifier, profile.label) for profile in workspace.profiles),
                ),
                profile_id=overview.profile_id,
                summary=(
                    f"{len(overview.characters)} 名角色 · "
                    f"{sum(item.equipped_count for item in overview.characters)} 件目前裝備 · "
                    f"{overview.physical_echo_count} 筆紀錄折疊為 "
                    f"{overview.logical_echo_count} 組聲骸"
                ),
            )
        if preserve_editor:
            self._page.apply_external_characters(
                workspace.rows, tuple(entry.view for entry in workspace.entries)
            )
            self._page.set_loading(False)
        else:
            self._page.set_scoring_profiles(workspace.profiles)
            self._page.set_characters(workspace.rows)
            selected = selected_id
            if selected is None and workspace.rows:
                selected = next(
                    (row.identifier for row in workspace.rows if not row.archived), None
                )
            if selected is not None and self._page.select_character(selected):
                entry = self._entries.get(selected)
                if entry is not None:
                    self._page.apply_character(entry.view)
        choices = tuple(
            ChoiceOption(row.identifier, row.name)
            for row in workspace.rows
            if row.is_candidate and not row.archived
        )
        self.character_choices_changed.emit(choices)
        active = tuple(row for row in workspace.rows if row.is_candidate and not row.archived)
        active_ids = {row.identifier for row in active}
        overview_characters = () if workspace.overview is None else workspace.overview.characters
        scored = [
            item.average_score
            for item in overview_characters
            if item.character_id in active_ids and item.average_score is not None
        ]
        score_summary = (
            "聲骸資料待補"
            if not scored
            else f"聲骸均分 {sum(scored) / len(scored):.1f} ({len(scored)} 名可評分)"
        )
        passing = sum(
            entry.view.passing_echoes
            for entry in workspace.entries
            if entry.view.is_candidate and not entry.view.archived
        )
        has_echo_data = any(
            slot.echo_id is not None
            for entry in workspace.entries
            for slot in entry.view.echo_slots
        )
        self.dashboard_snapshot_changed.emit(
            DashboardSnapshot(
                character_progress=(
                    "尚未建立常用角色"
                    if not active
                    else f"{len(active)} 名常用角色 · {score_summary}"
                ),
                qualified_echoes=f"{passing} 顆永久聲骸達到及格",
                today_priority=(
                    "先建立第一名常用角色"
                    if not active
                    else min(active, key=lambda row: (row.priority, row.name)).name
                ),
                has_character_data=bool(workspace.rows),
                has_echo_data=has_echo_data,
            )
        )

    def _apply_catalog_to_dialog(self) -> None:
        dialog = self._catalog_dialog
        if dialog is None:
            return
        existing_names = {entry.record.summary.name.casefold() for entry in self._entries.values()}
        dialog.set_catalog(
            tuple(
                CharacterCatalogItemView(
                    identifier=entry.identifier,
                    name=entry.name,
                    element=entry.element,
                    weapon_type=entry.weapon_type,
                    rarity=entry.rarity,
                    source_status=entry.source_status,
                    already_added=entry.name.casefold() in existing_names,
                )
                for entry in self._catalog.entries
            ),
            version=self._catalog.catalog_version,
            as_of_date=self._catalog.as_of_date,
            disclaimer=self._catalog.disclaimer,
        )

    @Slot(object)
    def _handle_dialog_job_result(self, payload: object) -> None:
        if self._closing or not isinstance(payload, _DialogJobResult):
            return
        if payload.operation is _DialogOperation.ECHO_SAVE and payload.error is None:
            self.scoring_context_changed.emit()
        if payload.operation in {
            _DialogOperation.TEAM_LOAD,
            _DialogOperation.TEAM_SAVE,
            _DialogOperation.TEAM_DELETE,
        }:
            dialog = self._team_dialog
            if dialog is None:
                return
            if payload.error is not None or not isinstance(payload.value, _TeamBundle):
                dialog.show_error(payload.error or "隊伍資料操作失敗。")
                return
            self._apply_team_bundle(payload.value, payload.selected_id)
            if payload.operation is not _DialogOperation.TEAM_LOAD:
                dialog.show_status("隊伍資料已儲存。")
                self.refresh()
            return
        echo_dialog = self._echo_dialog
        if echo_dialog is None:
            return
        active_context = self._active_echo_context
        if active_context is None or payload.generation != active_context[0]:
            if payload.operation is _DialogOperation.ECHO_SAVE and payload.error is None:
                self.refresh()
            return
        if payload.error is not None:
            echo_dialog.show_error(payload.error)
            return
        if payload.operation is _DialogOperation.ECHO_LOAD:
            if not isinstance(payload.value, _EchoBundle):
                echo_dialog.show_error("可用聲骸資料格式不正確。")
                return
            if payload.value.generation != active_context[0]:
                return
            echo_dialog.set_context(payload.value.context)
            echo_dialog.set_echo_choices(payload.value.choices)
            echo_dialog.set_selected_echo(payload.value.context.current_echo_id)
            echo_dialog.set_loading(False, "請選擇要裝備的聲骸。")
            return
        if (
            payload.operation is _DialogOperation.ECHO_SAVE
            and isinstance(payload.value, tuple)
            and len(payload.value) == 2
            and isinstance(payload.value[0], str)
            and isinstance(payload.value[1], int)
        ):
            echo_dialog.show_status("永久聲骸指派已更新。")
            self.refresh()
            character_id, slot_position = payload.value
            context = self._active_echo_context
            if context is None or (character_id, slot_position) != context[1:]:
                return
            self._submit_dialog(
                _DialogOperation.ECHO_LOAD,
                lambda: self._load_echo_bundle(context[0], character_id, slot_position),
                generation=context[0],
            )

    def _apply_team_bundle(self, bundle: _TeamBundle, selected_id: str | None) -> None:
        dialog = self._team_dialog
        if dialog is None:
            return
        self._team_by_id = {team.id: team for team in bundle.teams}
        dialog.set_teams(tuple(_team_summary_view(team) for team in bundle.teams))
        selected = selected_id or dialog.current_team_id()
        if selected is not None and selected in self._team_by_id:
            dialog.apply_team(_team_detail_view(self._team_by_id[selected]))
        elif not bundle.teams:
            dialog.begin_create()
        dialog.set_loading(False, "隊伍資料已載入。")


def _detail_view(
    detail: CharacterDetail,
    completion: CultivationCompletion,
    next_action: str,
) -> CharacterDetailView:
    result = completion
    summary = detail.summary
    first_team = detail.teams[0] if detail.teams else None
    assessed = {slot.slot_index: slot for slot in result.echo_slots}
    config = detail.cultivation_goal.get("panel_config", {})
    if not isinstance(config, dict):
        config = {}
    return CharacterDetailView(
        identifier=summary.id,
        name=summary.name,
        element=summary.element or "",
        role=summary.role or "",
        is_candidate=summary.is_candidate,
        priority=summary.priority,
        level=summary.level,
        resonance_chain=summary.resonance_chain,
        scoring_profile_id=summary.scoring_profile_id,
        scoring_profile_name=summary.scoring_profile_name,
        weapon=(
            WeaponProgressView()
            if detail.weapon is None
            else WeaponProgressView(
                detail.weapon.name,
                detail.weapon.level,
                detail.weapon.resonance_rank,
            )
        ),
        team_id=None if first_team is None else first_team.team_id,
        team_name=None if not detail.teams else "、".join(team.team_name for team in detail.teams),
        skills=tuple(
            SkillProgressView(
                skill.skill_key,
                skill.name,
                skill.current_level,
                skill.target_level,
                skill.is_core,
            )
            for skill in (
                detail.skills
                if detail.cultivation_goal.get("skill_order_custom") is True
                else sorted(detail.skills, key=lambda row: (-row.target_level, row.display_order))
            )
        ),
        attribute_nodes=detail.small_stat_nodes,
        echo_slots=tuple(
            PermanentEchoSlotView(
                position=slot.slot_index,
                echo_id=slot.echo_id,
                echo_name=slot.name,
                set_name=slot.set_name,
                main_stat=_main_stat_label(slot.main_stats),
                cost=slot.cost,
                level=slot.level,
                score=assessed[slot.slot_index].score,
                is_passing=assessed[slot.slot_index].is_passing,
                set_is_valid=_stored_echo_validity(
                    detail.cultivation_goal,
                    slot.slot_index,
                    slot.echo_id,
                    "set",
                ),
                main_stat_is_valid=_stored_echo_validity(
                    detail.cultivation_goal,
                    slot.slot_index,
                    slot.echo_id,
                    "main_stat",
                ),
            )
            for slot in detail.echo_slots
        ),
        cultivation_goal=str(detail.cultivation_goal.get("description", "")),
        panel_energy_regen=detail.cultivation_goal.get("scoring_panel_energy_regen"),
        energy_target_override=detail.cultivation_goal.get("scoring_energy_target"),
        calculated_panel=calculate_character_panel(detail),
        equipment_check=equipment_check(detail),
        character_ascension=config.get("character_ascension"),
        weapon_ascension=config.get("weapon_ascension"),
        notes=detail.notes or "",
        completion_percent=result.overall_percent,
        passing_echoes=result.echo_passing_count,
        weakest_position=f"位置 {result.weakest_echo_slot}",
        next_suggestion=next_action,
        archived=summary.archived_at is not None,
        unpositioned_echoes=tuple(
            f"{echo.name or '名稱待確認'} · "
            f"{('+' + str(echo.level)) if echo.level is not None else '等級待確認'} · "
            f"COST {echo.cost if echo.cost is not None else '—'} · "
            f"{echo.set_name or '套裝待辨識'}"
            for echo in detail.unpositioned_echoes
        ),
    )


def _equipment_piece(piece: EquippedEchoScore) -> EquipmentPieceView:
    return EquipmentPieceView(
        echo_id=piece.echo_id or "",
        character_id=piece.character_id,
        character_name=piece.character_name,
        name=piece.name or "名稱待確認",
        slot=piece.slot_index,
        level=piece.level,
        set_name=piece.set_name,
        score=piece.score,
        reason="、".join(piece.notes),
        fit_alert=equipment_fit_alert(piece),
        fit_summary=equipment_fit_alert(piece, brief=True),
        scan_count=piece.rescan_count + 1,
        is_passing=piece.is_passing,
    )


def _equipment_card(
    item: CharacterEquipmentOverview, detail: CharacterDetailView
) -> EquipmentCharacterView:
    categories = {category.key: category for category in item.categories}

    def done(key: str) -> bool:
        return key in categories and categories[key].at_goal

    return EquipmentCharacterView(
        identifier=item.character_id,
        name=item.character_name,
        element=item.element or detail.element,
        average=item.average_score
        if item.average_score is not None
        else item.partial_average_score,
        total=item.total_score,
        scored_count=item.scored_count,
        equipped_count=item.equipped_count,
        pieces=tuple(_equipment_piece(piece) for piece in item.slots if piece.echo_id),
        level_done=done("character"),
        weapon_done=done("weapon"),
        skills_done=done("skills"),
        echoes_done=done("echoes"),
        level_label=f"角色 Lv.{detail.level}",
        weapon_label=(f"武器 Lv.{detail.weapon.level}" if detail.weapon.name else "武器待登記"),
        skills_label=(
            f"技能 {sum(skill.current_level >= skill.target_level for skill in detail.skills)}/5"
        ),
        archived=detail.archived,
        profile_label=item.profile_name or "尚未設定角色需求",
        scoring_notes="; ".join(item.scoring_notes),
        guidance_html=_guidance_html(item),
        role_label=item.role,
        priority_label=_priority_label(item),
    )


def _guidance_html(item: CharacterEquipmentOverview) -> str:
    guide = guide_for_character(item.character_name)
    selected = (
        f"<p><b>目前使用: {escape(item.profile_name or '未設定')}</b><br>"
        f"目前有效權重: {escape(item.weight_summary or '無')}<br>"
        f"{escape('; '.join(item.scoring_notes))}</p>"
    )
    if guide is None:
        return (
            selected + "<p>尚無已查核攻略。未知角色不自動套通用輸出型。"
            "可依遊戲內推薦在評分設定建立個人權重, 再於角色選用。</p>"
        )
    if item.template_kind != guide.template_kind or not item.profile_is_system:
        selected += "<p>目前採用自行指定/共同比較的設定檔; 以下攻略僅供參考, 未套用其權重。</p>"
    return selected + guide.html()


def _priority_label(item: CharacterEquipmentOverview) -> str:
    weakest = min(
        (p for p in item.slots if p.score is not None), key=lambda p: p.score or 0, default=None
    )
    if weakest is None:
        return f"已保存 {item.equipped_count} 件裝備"
    advice = weakest.training_recommendation
    return f"先看位置 {weakest.slot_index} · {weakest.score:.1f} 分" + (
        " · " + advice.action if advice else ""
    )


def _gear_detail(item: CharacterEquipmentOverview) -> GearCharacterDetailView:
    pieces = []
    for p in item.slots:
        advice = p.training_recommendation
        target = (
            ""
            if advice is None
            else (
                "替換方向: "
                + " / ".join(advice.target_main_stats)
                + " | 優先詞條: "
                + "、".join(advice.target_substats)
            )
        )
        pieces.append(
            GearPieceDetailView(
                position=p.slot_index or 0,
                echo_id=p.echo_id,
                name=p.name or ("名稱待確認" if p.echo_id else "尚無裝備紀錄"),
                level=p.level,
                cost=p.cost,
                set_name=p.set_name or "套裝尚未辨識",
                main_stats=tuple(
                    f"{s.get('name') or '?'} "
                    f"{s.get('value') if s.get('value') is not None else '?'}{s.get('unit') or ''}"
                    for s in p.main_stats
                ),
                score=p.score,
                stats=tuple(
                    SubstatLineView(
                        name=s.name,
                        value=f"{s.value:g}{s.unit}" if s.value is not None else "—",
                        relevance=s.relevance
                        + (f" ({s.weight:g})" if s.weight is not None else ""),
                        contribution=s.contribution,
                        roll_quality=s.roll_quality,
                        detail=(
                            f"角色權重 {s.weight if s.weight is not None else '未知'}"
                            f" · {s.roll_quality}"
                        ),
                    )
                    for s in p.stat_details
                ),
                fit_alert=equipment_fit_alert(p),
                fit=f"套裝: {p.set_fit_reason} · 主詞條: {p.main_stat_fit_reason}",
                action="" if advice is None else advice.action,
                reason="" if advice is None else advice.reason,
                target=target,
                score_note="; ".join(p.notes),
                is_passing=p.is_passing,
            )
        )
    average = item.average_score if item.average_score is not None else item.partial_average_score
    return GearCharacterDetailView(
        identifier=item.character_id,
        name=item.character_name,
        subtitle=f"{item.element} · {item.role}",
        score_summary="資料待補"
        if average is None
        else f"{average:.1f} 均分 · {item.scored_count}/5 件",
        set_summary=item.set_summary,
        effective_weights=item.weight_summary,
        priority_stats="、".join(
            dict.fromkeys(
                stat
                for piece in item.slots
                if piece.training_recommendation
                for stat in piece.training_recommendation.target_substats
            )
        ),
        basis=(
            f"{item.profile_name or '尚未指定角色需求'}"
            " · 分數為下方副詞條得分加總; 套裝與主詞條另列。"
        ),
        context_note="; ".join(item.scoring_notes),
        pieces=tuple(pieces),
        pending_echoes=tuple(
            f"{p.name or '聲骸'} +{p.level if p.level is not None else '?'}"
            + (f" ({p.score:.1f} 分)" if p.score is not None else "")
            for p in item.pending_echoes
        ),
    )


def _with_equipment_scores(
    view: CharacterDetailView,
    item: CharacterEquipmentOverview,
    profile_name: str | None,
) -> CharacterDetailView:
    scores = {piece.slot_index: piece for piece in item.slots}
    slots = tuple(
        replace(
            slot,
            echo_id=scores[slot.position].echo_id,
            echo_name=scores[slot.position].name,
            set_name=scores[slot.position].set_name,
            cost=scores[slot.position].cost,
            level=scores[slot.position].level,
            score=scores[slot.position].score,
            main_stat=_main_stat_label(scores[slot.position].main_stats),
            is_passing=scores[slot.position].is_passing,
            score_note="、".join(scores[slot.position].notes),
            set_is_valid=(
                slot.set_is_valid if slot.echo_id == scores[slot.position].echo_id else None
            ),
            main_stat_is_valid=(
                slot.main_stat_is_valid if slot.echo_id == scores[slot.position].echo_id else None
            ),
        )
        for slot in view.echo_slots
    )
    lowest = min(
        (piece for piece in item.slots if piece.score is not None),
        key=lambda piece: piece.score or 0,
        default=None,
    )
    return replace(
        view,
        echo_slots=slots,
        passing_echoes=sum(piece.is_passing is True for piece in item.slots),
        weakest_position=(
            "尚未評估" if lowest is None else f"位置 {lowest.slot_index} · {lowest.score:.1f} 分"
        ),
        next_suggestion=(
            f"評分基準: {item.profile_name or profile_name or '尚未設定'}。"
            + (
                f"先查看位置 {lowest.slot_index} 的聲骸。"
                if lowest is not None
                else "先補齊裝備與詞條資料。"
            )
        ),
        unpositioned_echoes=tuple(
            f"{piece.name or '名稱待確認'} · +{piece.level if piece.level is not None else '?'} · "
            f"{piece.set_name or '套裝待辨識'} · "
            f"{f'{piece.score:.1f} 分' if piece.score is not None else '待評分'}"
            + (f" (重複掃描 {piece.rescan_count + 1} 次)" if piece.rescan_count else "")
            for piece in item.pending_echoes
        ),
    )


def _main_stat_label(stats: tuple[dict[str, object], ...]) -> str | None:
    if not stats:
        return None
    stat = stats[0]
    name = str(stat.get("name", "")).strip()
    value = stat.get("value")
    unit = str(stat.get("unit", ""))
    if not name:
        return None
    return name if value is None else f"{name} {value}{unit}"


def _team_summary_view(team: TeamSnapshot) -> TeamSummaryView:
    names: list[str | None] = [None, None, None]
    for member in team.members:
        names[member.slot_index - 1] = member.character_name
    return TeamSummaryView(
        identifier=team.id,
        name=team.name,
        priority=team.priority,
        member_names=(names[0], names[1], names[2]),
    )


def _team_detail_view(team: TeamSnapshot) -> TeamDetailView:
    members: list[str | None] = [None, None, None]
    for member in team.members:
        members[member.slot_index - 1] = member.character_id
    return TeamDetailView(
        identifier=team.id,
        name=team.name,
        priority=team.priority,
        notes=team.notes or "",
        member_ids=(members[0], members[1], members[2]),
    )


def _stored_echo_validity(
    goal: dict[str, object],
    slot_index: int,
    echo_id: str | None,
    key: str,
) -> bool | None:
    confirmations = goal.get("echo_slot_confirmations")
    if not isinstance(confirmations, dict):
        return None
    slot = confirmations.get(str(slot_index))
    if not isinstance(slot, dict):
        return None
    if echo_id is None or slot.get("echo_id") != echo_id:
        return None
    value = slot.get(key)
    return value if type(value) is bool else None


__all__ = ["CharactersPresenter"]
