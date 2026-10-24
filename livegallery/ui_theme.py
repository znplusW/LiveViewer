"""Apply the shared theme to live Qt windows without changing media state."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, Qt, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .theme import ThemeController, stylesheet_for, theme_palette, theme_tokens


def application_theme(mode=None):
    app = QApplication.instance()
    controller = getattr(app, '_liveviewer_theme_controller', None)
    if controller is None or getattr(controller, '_closed', False):
        controller = ThemeController(app, mode=mode or 'dark')
        app._liveviewer_theme_controller = controller
        app.aboutToQuit.connect(controller.close)
    elif mode is not None:
        controller.set_mode(mode)
    return controller


def tokens_for(widget):
    scheme = widget.window().property('liveviewerTheme') if widget is not None else None
    if scheme not in ('dark', 'light'):
        light = widget is not None and widget.palette().color(QPalette.ColorRole.Window).lightness() > 127
        scheme = 'light' if light else 'dark'
    return theme_tokens(scheme)


def _update_titlebar(widget, scheme):
    if (sys.platform != 'win32' or not widget.isWindow()
            or not widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created)):
        return
    try:
        api = ctypes.WinDLL('dwmapi.dll', use_last_error=True, winmode=0x800)
        setter = api.DwmSetWindowAttribute
        setter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        setter.restype = ctypes.c_long
        hwnd = ctypes.c_void_p(int(widget.winId()))
        dark = ctypes.c_int(scheme == 'dark')
        setter(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        colors = theme_tokens(scheme)
        # Windows 11 supports per-window caption/text colors. These window
        # attributes do not change Windows or PowerToys preferences.
        for attribute, token in ((35, 'background'), (36, 'text')):
            color = QColor(colors[token])
            value = ctypes.c_uint32(color.red() | color.green() << 8 | color.blue() << 16)
            setter(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError, ValueError):
        pass


class _ThemeBinding(QObject):
    def __init__(self, widget, callback=None):
        super().__init__(widget)
        self.widget = widget
        self.callback = callback
        self.controller = application_theme()
        self.controller.schemeChanged.connect(self.apply)
        widget.installEventFilter(self)
        self.apply(self.controller.effective_theme)

    @Slot(str)
    def apply(self, scheme):
        self.widget.setProperty('liveviewerTheme', scheme)
        self.widget.setPalette(theme_palette(scheme))
        self.widget.setStyleSheet(stylesheet_for(scheme))
        if self.callback is not None:
            self.callback(scheme)
        _update_titlebar(self.widget, scheme)
        self.widget.update()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange):
            _update_titlebar(self.widget, self.controller.effective_theme)
        return False


def bind_theme(widget, callback=None):
    """A QObject-owned connection is released when the window is deleted."""
    binding = _ThemeBinding(widget, callback)
    widget._theme_binding = binding
    return binding
