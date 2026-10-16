"""Database-free desktop dialogs and their immutable presentation DTOs."""

from wuwa_assistant.ui.dialogs.character_catalog import (
    CharacterCatalogDialog,
    CharacterCatalogItemView,
    CharacterCatalogSelectionRequest,
)
from wuwa_assistant.ui.dialogs.character_links import (
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
from wuwa_assistant.ui.dialogs.planning_data import PlanningDataDialog

__all__ = [
    "CharacterCatalogDialog",
    "CharacterCatalogItemView",
    "CharacterCatalogSelectionRequest",
    "CharacterChoiceView",
    "EchoAssignmentChoiceView",
    "EchoSlotAssignmentDialog",
    "EchoSlotAssignmentRequest",
    "EchoSlotContextView",
    "PlanningDataDialog",
    "TeamDetailView",
    "TeamEditorDialog",
    "TeamSaveRequest",
    "TeamSummaryView",
]
