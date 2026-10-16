"""Desktop application pages."""

from .characters import CharactersPage
from .dashboard import DashboardPage, DashboardSnapshot
from .echo_live import EchoLivePage
from .gacha import GachaPage
from .history import EchoHistoryPage
from .onboarding import (
    CaptureTestRequest,
    MonitorChoice,
    OnboardingRequest,
    OnboardingWizard,
)
from .scoring_profiles import ScoringProfilesPage
from .settings import MonitorOption, SettingsPage
from .tasks_events import TasksEventsPage
from .today_plan import TodayPlanPage

__all__ = [
    "CaptureTestRequest",
    "CharactersPage",
    "DashboardPage",
    "DashboardSnapshot",
    "EchoHistoryPage",
    "EchoLivePage",
    "GachaPage",
    "MonitorChoice",
    "MonitorOption",
    "OnboardingRequest",
    "OnboardingWizard",
    "ScoringProfilesPage",
    "SettingsPage",
    "TasksEventsPage",
    "TodayPlanPage",
]
