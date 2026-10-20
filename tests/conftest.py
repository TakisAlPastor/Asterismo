"""Pytest fixtures for UI and core integration tests."""
from __future__ import annotations

import os
from PySide6.QtWidgets import QApplication
import pytest


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Session-level offscreen QApplication fixture for headless UI testing."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
