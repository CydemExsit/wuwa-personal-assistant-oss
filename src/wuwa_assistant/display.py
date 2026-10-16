"""Display-edition identity and validated, persistence-neutral display choices."""

from __future__ import annotations

from dataclasses import dataclass

APP_ID = "WuwaDisplayAssistant"
APP_TITLE = "鳴潮養成助理 · 雙模式版"
DISPLAY_MODE_KEY = "display/mode"
RESOLUTION_KEY = "capture/resolution"
PANEL_PRESET_KEY = "display/panel_preset"
PANEL_POSITION_KEY = "display/panel_position"
DISPLAY_SETUP_KEY = "display/setup_complete"
DISPLAY_MODES = {"single": "單螢幕 · 小面板", "dual": "雙螢幕 · 完整工具"}
PANEL_PRESETS = {
    "top_left": "左上角",
    "top_right": "右上角",
    "bottom_left": "左下角",
    "bottom_right": "右下角",
    "center_left": "左側中央",
    "custom": "自訂位置 (Ctrl+Shift 拖曳)",
}
RESOLUTIONS = {"1920x1080": (1920, 1080), "2560x1440": (2560, 1440)}
DEFAULT_RESOLUTION = "2560x1440"


@dataclass(frozen=True, slots=True)
class DisplayPreferences:
    mode: str = "dual"
    resolution: str = DEFAULT_RESOLUTION
    panel_preset: str = "top_left"

    def __post_init__(self) -> None:
        if self.mode not in DISPLAY_MODES:
            raise ValueError("請選擇單螢幕或雙螢幕模式")
        if self.resolution not in RESOLUTIONS:
            raise ValueError("請選擇 1920x1080 或 2560x1440")
        if self.panel_preset not in PANEL_PRESETS:
            raise ValueError("小面板位置設定無效")
