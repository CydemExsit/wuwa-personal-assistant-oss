"""Array-free projection shared by the full workspace and passive panel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EchoPanelSnapshot:
    name: str = "等待聲骸辨識"
    level: int | None = None
    score: float | None = None
    target_score: float | None = None
    save_advice: str = "等待辨識"
    upgrade_advice: str = "等待辨識"
    basis: str = "尚未取得評分結果"
    status: str = "等待聲骸畫面"
    detail: str = "請在遊戲聲骸清單選擇聲骸。"
    missing_fields: tuple[str, ...] = ()
    stale: bool = False


def retention_advice(action: str, *, qualified: bool) -> str:
    """Summarize existing decisions without treating stop-upgrading as discard."""

    if action == "manual_confirmation":
        return "資料不足, 請先確認"
    if action == "qualified_finish" or qualified:
        return "達到目前評分門檻, 建議保留"
    if action == "continue":
        return "暫留候選, 完成下一階段再評估"
    if action == "cannot_surpass_existing":
        return "無法超越此目標, 其他角色用途待評估"
    if action in {"stop", "max_level"}:
        return "未達目前門檻, 其他角色用途待評估"
    return "等待評分"
