"""Visible fit issues from the same evaluated evidence used by planning."""

from __future__ import annotations

from .equipment_analysis import MAIN_LABELS, primary_main_key
from .equipment_overview import EquippedEchoScore


def equipment_fit_alert(piece: EquippedEchoScore, *, brief: bool = False) -> str:
    if piece.echo_id is None:
        return ""
    issues = []
    if piece.set_is_valid is False:
        issues.append("套裝不符")
    if piece.main_stat_is_valid is False:
        text = "主詞條不符"
        if not brief:
            key = primary_main_key(piece.main_stats)
            actual = MAIN_LABELS.get(key or "", key or "待確認")
            advice = piece.training_recommendation
            expected = (
                "依自訂配裝目標核對"
                if "自訂" in piece.main_stat_fit_reason
                else " / ".join(advice.target_main_stats)
                if advice and advice.target_main_stats
                else "建議方向待確認"
            )
            text += f": 目前 {actual}; 建議 {expected}"
        issues.append(text)
    elif piece.main_stat_is_valid is None:
        issues.append("主詞條待確認")
    return " · ".join(issues)
