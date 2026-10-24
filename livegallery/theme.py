"""Application colors and read-only Windows Apps-theme following.

Qt does not overwrite palette roles explicitly set by an application; this
controller therefore reapplies the complete application palette on a change.
It never changes Windows settings or requires PowerToys to be installed.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPalette


_TOKENS = {
    'dark': {
        'background': '#11151a', 'surface': '#151a20', 'field': '#1b222b',
        'text': '#e9edf2', 'muted': '#83909f', 'border': '#35404d', 'hover': '#303b48',
        'accent': '#d8ae6b', 'accent_text': '#181b20', 'selected': '#3c3326',
        'selected_text': '#f1d19b', 'canvas': '#000000', 'thumbnail': '#1a1e23',
        'thumbnail_selected': '#23272d', 'thumbnail_border': '#30343b',
        'thumbnail_selected_border': '#e6ba72', 'thumbnail_text': '#aab1bb',
        'thumbnail_selected_text': '#f4eee5', 'switch_on': '#3b82f6',
        'switch_off': '#58616d', 'switch_thumb': '#ffffff', 'focus': '#9ac5ff',
        'tick': '#ffffff', 'divider': '#2a313b', 'grip': '#7d8998',
        'badge_background': '#31281c', 'badge_text': '#f1cf91', 'badge_border': '#775e38',
    },
    'light': {
        'background': '#f4f6f8', 'surface': '#ffffff', 'field': '#ffffff',
        'text': '#202833', 'muted': '#566474', 'border': '#c5ced8', 'hover': '#e8eef5',
        'accent': '#d8ae6b', 'accent_text': '#181b20', 'selected': '#f2e6d1',
        'selected_text': '#614116', 'canvas': '#e7e9ed', 'thumbnail': '#ffffff',
        'thumbnail_selected': '#edf4ff', 'thumbnail_border': '#c5ced8',
        'thumbnail_selected_border': '#a66c13', 'thumbnail_text': '#49576a',
        'thumbnail_selected_text': '#15263a', 'switch_on': '#3b82f6',
        'switch_off': '#8793a3', 'switch_thumb': '#ffffff', 'focus': '#1d4ed8',
        'tick': '#ffffff', 'divider': '#cbd3dc', 'grip': '#64748b',
        'badge_background': '#fff1cf', 'badge_text': '#6d4b10', 'badge_border': '#c7a76b',
    },
}


def theme_tokens(theme):
    """Return an independent set of drawing colors for a resolved theme."""
    if theme not in _TOKENS:
        raise ValueError('主题必须是 dark 或 light。')
    return dict(_TOKENS[theme])


def theme_palette(theme):
    colors = theme_tokens(theme)
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: 'background', QPalette.ColorRole.WindowText: 'text',
        QPalette.ColorRole.Base: 'field', QPalette.ColorRole.AlternateBase: 'surface',
        QPalette.ColorRole.Text: 'text', QPalette.ColorRole.Button: 'surface',
        QPalette.ColorRole.ButtonText: 'text', QPalette.ColorRole.ToolTipBase: 'surface',
        QPalette.ColorRole.ToolTipText: 'text', QPalette.ColorRole.Highlight: 'switch_on',
        QPalette.ColorRole.HighlightedText: 'tick', QPalette.ColorRole.PlaceholderText: 'muted',
        QPalette.ColorRole.Link: 'switch_on', QPalette.ColorRole.LinkVisited: 'focus',
        QPalette.ColorRole.Light: 'surface', QPalette.ColorRole.Midlight: 'hover',
        QPalette.ColorRole.Mid: 'border', QPalette.ColorRole.Dark: 'muted',
        QPalette.ColorRole.Shadow: 'border', QPalette.ColorRole.BrightText: 'tick',
        QPalette.ColorRole.Accent: 'switch_on',
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role, name in roles.items():
            palette.setColor(group, role, QColor(colors[name]))
    disabled = '#697585' if theme == 'dark' else '#738091'
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled))
    return palette


DARK_STYLE = """
QWidget { background: #11151a; color: #e9edf2; font-family: 'Segoe UI', 'PingFang SC', sans-serif; font-size: 13px; }
QLabel { background: transparent; }
QMainWindow, QDialog { background: #11151a; }
QFrame#header { background: #161b22; border-bottom: 1px solid #2a3039; }
QFrame#filterbar { background: #14191f; border-bottom: 1px solid #262d36; }
QFrame#sidebar { background: #151a20; border-right: 1px solid #29303a; }
QFrame#infobar { background: #151a20; border-bottom: 1px solid #242b34; }
QFrame#film { background: #14191f; border-top: 1px solid #2a313b; }
QLabel#brand { color: #f1d19b; font-size: 25px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #83909f; }
QLabel#emptyTitle { color: #e7edf5; font-size: 25px; font-weight: 600; }
QLabel#emptyIcon { color: #e6ba72; font-size: 66px; }
QLabel#emptyBody { color: #8e9baa; font-size: 14px; line-height: 150%; }
QLineEdit#filename, QLineEdit#filename:focus { font-size: 15px; font-weight: 600; background: transparent; border: none; border-radius: 0; padding: 0; }
QLabel#liveBadge { color: #f1cf91; border: 1px solid #775e38; border-radius: 5px; padding: 4px 7px; background: #31281c; }
QLabel#toast { background: #3a3020; color: #f5d69e; border: 1px solid #6b5533; padding: 9px 14px; }
QPushButton { border: 1px solid #39424e; border-radius: 7px; background: #232b35; color: #dce3ec; padding: 8px 13px; min-height: 20px; }
QPushButton:hover { background: #303b48; border-color: #5a697b; }
QPushButton:pressed, QPushButton:checked { background: #463a27; border-color: #bf965a; color: #f4d49f; }
QPushButton#primary { background: #d8ae6b; color: #181b20; border-color: #d8ae6b; font-weight: 600; }
QPushButton#primary:hover { background: #eac187; }
QPushButton:disabled { color: #56616e; border-color: #2b323b; background: #1b2128; }
QLineEdit, QComboBox, QSpinBox, QKeySequenceEdit { padding: 7px 9px; border: 1px solid #35404d; border-radius: 6px; background: #1b222b; min-height: 20px; selection-background-color: #83663e; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QKeySequenceEdit:focus { border-color: #c7a166; }
QComboBox QAbstractItemView { background: #222b36; border: 1px solid #485565; selection-background-color: #57472f; }
QListView, QTreeWidget { border: none; background: transparent; outline: none; }
QTreeWidget::item { min-height: 32px; padding: 2px 4px; border-radius: 5px; }
QTreeWidget::item:selected { background: #3c3326; color: #f1d19b; }
QTreeWidget::item:hover { background: #252d38; }
QScrollBar:horizontal { background: #151a20; height: 10px; border: none; }
QScrollBar:vertical { background: #151a20; width: 10px; border: none; }
QScrollBar::handle { background: #3c4857; border-radius: 4px; min-width: 35px; min-height: 35px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QGroupBox { border: 1px solid #34404c; border-radius: 8px; margin-top: 13px; padding: 20px 13px 12px; }
QGroupBox::title { color: #dfc59b; subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QCheckBox { spacing: 9px; padding: 4px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #586678; border-radius: 3px; background: #202833; }
QCheckBox::indicator:checked { background: #d8ae6b; border: 3px solid #d8ae6b; }
QRadioButton { spacing: 9px; padding: 4px; }
QRadioButton::indicator { width: 17px; height: 17px; border: 1px solid #586678; border-radius: 9px; background: #202833; }
QRadioButton::indicator:checked { background: #d8ae6b; border-color: #d8ae6b; }
QMenu { background: #202833; border: 1px solid #465260; padding: 6px; }
QMenu::item { padding: 8px 22px; border-radius: 4px; }
QMenu::item:selected { background: #4a3d29; }
QMenu::separator { height: 1px; background: #3a4553; margin: 5px; }
QStatusBar { background: #11161c; color: #7e8c9c; border-top: 1px solid #252c35; font-size: 11px; }
QSplitter::handle { background: #252d37; width: 1px; }
QToolTip { background: #28323e; color: #edf1f7; border: 1px solid #586678; padding: 6px; }
"""

# A complete light stylesheet, with explicit surfaces and readable control states.
LIGHT_STYLE = """
QWidget { background: #f4f6f8; color: #202833; font-family: 'Segoe UI', 'PingFang SC', sans-serif; font-size: 13px; }
QLabel { background: transparent; }
QMainWindow, QDialog { background: #f4f6f8; }
QFrame#header { background: #ffffff; border-bottom: 1px solid #cbd3dc; }
QFrame#filterbar { background: #eef2f6; border-bottom: 1px solid #cbd3dc; }
QFrame#sidebar { background: #f9fafb; border-right: 1px solid #cbd3dc; }
QFrame#infobar { background: #ffffff; border-bottom: 1px solid #cbd3dc; }
QFrame#film { background: #f0f3f7; border-top: 1px solid #cbd3dc; }
QLabel#brand { color: #805113; font-size: 25px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #566474; }
QLabel#emptyTitle { color: #202833; font-size: 25px; font-weight: 600; }
QLabel#emptyIcon { color: #97631b; font-size: 66px; }
QLabel#emptyBody { color: #566474; font-size: 14px; line-height: 150%; }
QLineEdit#filename, QLineEdit#filename:focus { font-size: 15px; font-weight: 600; background: transparent; border: none; border-radius: 0; padding: 0; }
QLabel#liveBadge { color: #6d4b10; border: 1px solid #c7a76b; border-radius: 5px; padding: 4px 7px; background: #fff1cf; }
QLabel#toast { background: #fff3da; color: #674716; border: 1px solid #c7a76b; padding: 9px 14px; }
QPushButton { border: 1px solid #bec9d5; border-radius: 7px; background: #ffffff; color: #202833; padding: 8px 13px; min-height: 20px; }
QPushButton:hover { background: #e8eef5; border-color: #8294a9; }
QPushButton:pressed, QPushButton:checked { background: #f2e6d1; border-color: #ad7c37; color: #614116; }
QPushButton#primary { background: #d8ae6b; color: #181b20; border-color: #b88c46; font-weight: 600; }
QPushButton#primary:hover { background: #eac187; }
QPushButton:disabled { color: #738091; border-color: #d7dee6; background: #e9edf2; }
QLineEdit, QComboBox, QSpinBox, QKeySequenceEdit { padding: 7px 9px; border: 1px solid #c5ced8; border-radius: 6px; background: #ffffff; min-height: 20px; selection-background-color: #2563eb; selection-color: #ffffff; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QKeySequenceEdit:focus { border-color: #2563eb; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QKeySequenceEdit:disabled { color: #738091; background: #e9edf2; }
QComboBox QAbstractItemView { background: #ffffff; color: #202833; border: 1px solid #aab8c8; selection-background-color: #dbeafe; selection-color: #17325a; }
QListView, QTreeWidget { border: none; background: transparent; outline: none; }
QTreeWidget::item { min-height: 32px; padding: 2px 4px; border-radius: 5px; }
QTreeWidget::item:selected { background: #f2e6d1; color: #614116; }
QTreeWidget::item:hover { background: #e8eef5; }
QScrollBar:horizontal { background: #e7ecf2; height: 10px; border: none; }
QScrollBar:vertical { background: #e7ecf2; width: 10px; border: none; }
QScrollBar::handle { background: #99a8ba; border-radius: 4px; min-width: 35px; min-height: 35px; }
QScrollBar::handle:hover { background: #6e829a; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QGroupBox { border: 1px solid #c5ced8; border-radius: 8px; margin-top: 13px; padding: 20px 13px 12px; }
QGroupBox::title { color: #654718; subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QCheckBox { spacing: 9px; padding: 4px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #8292a4; border-radius: 3px; background: #ffffff; }
QCheckBox::indicator:checked { background: #3b82f6; border: 1px solid #3b82f6; }
QRadioButton { spacing: 9px; padding: 4px; }
QRadioButton::indicator { width: 17px; height: 17px; border: 1px solid #8292a4; border-radius: 9px; background: #ffffff; }
QRadioButton::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QMenu { background: #ffffff; color: #202833; border: 1px solid #aab8c8; padding: 6px; }
QMenu::item { padding: 8px 22px; border-radius: 4px; }
QMenu::item:selected { background: #e4edf8; color: #17325a; }
QMenu::item:disabled { color: #738091; }
QMenu::separator { height: 1px; background: #cbd3dc; margin: 5px; }
QStatusBar { background: #eef2f6; color: #566474; border-top: 1px solid #cbd3dc; font-size: 11px; }
QSplitter::handle { background: #cbd3dc; width: 1px; }
QToolTip { background: #ffffff; color: #202833; border: 1px solid #aab8c8; padding: 6px; }
"""


def stylesheet_for(theme):
    if theme not in _TOKENS:
        raise ValueError('主题必须是 dark 或 light。')
    return DARK_STYLE if theme == 'dark' else LIGHT_STYLE


WINDOWS_PERSONALIZE_KEY = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
WM_SETTINGCHANGE = 0x001A
WM_THEMECHANGED = 0x031A


def windows_apps_theme(*, registry=None, key_path=WINDOWS_PERSONALIZE_KEY, root=None):
    """Read one AppsUseLightTheme DWORD; optional key is for isolated tests.

    SystemUsesLightTheme controls Windows shell surfaces and is deliberately
    ignored. No registry value is ever created, changed, or deleted here.
    """
    if registry is None:
        try:
            import winreg as registry
        except ImportError:
            return None
    if root is None:
        root = registry.HKEY_CURRENT_USER
    try:
        with registry.OpenKey(root, key_path, 0, registry.KEY_READ) as key:
            value, kind = registry.QueryValueEx(key, 'AppsUseLightTheme')
        if kind == registry.REG_DWORD and type(value) is int and value in (0, 1):
            return 'light' if value else 'dark'
    except (OSError, ValueError, TypeError):
        pass
    return None


def _qt_system_theme(application):
    scheme = application.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return 'dark'
    if scheme == Qt.ColorScheme.Light:
        return 'light'
    return None


class _WindowsThemeFilter(QAbstractNativeEventFilter):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            from ctypes import wintypes
            address = int(message)
            if address:
                message_id = ctypes.cast(address, ctypes.POINTER(wintypes.MSG)).contents.message
                if message_id in (WM_SETTINGCHANGE, WM_THEMECHANGED):
                    self.controller._queue_system_refresh()
        return False, 0


class ThemeController(QObject):
    """Resolve appearance and keep explicit application palettes in sync.

    ``system_provider`` is an optional read-only test source returning dark,
    light or None. Windows uses AppsUseLightTheme before Qt's platform hint.
    The timer only runs in system mode and performs no media work.
    """

    schemeChanged = Signal(str)

    def __init__(self, application, mode='dark', *, system_provider=None, poll_interval_ms=1000):
        super().__init__(application)
        self.application = application
        self.system_provider = system_provider
        self._mode = None
        self._effective_theme = None
        self._closed = False
        self._refresh_pending = False
        self._native_filter = None
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(poll_interval_ms)))
        self._poll_enabled = poll_interval_ms > 0
        self._timer.timeout.connect(self.refresh_system)
        self._hints = application.styleHints()
        self._hints.colorSchemeChanged.connect(self._system_hint_changed)
        if sys.platform == 'win32':
            self._native_filter = _WindowsThemeFilter(self)
            application.installNativeEventFilter(self._native_filter)
        self.set_mode(mode)

    @property
    def mode(self):
        return self._mode

    @property
    def effective_theme(self):
        return self._effective_theme

    def _read_system_theme(self):
        if self.system_provider is not None:
            try:
                scheme = self.system_provider()
            except (OSError, ValueError, TypeError):
                scheme = None
        else:
            scheme = windows_apps_theme() if sys.platform == 'win32' else None
        if scheme not in ('dark', 'light'):
            scheme = _qt_system_theme(self.application)
        return scheme if scheme in ('dark', 'light') else (self._effective_theme or 'dark')

    def set_mode(self, mode):
        if self._closed:
            return
        if mode not in ('dark', 'light', 'system'):
            raise ValueError('外观模式必须是 dark、light 或 system。')
        self._mode = mode
        if mode == 'system' and self._poll_enabled:
            self._timer.start()
        else:
            self._timer.stop()
        self._apply(self._read_system_theme() if mode == 'system' else mode)

    def _apply(self, scheme):
        if self._closed or scheme == self._effective_theme:
            return
        self._effective_theme = scheme
        self.application.setPalette(theme_palette(scheme))
        self.schemeChanged.emit(scheme)

    def refresh_system(self):
        if not self._closed and self.mode == 'system':
            self._apply(self._read_system_theme())

    def _system_hint_changed(self, *_args):
        self._queue_system_refresh()

    def _queue_system_refresh(self):
        if self._closed or self.mode != 'system' or self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self._run_queued_refresh)

    def _run_queued_refresh(self):
        self._refresh_pending = False
        self.refresh_system()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._hints.colorSchemeChanged.disconnect(self._system_hint_changed)
        if self._native_filter is not None:
            self.application.removeNativeEventFilter(self._native_filter)
            self._native_filter = None

    dispose = close
