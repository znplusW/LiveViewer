"""Appearance palettes and safe, read-only system-following behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from livegallery.theme import (DARK_STYLE, LIGHT_STYLE, ThemeController,
                               stylesheet_for, theme_palette, theme_tokens,
                               windows_apps_theme)


class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.original_palette = QPalette(self.application.palette())
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        self.application.setPalette(self.original_palette)
        self.application.processEvents()

    def controller(self, *args, **kwargs):
        controller = ThemeController(self.application, *args, **kwargs)
        self.controllers.append(controller)
        return controller

    def test_default_dark_and_manual_light_do_not_read_system_source(self):
        provider = MagicMock(return_value='light')
        controller = self.controller(system_provider=provider)
        self.assertEqual(controller.mode, 'dark')
        self.assertEqual(controller.effective_theme, 'dark')
        self.assertFalse(controller._timer.isActive())
        provider.assert_not_called()
        controller.set_mode('light')
        self.assertEqual(controller.effective_theme, 'light')
        self.assertEqual(self.application.palette().color(QPalette.ColorRole.Window).name(), '#f4f6f8')
        self.assertEqual(self.application.palette().color(QPalette.ColorRole.Text).name(), '#202833')
        provider.assert_not_called()

    def test_system_refresh_repaints_explicit_palette_and_emits_only_for_changes(self):
        state = ['dark']
        controller = self.controller('system', system_provider=lambda: state[0], poll_interval_ms=0)
        changed = QSignalSpy(controller.schemeChanged)
        state[0] = 'light'
        controller.refresh_system()
        self.assertEqual(controller.effective_theme, 'light')
        self.assertEqual(changed.count(), 1)
        self.assertEqual(self.application.palette().color(QPalette.ColorRole.Base).name(), '#ffffff')
        for _ in range(4):
            controller.refresh_system()
        self.assertEqual(changed.count(), 1)
        state[0] = 'dark'
        controller.refresh_system()
        self.assertEqual(changed.count(), 2)
        self.assertEqual(self.application.palette().color(QPalette.ColorRole.Base).name(), '#1b222b')

    def test_manual_override_ignores_system_notifications_and_system_mode_resumes(self):
        state = ['light']
        provider = MagicMock(side_effect=lambda: state[0])
        controller = self.controller('system', system_provider=provider, poll_interval_ms=0)
        controller.set_mode('dark')
        calls = provider.call_count
        controller.refresh_system()
        controller._queue_system_refresh()
        self.application.processEvents()
        self.assertEqual(controller.effective_theme, 'dark')
        self.assertEqual(provider.call_count, calls)
        controller.set_mode('system')
        self.assertEqual(controller.effective_theme, 'light')

    def test_qt_notification_queues_readonly_source_and_source_has_priority(self):
        state = ['dark']
        controller = self.controller('system', system_provider=lambda: state[0], poll_interval_ms=0)
        changed = QSignalSpy(controller.schemeChanged)
        self.application.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
        self.application.processEvents()
        self.assertEqual(controller.effective_theme, 'dark')
        self.assertEqual(changed.count(), 0)
        state[0] = 'light'
        self.application.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
        self.application.processEvents()
        self.assertEqual(controller.effective_theme, 'light')
        self.assertEqual(changed.count(), 1)

    def test_polling_detects_missing_native_notification_and_close_stops_watchers(self):
        state = ['dark']
        provider = MagicMock(side_effect=lambda: state[0])
        controller = self.controller('system', system_provider=provider, poll_interval_ms=10)
        changed = QSignalSpy(controller.schemeChanged)
        state[0] = 'light'
        QTest.qWait(50)
        self.assertEqual(controller.effective_theme, 'light')
        self.assertEqual(changed.count(), 1)
        controller.close()
        controller.close()
        self.assertFalse(controller._timer.isActive())
        calls = provider.call_count
        state[0] = 'dark'
        self.application.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
        QTest.qWait(30)
        controller.refresh_system()
        self.assertEqual(provider.call_count, calls)
        self.assertEqual(changed.count(), 1)

    def test_invalid_or_missing_source_uses_qt_hint_without_changing_system_hint(self):
        with patch('livegallery.theme._qt_system_theme', return_value='light'):
            controller = self.controller('system', system_provider=lambda: None, poll_interval_ms=0)
            self.assertEqual(controller.effective_theme, 'light')
        with patch('livegallery.theme._qt_system_theme', return_value=None):
            controller.refresh_system()
            self.assertEqual(controller.effective_theme, 'light')
        with self.assertRaises(ValueError):
            controller.set_mode('invalid')

    def test_tokens_are_complete_independent_and_palette_has_readable_roles(self):
        dark, light = theme_tokens('dark'), theme_tokens('light')
        self.assertEqual(set(dark), set(light))
        self.assertEqual(dark['canvas'], '#000000')
        self.assertEqual(light['canvas'], '#e7e9ed')
        for theme in ('dark', 'light'):
            colors = theme_tokens(theme)
            self.assertEqual(colors['switch_on'], '#3b82f6')
            self.assertEqual(colors['tick'], '#ffffff')
            palette = theme_palette(theme)
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                self.assertEqual(palette.color(group, QPalette.ColorRole.WindowText).name(), colors['text'])
                self.assertEqual(palette.color(group, QPalette.ColorRole.Window).name(), colors['background'])
                self.assertEqual(palette.color(group, QPalette.ColorRole.HighlightedText).name(), '#ffffff')
            self.assertGreater(_contrast(colors['text'], colors['background']), 10)
            self.assertGreater(_contrast(colors['muted'], colors['background']), 4.5)
            self.assertGreater(_contrast(colors['selected_text'], colors['selected']), 4.5)
        dark['text'] = '#000000'
        self.assertNotEqual(theme_tokens('dark')['text'], dark['text'])
        with self.assertRaises(ValueError):
            theme_tokens('system')

    def test_both_styles_include_controls_menus_popups_and_filename_selection(self):
        self.assertEqual(stylesheet_for('dark'), DARK_STYLE)
        self.assertEqual(stylesheet_for('light'), LIGHT_STYLE)
        for style in (DARK_STYLE, LIGHT_STYLE):
            for selector in ('QFrame#film', 'QLineEdit#filename', 'QComboBox QAbstractItemView',
                             'QMenu::separator', 'QPushButton:disabled', 'QGroupBox::title',
                             'QScrollBar::handle', 'QToolTip'):
                self.assertIn(selector, style)
        self.assertIn('selection-color: #ffffff', LIGHT_STYLE)
        self.assertIn('QMenu::item:disabled', LIGHT_STYLE)

    def test_windows_reader_reads_apps_dword_only_and_never_writes(self):
        registry = MagicMock()
        registry.HKEY_CURRENT_USER, registry.KEY_READ, registry.REG_DWORD = 1, 2, 4
        registry.QueryValueEx.return_value = (1, registry.REG_DWORD)
        self.assertEqual(windows_apps_theme(registry=registry), 'light')
        registry.QueryValueEx.assert_called_once_with(registry.OpenKey.return_value.__enter__.return_value,
                                                      'AppsUseLightTheme')
        registry.QueryValueEx.return_value = (0, registry.REG_DWORD)
        self.assertEqual(windows_apps_theme(registry=registry), 'dark')
        for value in (('0', 1), (None, registry.REG_DWORD), (2, registry.REG_DWORD)):
            registry.QueryValueEx.return_value = value
            self.assertIsNone(windows_apps_theme(registry=registry))
        registry.OpenKey.side_effect = OSError('not available')
        self.assertIsNone(windows_apps_theme(registry=registry))
        registry.SetValueEx.assert_not_called()
        registry.CreateKey.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows-only isolated registry/native-message probe')
    def test_windows_temporary_key_and_native_settingchange_broadcast(self):
        environment = dict(os.environ, QT_QPA_PLATFORM='windows')
        completed = subprocess.run([sys.executable, '-m', 'tests.test_theme', '--windows-theme-probe'],
                                   cwd=Path(__file__).resolve().parents[1], env=environment,
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(result, {'provider': 'temporary-registry-key', 'native_message': 'WM_SETTINGCHANGE',
                                  'initial': 'light', 'changed': 'dark', 'polling': False})


def _contrast(first, second):
    def luminance(value):
        values = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        values = [part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4 for part in values]
        return .2126 * values[0] + .7152 * values[1] + .0722 * values[2]
    values = sorted((luminance(first), luminance(second)))
    return (values[1] + .05) / (values[0] + .05)


def _windows_native_probe():
    """Only write/delete a UUID test key; never modify Windows appearance."""
    import ctypes
    import uuid
    import winreg
    from PySide6.QtWidgets import QWidget
    from livegallery.theme import WM_SETTINGCHANGE

    path = 'Software\\LiveViewerThemeTest-' + uuid.uuid4().hex
    app = QApplication([])
    controller = None
    window = QWidget()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, 'AppsUseLightTheme', 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, 'SystemUsesLightTheme', 0, winreg.REG_DWORD, 0)
        controller = ThemeController(app, 'system',
            system_provider=lambda: windows_apps_theme(key_path=path), poll_interval_ms=0)
        initial = controller.effective_theme
        window.show()
        window.winId()
        app.processEvents()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'AppsUseLightTheme', 0, winreg.REG_DWORD, 0)
            winreg.SetValueEx(key, 'SystemUsesLightTheme', 0, winreg.REG_DWORD, 1)
        api = ctypes.WinDLL('user32.dll', use_last_error=True, winmode=0x800)
        api.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
            ctypes.c_ssize_t, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
        api.SendMessageTimeoutW.restype = ctypes.c_size_t
        area = ctypes.create_unicode_buffer('ImmersiveColorSet')
        result = ctypes.c_size_t()
        api.SendMessageTimeoutW(0xffff, WM_SETTINGCHANGE, 0, ctypes.addressof(area), 2, 1000,
                                ctypes.byref(result))
        QTest.qWait(150)
        assert initial == 'light', initial
        assert controller.effective_theme == 'dark', controller.effective_theme
        print(json.dumps({'provider': 'temporary-registry-key', 'native_message': 'WM_SETTINGCHANGE',
                          'initial': initial, 'changed': controller.effective_theme, 'polling': False}))
    finally:
        if controller is not None:
            controller.close()
        window.close()
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


if __name__ == '__main__':
    if '--windows-theme-probe' in sys.argv:
        _windows_native_probe()
    else:
        unittest.main()
