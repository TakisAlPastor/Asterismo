"""Thread-safe real-time log console widget."""

from __future__ import annotations

from datetime import datetime
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import (
    CARD_BORDER_RADIUS,
    FONT_SIZE_SMALL,
    LAYOUT_MARGIN_SMALL,
    LAYOUT_SPACING_SMALL,
    LOG_CONSOLE_MAX_BLOCKS,
)


class LogConsole(QWidget):
    """Reusable dockable/collapsible console widget for streaming application logs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._is_collapsed: bool = False
        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(
            LAYOUT_MARGIN_SMALL,
            LAYOUT_MARGIN_SMALL,
            LAYOUT_MARGIN_SMALL,
            LAYOUT_MARGIN_SMALL,
        )
        main_layout.setSpacing(LAYOUT_SPACING_SMALL)

        # Header bar
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)

        self._title_label = QLabel("Activity & Diagnostics Log", self)
        self._title_label.setObjectName("log_title")
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        self._clear_btn = QPushButton("Clear", self)
        self._clear_btn.clicked.connect(self.clear_logs)
        header_layout.addWidget(self._clear_btn)

        self._copy_btn = QPushButton("Copy All", self)
        self._copy_btn.clicked.connect(self.copy_logs)
        header_layout.addWidget(self._copy_btn)

        self._toggle_btn = QPushButton("Collapse", self)
        self._toggle_btn.clicked.connect(self.toggle_collapse)
        header_layout.addWidget(self._toggle_btn)

        main_layout.addLayout(header_layout)

        # Log Text Box
        self._text_edit = QPlainTextEdit(self)
        self._text_edit.setObjectName("log_console")
        self._text_edit.setReadOnly(True)
        self._text_edit.setMaximumBlockCount(LOG_CONSOLE_MAX_BLOCKS)
        main_layout.addWidget(self._text_edit)

    @Slot(str)
    def append_log(self, message: str) -> None:
        """Thread-safe slot to append a log line with timestamp and auto-scroll."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}"
        self._text_edit.appendPlainText(formatted)

        # Auto-scroll to end
        scrollbar = self._text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @Slot()
    def clear_logs(self) -> None:
        """Clear all messages from the log console."""
        self._text_edit.clear()

    @Slot()
    def copy_logs(self) -> None:
        """Copy entire log history to clipboard."""
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(self._text_edit.toPlainText())

    @Slot()
    def toggle_collapse(self) -> None:
        """Toggle visibility of the text edit area."""
        self._is_collapsed = not self._is_collapsed
        self._text_edit.setVisible(not self._is_collapsed)
        self._toggle_btn.setText(
            "Expand" if self._is_collapsed else "Collapse")

    def get_text(self) -> str:
        """Helper to inspect console contents (primarily for testing)."""
        return self._text_edit.toPlainText()
