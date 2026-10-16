from __future__ import annotations

import gc
import os
from collections.abc import Iterator
from typing import cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    existing = QApplication.instance()
    application = cast(QApplication, existing) if existing is not None else QApplication([])
    application.setQuitOnLastWindowClosed(False)
    yield application
    application.processEvents()


@pytest.fixture(autouse=True)
def clean_qt_objects_after_test(qapp: QApplication) -> Iterator[None]:
    """Destroy test widgets and collect their cycles on the GUI thread.

    Closing a widget only hides it unless WA_DeleteOnClose was set. Retained
    wrapper cycles can otherwise be collected by a background DB worker,
    destroying native widgets while the GUI thread is processing their events.
    """

    automatic_gc = gc.isenabled()
    gc.disable()
    existing_widgets = set(qapp.topLevelWidgets())
    try:
        yield
    finally:
        try:
            for widget in qapp.topLevelWidgets():
                if widget not in existing_widgets and isValid(widget):
                    widget.close()
                    if isValid(widget):
                        widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()
            gc.collect()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()
        finally:
            if automatic_gc:
                gc.enable()
