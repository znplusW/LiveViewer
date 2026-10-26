import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QAccessible, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from shiboken6 import isValid

from livegallery.fullscreen_gesture_selector import FullscreenGestureSelector
from livegallery.settings_controls import SettingsScrollArea, SettingsWheelGuard, TickCheckBox
from livegallery.theme import stylesheet_for


class FullscreenGestureSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.setStyleSheet(stylesheet_for('dark'))
        layout = QVBoxLayout(self.window)
        self.selector = FullscreenGestureSelector(parent=self.window)
        layout.addWidget(self.selector)
        self.window.resize(360, 120)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.selector.menu.close()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def popup(self):
        self.selector.menu.popup(self.selector.mapToGlobal(QPoint(0, self.selector.height())))
        self.app.processEvents()
        self.assertTrue(self.selector.menu.isVisible())

    def test_only_top_defaults_enabled_and_silent_setters_migrate_legacy_and_preserve_empty(self):
        changed = QSignalSpy(self.selector.selectionChanged)
        self.assertEqual(self.selector.selected_gestures(), ['top'])
        self.assertEqual(set(self.selector.checkboxes), {'top'})
        self.assertEqual(set(self.selector.actions_by_gesture), {'top'})
        self.assertEqual(len(self.selector.menu.actions()), 1)
        self.assertEqual(self.selector.checkboxes['top'].text(), '屏幕上沿下滑')
        for values in (['right', 'top', 'top', 'unsupported'], ['left'], ['right'], None):
            self.selector.set_selected_gestures(values)
            self.assertEqual(self.selector.selected_gestures(), ['top'])
        self.selector.set_selected_gestures(['unsupported', {}, []])
        self.assertEqual(self.selector.selected_gestures(), [])
        self.selector.set_selected_gestures([])
        self.assertEqual(self.selector.selected_gestures(), [])
        self.assertEqual(self.selector.text(), '不启用退出手势')
        self.assertEqual(changed.count(), 0)

    def test_popup_whole_blank_row_text_and_indicator_toggle_sole_option_without_closing(self):
        changed = QSignalSpy(self.selector.selectionChanged)
        self.popup()
        for point in ('blank', 'text', 'indicator'):
            box = self.selector.checkboxes['top']
            self.assertIsInstance(box, TickCheckBox)
            position = QPoint(box.width() - 5, box.height() // 2) if point == 'blank' else QPoint(
                75 if point == 'text' else 20, box.height() // 2)
            QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=position)
            self.app.processEvents()
            self.assertTrue(self.selector.menu.isVisible())
        self.assertEqual(self.selector.selected_gestures(), [])
        self.assertEqual(changed.count(), 3)
        self.assertEqual([changed.at(index)[0] for index in range(3)], [[], ['top'], []])

    def test_space_on_active_menu_action_and_checkbox_keep_popup_open_and_semantics(self):
        self.popup()
        action = self.selector.actions_by_gesture['top']
        self.selector.menu.setActiveAction(action)
        QTest.keyClick(self.selector.menu, Qt.Key.Key_Space)
        self.assertEqual(self.selector.selected_gestures(), [])
        self.assertTrue(self.selector.menu.isVisible())
        box = self.selector.checkboxes['top']
        box.setFocus()
        QTest.keyClick(box, Qt.Key.Key_Space)
        self.assertEqual(self.selector.selected_gestures(), ['top'])
        self.assertTrue(self.selector.menu.isVisible())
        self.assertEqual(QAccessible.queryAccessibleInterface(box).role(), QAccessible.Role.CheckBox)

    def test_actual_window_touch_toggles_once_and_disabled_selector_cannot_change(self):
        self.popup()
        box = self.selector.checkboxes['top']
        handle = self.selector.menu.windowHandle()
        self.assertIsNotNone(handle)
        point = box.mapTo(self.selector.menu, QPoint(box.width() - 6, box.height() // 2))
        changed = QSignalSpy(self.selector.selectionChanged)
        device = QTest.createTouchDevice()
        touch = QTest.touchEvent(handle, device, False)
        touch.press(0, point, handle).commit(); touch.release(0, point, handle).commit()
        self.app.processEvents()
        self.assertEqual(self.selector.selected_gestures(), [])
        self.assertEqual(changed.count(), 1)
        self.assertTrue(self.selector.menu.isVisible())
        self.selector.menu.close()
        self.selector.setEnabled(False)
        QTest.mouseClick(self.selector, Qt.MouseButton.LeftButton)
        self.assertFalse(self.selector.menu.isVisible())
        self.assertEqual(self.selector.selected_gestures(), [])

    def test_top_summary_elides_with_full_tooltip_and_both_themes_remain_blue(self):
        self.assertEqual(self.selector.text(), '屏幕上沿下滑')
        self.selector.setMinimumWidth(0)
        self.selector.setFixedWidth(95)
        self.window.resize(220, 120)
        self.app.processEvents()
        self.assertIn('…', self.selector.text())
        self.assertIn('屏幕上沿下滑', self.selector.toolTip())
        self.assertIn('取消勾选', self.selector.toolTip())
        for obsolete in ('左侧', '右侧', '多选'):
            self.assertNotIn(obsolete, self.selector.toolTip())
        for mode in ('dark', 'light'):
            self.window.setProperty('liveviewerTheme', mode)
            self.window.setStyleSheet(stylesheet_for(mode))
            self.app.processEvents()
            self.assertTrue(all('#3b82f6' in checkbox.styleSheet() for checkbox in self.selector.checkboxes.values()))

    def test_parent_theme_propagation_defers_and_coalesces_child_styles(self):
        class TrackedSelector(FullscreenGestureSelector):
            def __init__(self, parent):
                self.change_depth = 0
                self.applied_depths = []
                super().__init__(parent=parent)

            def changeEvent(self, event):
                self.change_depth += 1
                try:
                    super().changeEvent(event)
                finally:
                    self.change_depth -= 1

            def _apply_option_style(self):
                self.applied_depths.append(self.change_depth)
                super()._apply_option_style()

        selector = TrackedSelector(self.window)
        self.app.processEvents()
        selector.applied_depths.clear()
        selector.set_selected_gestures(['top'])
        for mode in ('light', 'dark', 'light'):
            self.window.setProperty('liveviewerTheme', mode)
            self.window.setStyleSheet(stylesheet_for(mode))
            QApplication.sendEvent(selector, QEvent(QEvent.Type.FontChange))
        self.assertEqual(selector.applied_depths, [],
                         'Child QSS was changed inside the parent style propagation stack')
        self.app.processEvents()
        self.assertEqual(selector.applied_depths, [0])
        self.assertEqual(selector.selected_gestures(), ['top'])
        self.assertTrue(all('#202833' in box.styleSheet() and '#3b82f6' in box.styleSheet()
                            for box in selector.checkboxes.values()))

    def test_unchanged_style_does_not_repolish_and_pending_update_dies_with_selector(self):
        boxes = list(self.selector.checkboxes.values())
        with patch.object(boxes[0], 'setStyleSheet', wraps=boxes[0].setStyleSheet) as restyle:
            for kind in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange, QEvent.Type.FontChange):
                QApplication.sendEvent(self.selector, QEvent(kind))
            self.assertTrue(self.selector._option_style_timer.isActive())
            self.app.processEvents()
            self.assertEqual(restyle.call_count, 0)
        parent = QWidget()
        selector = FullscreenGestureSelector(parent=parent)
        timer = selector._option_style_timer
        self.assertTrue(timer.isActive())
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(selector))
        self.assertFalse(isValid(timer))
        self.app.processEvents()

    def test_isolated_process_settings_save_theme_bursts_open_menu_and_pending_delete(self):
        # A Qt crash must fail this parent process as a return code, rather than
        # taking down the entire unit runner or hiding a Python callback error.
        script = textwrap.dedent('''
            import json
            import sys
            from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
            from PySide6.QtTest import QTest
            from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget
            from livegallery.app import MainWindow, SettingsDialog
            from livegallery.fullscreen_gesture_selector import FullscreenGestureSelector
            app = QApplication([])
            errors = []
            sys.excepthook = lambda kind, value, tb: errors.append(f'{kind.__name__}: {value}')
            window = MainWindow()
            window.show()
            app.processEvents()
            state = ['dark']
            window.theme_controller.system_provider = lambda: state[0]
            saved = []
            def save(mode):
                def choose():
                    dialog = app.activeModalWidget()
                    try:
                        assert isinstance(dialog, SettingsDialog)
                        dialog.theme_mode.setCurrentIndex(dialog.theme_mode.findData(mode))
                        buttons = dialog.findChild(QDialogButtonBox)
                        QTest.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
                    except Exception as error:
                        errors.append(repr(error))
                        if dialog is not None:
                            dialog.reject()
                QTimer.singleShot(0, choose)
                window.open_settings()
                app.processEvents()
                assert window.theme_controller.mode == mode
                saved.append(mode)
            for mode in ('dark', 'light', 'system') * 4:
                save(mode)
            dialog = SettingsDialog(window.preferences, window)
            dialog.show()
            app.processEvents()
            selector = dialog.fullscreen_exit_gestures
            selector.set_selected_gestures(['top'])
            selector.menu.popup(selector.mapToGlobal(QPoint(0, selector.height())))
            app.processEvents()
            for scheme in ('light', 'dark') * 12:
                state[0] = scheme
                window.theme_controller.refresh_system()
                app.processEvents()
                assert selector.menu.isVisible()
                assert selector.selected_gestures() == ['top']
                assert all('#3b82f6' in box.styleSheet() for box in selector.checkboxes.values())
            selector.menu.close()
            dialog.close()
            # Delete an entire selector tree before its queued initialization.
            for _ in range(12):
                parent = QWidget()
                pending = FullscreenGestureSelector(parent=parent)
                assert pending._option_style_timer.isActive()
                parent.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
            window.close()
            assert window.wait_for_shutdown(10), window._shutdown_error
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert not errors, errors
            print(json.dumps({'platform': app.platformName(), 'actual_settings_saves': len(saved),
                              'open_menu_system_theme_changes': 24, 'pending_deletes': 12,
                              'callback_errors': errors}))
        ''')
        env = dict(os.environ)
        # Keep the caller's Qt backend: CI can run this same process test with
        # the Windows platform while headless unit runs retain offscreen.
        env['PYTHONFAULTHANDLER'] = '1'
        with tempfile.TemporaryDirectory() as folder:
            env['LIVEGALLERY_DATA_DIR'] = str(Path(folder).resolve() / 'data')
            result = subprocess.run([sys.executable, '-c', script], env=env,
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        self.assertIn('"actual_settings_saves": 12', result.stdout)
        self.assertIn('"open_menu_system_theme_changes": 24', result.stdout)
        self.assertIn('"callback_errors": []', result.stdout)

    def test_closed_wheel_routes_to_settings_page_once_without_changing_gestures(self):
        scroll = SettingsScrollArea()
        content = QWidget()
        layout = QVBoxLayout(content)
        self.selector.setParent(content)
        layout.addWidget(self.selector)
        content.setMinimumHeight(1400)
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.resize(350, 250)
        guard = SettingsWheelGuard(scroll)
        guard.protect(self.selector)
        scroll.show()
        self.app.processEvents()
        before = self.selector.selected_gestures()
        scrolling = QSignalSpy(scroll.verticalScrollBar().valueChanged)
        point = self.selector.rect().center()
        event = QWheelEvent(QPointF(point), QPointF(self.selector.mapToGlobal(point)), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.selector, event)
        self.assertTrue(event.isAccepted())
        self.assertGreater(scroll.verticalScrollBar().value(), 0)
        self.assertEqual(scrolling.count(), 1)
        self.assertEqual(self.selector.selected_gestures(), before)
        self.selector.setParent(self.window)
        scroll.close(); scroll.deleteLater()


if __name__ == '__main__':
    unittest.main()
