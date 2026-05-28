"""Unified Claude-inspired theme for the dock widget.

Provides:
- A warm beige/cream palette with Claude's signature orange accent.
- A complete QSS that covers every widget class actually used in the UI,
  so applying it on the dock widget makes the look consistent without
  losing native borders / hover / pressed feedback.
- ``apply_theme(widget)`` — call once after ``setupUi`` to install the
  font and stylesheet on the whole widget tree.

Constants are exposed so other modules (e.g. button factories in Python
code) can build matching styles instead of hard-coding hex values.
"""

from qgis.PyQt.QtGui import QFont, QFontDatabase


# ---- Font ------------------------------------------------------------------

UI_FONT_FAMILY = "Microsoft YaHei UI"
UI_FONT_FALLBACKS = ("Microsoft YaHei", "PingFang SC", "Segoe UI", "Arial")
UI_FONT_POINTSIZE = 10

MONO_FONT_FAMILY = "Consolas"


# ---- Palette ---------------------------------------------------------------

# Surfaces
BG_PRIMARY     = "#FAF9F5"   # main beige background
BG_SECONDARY   = "#F0EEE6"   # cards / muted panels
BG_INPUT       = "#FFFFFF"   # text inputs (clean white on beige)
BG_DISABLED    = "#EFEDE4"

# Text
TEXT_PRIMARY   = "#3D3929"   # warm dark brown
TEXT_SECONDARY = "#6B6759"
TEXT_MUTED     = "#9C9789"   # placeholders, captions
TEXT_ON_ACCENT = "#FFFFFF"   # text on orange / colored buttons

# Borders
BORDER          = "#E5E4DD"
BORDER_STRONG   = "#D4D1C4"
BORDER_FOCUS    = "#D97757"  # Claude orange on focus

# Claude orange (signature accent)
ACCENT          = "#D97757"
ACCENT_HOVER    = "#C26646"
ACCENT_PRESSED  = "#A8553A"
ACCENT_SUBTLE   = "#F5E6DD"   # very light tint for hover backgrounds

# Low-saturation semantic palette
SUCCESS         = "#7A9471"
SUCCESS_HOVER   = "#688160"
SUCCESS_PRESSED = "#566D4F"

WARNING         = "#C49464"
WARNING_HOVER   = "#A87E54"
WARNING_PRESSED = "#8C6845"

DANGER          = "#B26A60"
DANGER_HOVER    = "#9A5950"
DANGER_PRESSED  = "#7F4940"

NEUTRAL_BG       = "#EBE9DF"   # Interrupt / Clear / Load Data
NEUTRAL_HOVER    = "#DFDCCE"
NEUTRAL_PRESSED  = "#CFCCBC"

BORDER_RADIUS_PX = 6


# ---- Helpers ---------------------------------------------------------------

def _resolve_family(preferred, fallbacks):
    try:
        families = set(QFontDatabase.families())          # PyQt6 static
    except TypeError:
        families = set(QFontDatabase().families())        # PyQt5 instance
    for name in (preferred, *fallbacks):
        if name in families:
            return name
    return preferred


def ui_font_stack():
    return ", ".join([f'"{f}"' for f in (UI_FONT_FAMILY, *UI_FONT_FALLBACKS)])


# Reusable button stylesheet builder. ``palette`` is one of "primary",
# "success", "warning", "danger", "neutral". Returns a complete QSS string
# (background, border, hover, pressed, disabled) suitable for
# ``button.setStyleSheet(...)``.
_BTN_PALETTES = {
    "primary": (ACCENT, ACCENT_HOVER, ACCENT_PRESSED, TEXT_ON_ACCENT),
    "success": (SUCCESS, SUCCESS_HOVER, SUCCESS_PRESSED, TEXT_ON_ACCENT),
    "warning": (WARNING, WARNING_HOVER, WARNING_PRESSED, TEXT_ON_ACCENT),
    "danger":  (DANGER,  DANGER_HOVER,  DANGER_PRESSED,  TEXT_ON_ACCENT),
    "neutral": (NEUTRAL_BG, NEUTRAL_HOVER, NEUTRAL_PRESSED, TEXT_PRIMARY),
}

def button_qss(palette="primary"):
    bg, hover, pressed, fg = _BTN_PALETTES[palette]
    border_color = BORDER if palette == "neutral" else bg
    return f"""
    QPushButton {{
        background-color: {bg};
        color: {fg};
        border: 1px solid {border_color};
        border-radius: {BORDER_RADIUS_PX}px;
        padding: 6px 14px;
        font-weight: 600;
    }}
    QPushButton:hover    {{ background-color: {hover};   border-color: {hover}; }}
    QPushButton:pressed  {{ background-color: {pressed}; border-color: {pressed}; }}
    QPushButton:disabled {{
        background-color: {BG_DISABLED};
        color: {TEXT_MUTED};
        border-color: {BORDER};
    }}
    """


def _global_qss():
    """The big stylesheet that covers every widget class used in the dock."""
    r = BORDER_RADIUS_PX
    fam = ui_font_stack()
    return f"""
    /* ---- Base typography ----------------------------------------------- */
    QWidget {{
        font-family: {fam};
        color: {TEXT_PRIMARY};
        background-color: {BG_PRIMARY};
    }}
    QLabel, QCheckBox, QRadioButton {{
        background: transparent;
    }}

    /* ---- Tabs ---------------------------------------------------------- */
    QTabWidget::pane {{
        background-color: {BG_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: {r}px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: {BG_SECONDARY};
        color: {TEXT_SECONDARY};
        padding: 6px 16px;
        border: 1px solid {BORDER};
        border-bottom: none;
        border-top-left-radius: {r}px;
        border-top-right-radius: {r}px;
        margin-right: 2px;
        font-weight: 600;
    }}
    QTabBar::tab:selected {{
        background: {BG_PRIMARY};
        color: {TEXT_PRIMARY};
        border-bottom: 2px solid {ACCENT};
    }}
    QTabBar::tab:hover:!selected {{
        background: {ACCENT_SUBTLE};
        color: {TEXT_PRIMARY};
    }}

    /* ---- Text inputs --------------------------------------------------- */
    QPlainTextEdit, QTextEdit, QLineEdit, QTextBrowser {{
        background-color: {BG_INPUT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: {r}px;
        padding: 6px;
        selection-background-color: {ACCENT_SUBTLE};
        selection-color: {TEXT_PRIMARY};
    }}
    QPlainTextEdit:focus, QTextEdit:focus, QLineEdit:focus, QTextBrowser:focus {{
        border: 1px solid {BORDER_FOCUS};
    }}
    QPlainTextEdit:disabled, QTextEdit:disabled, QLineEdit:disabled,
    QPlainTextEdit:read-only, QTextEdit:read-only, QLineEdit:read-only {{
        background-color: {BG_DISABLED};
        color: {TEXT_MUTED};
    }}

    /* Primary user input — always shows accent orange border */
    QPlainTextEdit#task_LineEdit {{
        border: 2px solid {ACCENT};
    }}
    QPlainTextEdit#task_LineEdit:focus {{
        border: 2px solid {ACCENT_HOVER};
    }}

    /* ---- Default QPushButton (secondary / neutral) -------------------- */
    QPushButton {{
        background-color: {NEUTRAL_BG};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: {r}px;
        padding: 6px 14px;
    }}
    QPushButton:hover    {{ background-color: {NEUTRAL_HOVER};   border-color: {BORDER_STRONG}; }}
    QPushButton:pressed  {{ background-color: {NEUTRAL_PRESSED}; border-color: {BORDER_STRONG}; }}
    QPushButton:disabled {{
        background-color: {BG_DISABLED};
        color: {TEXT_MUTED};
        border-color: {BORDER};
    }}

    /* Primary action button (Send Request) — orange */
    QPushButton#run_button {{
        background-color: {ACCENT};
        color: {TEXT_ON_ACCENT};
        border: 1px solid {ACCENT};
        font-weight: 600;
    }}
    QPushButton#run_button:hover   {{ background-color: {ACCENT_HOVER};   border-color: {ACCENT_HOVER}; }}
    QPushButton#run_button:pressed {{ background-color: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}

    /* ---- QToolButton (toolbar buttons) -------------------------------- */
    QToolButton {{
        background-color: transparent;
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: {r}px;
        padding: 4px 8px;
    }}
    QToolButton:hover {{
        background-color: {ACCENT_SUBTLE};
        border-color: {ACCENT};
    }}
    QToolButton:pressed {{
        background-color: {ACCENT};
        color: {TEXT_ON_ACCENT};
        border-color: {ACCENT};
    }}

    /* ---- ComboBox ------------------------------------------------------ */
    QComboBox {{
        background-color: {BG_INPUT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: {r}px;
        padding: 4px 8px;
        min-height: 22px;
    }}
    QComboBox:hover {{ border-color: {BORDER_STRONG}; }}
    QComboBox:focus {{ border-color: {BORDER_FOCUS}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background-color: {BG_INPUT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        selection-background-color: {ACCENT_SUBTLE};
        selection-color: {TEXT_PRIMARY};
        outline: 0;
    }}

    /* ---- CheckBox / RadioButton --------------------------------------- */
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {BORDER_STRONG};
        background: {BG_INPUT};
    }}
    QCheckBox::indicator {{ border-radius: 3px; }}
    QRadioButton::indicator {{ border-radius: 7px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {ACCENT};
        border-color: {ACCENT};
    }}

    /* ---- GroupBox ------------------------------------------------------ */
    QGroupBox {{
        background-color: {BG_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: {r}px;
        margin-top: 10px;
        padding-top: 8px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {TEXT_SECONDARY};
    }}

    /* ---- ProgressBar --------------------------------------------------- */
    QProgressBar {{
        border: 1px solid {BORDER};
        border-radius: {r}px;
        background: {BG_SECONDARY};
        text-align: center;
        color: {TEXT_PRIMARY};
    }}
    QProgressBar::chunk {{
        background-color: {ACCENT};
        border-radius: {r}px;
    }}

    /* ---- ScrollBar ----------------------------------------------------- */
    QScrollBar:vertical {{
        background: {BG_SECONDARY}; width: 10px; margin: 0;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER_STRONG};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

    QScrollBar:horizontal {{
        background: {BG_SECONDARY}; height: 10px; margin: 0;
        border: none;
    }}
    QScrollBar::handle:horizontal {{
        background: {BORDER_STRONG};
        border-radius: 5px;
        min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {TEXT_MUTED}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    /* ---- Splitter / Frame --------------------------------------------- */
    QSplitter::handle {{ background: {BORDER}; }}
    QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {BORDER}; }}

    /* ---- ToolTip ------------------------------------------------------- */
    QToolTip {{
        background-color: {TEXT_PRIMARY};
        color: {BG_PRIMARY};
        border: 1px solid {TEXT_PRIMARY};
        padding: 4px 6px;
    }}
    """


def apply_theme(widget):
    """Install the unified font + Claude-style stylesheet on ``widget``.

    Safe to call once after ``setupUi`` of the top-level dock widget; child
    widgets inherit automatically. Existing per-widget inline stylesheets
    (those set via ``setStyleSheet`` directly on a child) still win and
    should be reviewed separately if they need to follow the theme.
    """
    family = _resolve_family(UI_FONT_FAMILY, UI_FONT_FALLBACKS)
    widget.setFont(QFont(family, UI_FONT_POINTSIZE))
    widget.setStyleSheet(_global_qss())
