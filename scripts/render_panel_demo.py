"""Render only an owned widget with fabricated UI values; no game/DB capture."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from wuwa_assistant.ui.echo_panel import EchoPanel
from wuwa_assistant.ui.panel_model import EchoPanelSnapshot
from wuwa_assistant.ui.theme import apply_dark_theme


def main() -> None:
    # Windows' native font engine provides the same Traditional Chinese glyphs
    # as the supported desktop app; a generic offscreen plugin can show tofu.
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
    app = QApplication([])
    apply_dark_theme(app)
    panel = EchoPanel(protect=lambda: None)
    panel.apply_snapshot(
        EchoPanelSnapshot(
            name="示範聲骸",
            level=15,
            score=61.2,
            target_score=72.5,
            save_advice="暫留候選, 待下一階段再評估",
            upgrade_advice="至 +20 再確認",
            basis="合成資料 · 僅供介面示意",
            status="示範模式",
            detail="這些數值為虛構示意, 不是玩家資料或實際評分結果。",
        )
    )
    panel.start(QRect(0, 0, 1280, 720), "top_left")
    app.processEvents()
    target = Path(__file__).resolve().parents[1] / "docs/images/echo-panel-demo.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not panel.grab().save(str(target)):
        raise RuntimeError("Could not render synthetic widget demo")
    panel.end()
    panel.close()
    app.processEvents()


if __name__ == "__main__":
    main()
