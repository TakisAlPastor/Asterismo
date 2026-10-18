"""UI theme tokens, palette definitions, and modern QSS stylesheet generator."""

from __future__ import annotations

from dataclasses import dataclass
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.core.config.models import ThemeMode


WINDOW_MIN_WIDTH: int = 1120
WINDOW_MIN_HEIGHT: int = 740
SIDEBAR_FIXED_WIDTH: int = 220
LOG_CONSOLE_DEFAULT_HEIGHT: int = 180
LOG_CONSOLE_MIN_HEIGHT: int = 80
LOG_CONSOLE_MAX_BLOCKS: int = 2000

LAYOUT_MARGIN_SMALL: int = 8
LAYOUT_MARGIN_STD: int = 16
LAYOUT_SPACING_SMALL: int = 6
LAYOUT_SPACING_STD: int = 12
CARD_BORDER_RADIUS: int = 8
BUTTON_BORDER_RADIUS: int = 6
INPUT_BORDER_RADIUS: int = 4

FONT_SIZE_SMALL: int = 11
FONT_SIZE_BODY: int = 13
FONT_SIZE_SUBHEADER: int = 15
FONT_SIZE_HEADER: int = 18
FONT_SIZE_TITLE: int = 22


@dataclass(frozen=True, slots=True)
class ColorPalette:
    """Semantic color tokens for UI surfaces."""

    bg_main: str
    bg_surface: str
    bg_surface_alt: str
    bg_sidebar: str
    border: str
    border_focus: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent: str
    accent_hover: str
    status_ok: str
    status_missing: str
    status_broken: str


DARK_PALETTE = ColorPalette(
    bg_main="#0f172a",
    bg_surface="#1e293b",
    bg_surface_alt="#334155",
    bg_sidebar="#0b1120",
    border="#334155",
    border_focus="#38bdf8",
    text_primary="#f8fafc",
    text_secondary="#cbd5e1",
    text_muted="#64748b",
    accent="#0284c7",
    accent_hover="#0369a1",
    status_ok="#22c55e",
    status_missing="#f59e0b",
    status_broken="#ef4444",
)

LIGHT_PALETTE = ColorPalette(
    bg_main="#f8fafc",
    bg_surface="#ffffff",
    bg_surface_alt="#f1f5f9",
    bg_sidebar="#e2e8f0",
    border="#cbd5e1",
    border_focus="#0284c7",
    text_primary="#0f172a",
    text_secondary="#334155",
    text_muted="#94a3b8",
    accent="#0284c7",
    accent_hover="#0369a1",
    status_ok="#16a34a",
    status_missing="#d97706",
    status_broken="#dc2626",
)


def get_palette(theme: ThemeMode) -> ColorPalette:
    """Retrieve color palette based on selected theme mode."""
    if theme == ThemeMode.LIGHT:
        return LIGHT_PALETTE
    if theme == ThemeMode.DARK:
        return DARK_PALETTE

    # Resolve OS theme dynamically for ThemeMode.SYSTEM
    app = QApplication.instance()
    if app:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return LIGHT_PALETTE

    return DARK_PALETTE


def build_stylesheet(theme: ThemeMode) -> str:
    """Generate cohesive QSS stylesheet matching the active palette."""
    pal = get_palette(theme)
    return f"""
    QMainWindow, QWidget {{
        background-color: {pal.bg_main};
        color: {pal.text_primary};
        font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
        font-size: {FONT_SIZE_BODY}px;
    }}

    QListWidget#sidebar {{
        background-color: {pal.bg_sidebar};
        border: none;
        border-right: 1px solid {pal.border};
        padding: {LAYOUT_MARGIN_SMALL}px 0px;
        outline: none;
    }}

    QListWidget#sidebar::item {{
        padding: 12px {LAYOUT_MARGIN_STD}px;
        color: {pal.text_secondary};
        font-weight: 500;
        border-left: 3px solid transparent;
        margin: 2px {LAYOUT_MARGIN_SMALL}px;
        border-radius: {BUTTON_BORDER_RADIUS}px;
    }}

    QListWidget#sidebar::item:hover {{
        background-color: {pal.bg_surface};
        color: {pal.text_primary};
    }}

    QListWidget#sidebar::item:selected {{
        background-color: {pal.bg_surface};
        color: {pal.accent};
        border-left: 3px solid {pal.accent};
        font-weight: 600;
    }}

    QFrame.card {{
        background-color: {pal.bg_surface};
        border: 1px solid {pal.border};
        border-radius: {CARD_BORDER_RADIUS}px;
        padding: {LAYOUT_MARGIN_STD}px;
    }}

    QLabel.title {{
        font-size: {FONT_SIZE_TITLE}px;
        font-weight: bold;
        color: {pal.text_primary};
    }}

    QLabel.header {{
        font-size: {FONT_SIZE_HEADER}px;
        font-weight: 600;
        color: {pal.text_primary};
    }}

    QLabel.subheader {{
        font-size: {FONT_SIZE_SUBHEADER}px;
        font-weight: 600;
        color: {pal.text_secondary};
    }}

    QLabel.muted {{
        font-size: {FONT_SIZE_SMALL}px;
        color: {pal.text_muted};
    }}

    QLineEdit, QSpinBox, QComboBox {{
        background-color: {pal.bg_surface};
        border: 1px solid {pal.border};
        border-radius: {INPUT_BORDER_RADIUS}px;
        padding: 6px 10px;
        color: {pal.text_primary};
        selection-background-color: {pal.accent};
    }}

    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border: 1px solid {pal.border_focus};
    }}

    QPushButton {{
        background-color: {pal.bg_surface_alt};
        border: 1px solid {pal.border};
        border-radius: {BUTTON_BORDER_RADIUS}px;
        padding: 8px 16px;
        font-weight: 600;
        color: {pal.text_primary};
    }}

    QPushButton:hover {{
        background-color: {pal.border};
    }}

    QPushButton:pressed {{
        background-color: {pal.bg_surface};
    }}

    QPushButton.primary {{
        background-color: {pal.accent};
        border: 1px solid {pal.accent};
        color: #ffffff;
    }}

    QPushButton.primary:hover {{
        background-color: {pal.accent_hover};
    }}

    QPushButton:disabled {{
        opacity: 0.5;
        color: {pal.text_muted};
        background-color: {pal.bg_surface_alt};
        border-color: {pal.border};
    }}

    QCheckBox {{
        spacing: 8px;
        color: {pal.text_primary};
    }}

    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border-radius: 3px;
        border: 1px solid {pal.border};
        background-color: {pal.bg_surface};
    }}

    QCheckBox::indicator:checked {{
        background-color: {pal.accent};
        border-color: {pal.accent};
    }}

    QPlainTextEdit#log_console {{
        background-color: {pal.bg_sidebar};
        border: 1px solid {pal.border};
        border-radius: {INPUT_BORDER_RADIUS}px;
        font-family: "Consolas", "Courier New", monospace;
        font-size: {FONT_SIZE_SMALL}px;
        color: {pal.text_secondary};
        padding: 6px;
    }}

    QSplitter::handle {{
        background-color: {pal.border};
        height: 2px;
    }}

    QStatusBar {{
        background-color: {pal.bg_sidebar};
        border-top: 1px solid {pal.border};
        color: {pal.text_muted};
        font-size: {FONT_SIZE_SMALL}px;
    }}

    QScrollBar:vertical {{
        background: {pal.bg_main};
        width: 10px;
        margin: 0px;
    }}

    QScrollBar::handle:vertical {{
        background: {pal.border};
        min-height: 20px;
        border-radius: 5px;
    }}

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    """
