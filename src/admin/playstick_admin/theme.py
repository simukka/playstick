"""The look: one dark theme, applied to the whole application at startup.

It is the Fusion style, a dark palette, and a style sheet over both, and each of
the three covers what the others cannot. The style sheet is where nearly all of
it is. Fusion is there because it is the one built-in style that honours a style
sheet everywhere and looks the same on every desktop -- the native styles each
ignore a different handful of rules. The palette is for whatever the sheet never
reaches: placeholder text, a message box's body, a `palette(mid)` border, the
colours a delegate paints with. Without it those come out in Fusion's light
default, a white patch in a dark window.

Buttons take a role by object name rather than a colour of their own, so a view
says what a button is for and this file says what that looks like:

  primary   the step the page exists for -- add, save, push
  accent    a secondary action worth finding -- rescan, run
  danger    stops or discards something
"""

from PySide6.QtGui import QColor, QPalette

# Named because things outside the sheet want them: the palette below, the
# delegates that paint the table's pills, and a view that colours one label.
BACKGROUND = "#0A0D18"
SIDEBAR = "#0C1020"
SURFACE = "#0E1424"
RAISED = "#141B2E"
TEXT = "#F2F4FA"
MUTED = "#8E9BB8"
DISABLED = "#566179"
BORDER = "#252F47"
HIGHLIGHT = "#1B2C55"
LINK = "#3B82F6"
DANGER = "#F472B6"
OK = "#22C55E"
BUSY = "#3B82F6"

# Pills in the tables: (background, text) by kind. The Prepare tab's Source and
# Status columns; see views.table_delegates.
PILLS = {
    "source": ("#16264A", "#8DB4FF"),
    "source-alt": ("#2E1B4E", "#C69BFF"),
    "ok": ("#0F2E24", "#4ADE80"),
    "busy": ("#132A52", "#60A5FA"),
    "warn": ("#3A2A10", "#FBBF24"),
    "bad": ("#3A1424", "#F87171"),
    "muted": ("#1A2134", "#8E9BB8"),
}

STYLE = """
QWidget { background:#0A0D18; color:#F2F4FA; font-family:"Inter","Noto Sans",sans-serif; font-size:12px; }
QMainWindow, QDialog { background:#0A0D18; }
QLabel, QCheckBox, QRadioButton { background:transparent; }

QFrame#sidebar { background:#0C1020; border-right:1px solid #1C2438; }
QFrame#sidebar QWidget { background:transparent; }
QFrame#statusCard { background:#111829; border:1px solid #232D45; border-radius:10px; }
QLabel#statusHead { font-weight:700; }
QLabel#statusDetail { color:#8E9BB8; font-size:11px; }
QFrame#rule { background:#1C2438; max-height:1px; min-height:1px; border:0; }
QLabel#brand { font-size:25px; font-weight:800; color:#F2F4FA; }
QLabel#heading { font-size:30px; font-weight:750; }
QLabel#subheading, QLabel#meta { color:#8E9BB8; }
QLabel#subheading { font-size:14px; }
QLabel#section { font-size:16px; font-weight:700; }

QPushButton, QToolButton { background:#121929; border:1px solid #2A3550; border-radius:7px; padding:7px 13px; }
QPushButton:hover, QToolButton:hover { background:#1A2440; border-color:#4B6FC4; }
QPushButton:pressed, QToolButton:pressed { background:#22305A; }
QPushButton:disabled, QToolButton:disabled { background:#0F1422; border-color:#1E2639; color:#566179; }
QPushButton#primary { background:qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2F6BFF, stop:1 #2456E8); border:1px solid #4D82FF; font-weight:650; padding:8px 18px; }
QPushButton#primary:hover { background:#3B78FF; }
QPushButton#accent { background:qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #C026D3, stop:0.6 #E2369F, stop:1 #F97316); border:1px solid #E24FB8; font-weight:650; padding:8px 18px; }
QPushButton#accent:hover { background:qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #D03BE0, stop:0.6 #EE4BAD, stop:1 #FB8530); }
QPushButton#danger { background:#1C1224; color:#F472B6; border:1px solid #DB2777; }
QPushButton#danger:hover { background:#2A1530; }
QPushButton#primary:disabled, QPushButton#accent:disabled, QPushButton#danger:disabled { background:#0F1422; border-color:#1E2639; color:#566179; }
QPushButton#nav { text-align:left; border:0; background:transparent; padding:11px 14px; border-radius:8px; color:#AEBBD8; font-size:13px; }
QPushButton#nav:hover { background:#141D36; }
QPushButton#nav:checked { background:qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1D3478, stop:1 #172452); border-left:3px solid #3B82F6; color:#FFFFFF; font-weight:600; }
QToolButton#flat { background:transparent; border:0; padding:4px; border-radius:5px; }
QToolButton#flat:hover { background:#1A2440; }
QToolButton#flat:checked { background:#1B2C55; }

QLineEdit, QComboBox, QSpinBox { background:#0F1526; border:1px solid #2A3550; border-radius:8px; padding:9px 12px; selection-background-color:#3B82F6; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color:#3B82F6; }
QLineEdit:disabled, QComboBox:disabled { color:#566179; border-color:#1E2639; }
QLineEdit#search { padding:10px 12px; font-size:13px; }
QComboBox QAbstractItemView { background:#0F1526; border:1px solid #2A3550; selection-background-color:#1B2C55; }

QCheckBox, QRadioButton { spacing:8px; color:#C9D2E6; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #4A5878; border-radius:4px; background:#0F1526; }
QCheckBox::indicator:hover { border-color:#6C8BD0; }
QCheckBox::indicator:checked { background:#3B82F6; border-color:#3B82F6; }

QTableView, QListView, QTreeView { background:#0E1424; alternate-background-color:#0E1424; border:1px solid #222B41; border-radius:8px; gridline-color:#1A2236; selection-background-color:#18264A; selection-color:#FFFFFF; outline:0; }
QTableView::item { padding:0 8px; border:0; border-bottom:1px solid #182033; }
QTableView::item:selected { background:#18264A; }
QListView::item { padding:4px 7px; border:0; }
QHeaderView { background:#111829; border:0; border-top-left-radius:8px; border-top-right-radius:8px; }
QHeaderView::section { background:#111829; color:#AEBBD8; padding:9px 8px; border:0; border-bottom:1px solid #222B41; border-right:1px solid #1A2236; font-weight:500; }
QHeaderView::section:last { border-right:0; }
QTableCornerButton::section { background:#111829; border:0; }

QGroupBox { border:1px solid #222B41; border-radius:8px; margin-top:16px; padding:12px 8px 8px 8px; font-size:14px; font-weight:700; }
QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px; }

QTabWidget::pane { border:1px solid #222B41; border-radius:8px; top:-1px; }
QTabBar::tab { background:#0F1526; color:#AEBBD8; border:1px solid #222B41; border-bottom:0; border-top-left-radius:7px; border-top-right-radius:7px; padding:7px 14px; margin-right:2px; }
QTabBar::tab:hover { background:#141D36; }
QTabBar::tab:selected { background:#1B2C55; color:#FFFFFF; font-weight:600; }

QProgressBar { background:#0F1526; border:1px solid #2A3550; border-radius:11px; text-align:center; min-height:22px; max-height:22px; font-weight:700; color:#FFFFFF; }
QProgressBar::chunk { border-radius:10px; background:qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1E90FF, stop:0.45 #8B3CF6, stop:0.75 #E83E8C, stop:1 #FF8A3D); }

QPlainTextEdit, QTextEdit { background:#0B1120; color:#B9CCF2; border:1px solid #222B41; border-radius:8px; padding:9px; font-family:"JetBrains Mono","DejaVu Sans Mono",monospace; font-size:11px; }
QFrame#console { background:#0B1120; border:1px solid #222B41; border-radius:8px; }
QFrame#console QWidget { background:transparent; }
QFrame#console QPlainTextEdit { border:0; padding:4px 2px; }

QMenuBar { background:#0C1020; border-bottom:1px solid #1C2438; }
QMenuBar::item { background:transparent; padding:6px 10px; }
QMenuBar::item:selected { background:#141D36; border-radius:4px; }
QMenu { background:#0F1526; border:1px solid #2A3550; padding:4px; }
QMenu::item { padding:6px 18px; border-radius:4px; }
QMenu::item:selected { background:#1B2C55; }
QMenu::item:disabled { color:#566179; }
QMenu::separator { height:1px; background:#1C2438; margin:4px 8px; }

QStatusBar { background:#0C1020; color:#8E9BB8; border-top:1px solid #1C2438; }
QToolTip { background:#141B2E; color:#F2F4FA; border:1px solid #2A3550; padding:4px; }
QSplitter::handle { background:transparent; }
QScrollArea { border:0; }

QScrollBar:vertical { background:transparent; width:10px; margin:2px; }
QScrollBar::handle:vertical { background:#2A3550; border-radius:3px; min-height:24px; }
QScrollBar:horizontal { background:transparent; height:10px; margin:2px; }
QScrollBar::handle:horizontal { background:#2A3550; border-radius:3px; min-width:24px; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:none; }
"""


def palette():
    p = QPalette()
    for role, colour in (
            (QPalette.Window, BACKGROUND),
            (QPalette.WindowText, TEXT),
            (QPalette.Base, SURFACE),
            (QPalette.AlternateBase, SURFACE),
            (QPalette.Text, TEXT),
            (QPalette.PlaceholderText, "#5F6B86"),
            (QPalette.Button, RAISED),
            (QPalette.ButtonText, TEXT),
            (QPalette.BrightText, DANGER),
            (QPalette.Highlight, HIGHLIGHT),
            (QPalette.HighlightedText, "#FFFFFF"),
            (QPalette.ToolTipBase, RAISED),
            (QPalette.ToolTipText, TEXT),
            (QPalette.Link, LINK),
            (QPalette.Light, "#22305A"),
            (QPalette.Midlight, "#1A2440"),
            (QPalette.Mid, BORDER),
            (QPalette.Dark, SIDEBAR),
            (QPalette.Shadow, "#000000")):
        p.setColor(role, QColor(colour))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(DISABLED))
    return p


def apply(app):
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setStyleSheet(STYLE)
