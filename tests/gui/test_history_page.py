from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from wuwa_assistant.ui.pages.history import (
    EchoHistoryPage,
    HistoryEventRow,
    HistoryObservationRow,
    HistorySessionDetail,
    HistorySessionRow,
)
from wuwa_assistant.ui.theme import apply_dark_theme


def _rows() -> tuple[HistorySessionRow, ...]:
    return (
        HistorySessionRow(
            "echo-session-0001",
            "2026-08-01 10:00",
            "2026-08-01 10:05",
            "不熄獵手",
            15,
            "保留",
            78.5,
            "測試角色",
            "第 1 次辨識",
        ),
        HistorySessionRow(
            "echo-session-0002",
            "2026-08-01 10:06",
            None,
            "鳴泣戰士",
            5,
            "尚未決定",
            capture_run="第 2 次辨識",
        ),
    )


def test_history_score_keeps_gradient_when_selected_and_sorted(qapp: QApplication) -> None:
    page = EchoHistoryPage()
    apply_dark_theme(page)
    page.resize(1200, 800)
    page.show()
    page.set_sessions(_rows())
    try:
        page.session_table.sortByColumn(5, Qt.SortOrder.DescendingOrder)
        index = page.proxy_model.index(0, 5)
        page.session_table.selectRow(0)
        page.session_table.scrollTo(index)
        qapp.processEvents()
        expected = index.data(Qt.ItemDataRole.ForegroundRole).name()
        cell = page.session_table.viewport().grab(page.session_table.visualRect(index)).toImage()
        pixels = {
            cell.pixelColor(x, y).name() for y in range(cell.height()) for x in range(cell.width())
        }
        assert expected in pixels
    finally:
        page.close()


def test_history_page_requests_refresh_filters_and_selects_session(
    qapp: QApplication,
) -> None:
    page = EchoHistoryPage()
    refreshes: list[bool] = []
    selections: list[str] = []
    page.refresh_requested.connect(lambda: refreshes.append(True))
    page.session_selected.connect(selections.append)
    page.show()
    qapp.processEvents()

    QTest.mouseClick(page.refresh_button, Qt.MouseButton.LeftButton)
    assert refreshes == [True]
    assert not page.refresh_button.isEnabled()

    page.set_sessions(_rows())
    assert page.session_model.rowCount() == 2
    from wuwa_assistant.ui.score_colors import score_color

    for row, score in ((0, 78.5), (1, None)):
        assert page.session_model.data(
            page.session_model.index(row, 5), int(Qt.ItemDataRole.ForegroundRole)
        ) == score_color(score)
    assert page.proxy_model.rowCount() == 2
    assert page.status_label.text() == "2 筆掃描 · 2 組聲骸"
    assert page.session_model.headerData(0, Qt.Orientation.Horizontal) == "辨識批次"
    assert page.session_model.data(page.session_model.index(0, 0)) == "第 1 次辨識"
    assert page.refresh_button.isEnabled()

    page.search_edit.setText("測試角色")
    qapp.processEvents()
    assert page.proxy_model.rowCount() == 1
    page.session_table.setCurrentIndex(page.proxy_model.index(0, 0))
    qapp.processEvents()
    assert selections[-1] == "echo-session-0001"
    page.close()


def test_history_page_renders_detail_and_reports_service_errors(
    qapp: QApplication,
) -> None:
    page = EchoHistoryPage()
    page.show()
    qapp.processEvents()
    page.set_sessions(_rows())
    page.set_detail(
        HistorySessionDetail(
            session_id="echo-session-0001",
            echo_name="不熄獵手",
            set_name="羽落空塵之歌",
            final_level=15,
            final_disposition="保留",
            assigned_character="測試角色",
            observations=(
                HistoryObservationRow("10:00:00", 10, "暴擊 7.5%", 0.96),
                HistoryObservationRow("10:01:00", 15, "攻擊 10.1%", 1.0, corrected=True),
            ),
            events=(HistoryEventRow("10:01:00", "升級", "+10 → +15"),),
        )
    )

    assert page.detail_session_label.text() == "echo-session-0001"
    assert page.detail_name_label.text() == "不熄獵手"
    assert page.detail_level_label.text() == "+15"
    assert page.observation_model.rowCount() == 2
    assert page.observation_model.data(page.observation_model.index(1, 4)) == "是"
    assert page.event_model.rowCount() == 1

    page.show_error("資料庫暫時無法讀取")
    assert page.status_label.property("error") is True
    assert "資料庫暫時無法讀取" in page.status_label.text()
    assert page.refresh_button.isEnabled()
    page.close()


def test_history_selection_is_preserved_across_refresh(qapp: QApplication) -> None:
    page = EchoHistoryPage()
    page.show()
    qapp.processEvents()
    page.set_sessions(_rows())
    assert page.select_session("echo-session-0002")
    assert page.detail_session_label.text() == "—"
    page.set_sessions(tuple(reversed(_rows())))
    current = page.proxy_model.mapToSource(page.session_table.currentIndex())
    assert page.session_model.session_id_at(current.row()) == "echo-session-0002"
    assert not page.select_session("missing")
    page.close()
