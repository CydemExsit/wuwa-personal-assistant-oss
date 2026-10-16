"""Reconcile immutable card snapshots without rebuilding unchanged Qt widgets."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence

from PySide6.QtWidgets import QGridLayout, QWidget


class KeyedGrid[T]:
    """Own widgets for one current snapshot; hidden views are created on demand."""

    def __init__(
        self, layout: QGridLayout, key: Callable[[T], Hashable], create: Callable[[T], QWidget]
    ) -> None:
        self._layout = layout
        self._key = key
        self._create = create
        self._entries: dict[Hashable, tuple[T, object, QWidget]] = {}
        self._positions: tuple[tuple[Hashable, ...], int] | None = None

    def retain(self, keys: Iterable[Hashable]) -> None:
        valid = set(keys)
        for key in tuple(self._entries):
            if key not in valid:
                _, _, widget = self._entries.pop(key)
                self._layout.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
                self._positions = None

    def hide(self) -> None:
        for _, _, widget in self._entries.values():
            widget.hide()

    def render(self, rows: Sequence[T], columns: int, *, context: object = None) -> None:
        keys = tuple(self._key(row) for row in rows)
        self.retain(keys)
        changed = False
        for key, row in zip(keys, rows, strict=True):
            previous = self._entries.get(key)
            if previous is not None and previous[:2] == (row, context):
                continue
            if previous is not None:
                self._layout.removeWidget(previous[2])
                previous[2].hide()
                previous[2].deleteLater()
            self._entries[key] = (row, context, self._create(row))
            changed = True
        if changed or self._positions != (keys, columns):
            while self._layout.count():
                self._layout.takeAt(0)
            for index, key in enumerate(keys):
                self._layout.addWidget(self._entries[key][2], index // columns, index % columns)
            for column in range(5):
                self._layout.setColumnStretch(column, 1 if column < columns else 0)
                self._layout.setColumnMinimumWidth(column, 0)
            self._positions = (keys, columns)
        for _, _, widget in self._entries.values():
            widget.show()
