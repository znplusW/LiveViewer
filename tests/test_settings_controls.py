"""Actual settings wheel, checkbox accessibility and input behavior."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QAccessible, QKeySequence, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog

from livegallery.app import MediaTypeFilter, SettingsDialog, STYLE
from livegallery.settings import Preferences
from livegallery.settings_controls import PillSwitch, TickCheckBox


class WheelRecorder(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.events = []

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            self.events.append((event.pixelDelta(), event.angleDelta(), event.phase(), event.inverted()))
        return False


class SettingsControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'LIVEGALLERY_DATA_DIR': str(self.base / 'data')})
        self.environment.start()
        self.directory = patch('livegallery.app.app_directory', return_value=self.base)
        self.directory.start()
        self.dialog = SettingsDialog(Preferences())
        self.dialog.setStyleSheet(STYLE)
        self.dialog.resize(680, 480)
        self.dialog.show()
        self.dialog.activateWindow()
        self.application.processEvents()
        self.scroll = self.dialog.scroll_area
        self.bar = self.scroll.verticalScrollBar()
        self.assertGreater(self.bar.maximum(), 200)

    def tearDown(self):
        self.dialog.reject()
        self.dialog.deleteLater()
        self.application.processEvents()
        self.directory.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def wheel(self, target, *, angle=(0, -120), pixels=(0, 0),
              phase=Qt.ScrollPhase.NoScrollPhase, inverted=False):
        center = target.rect().center()
        event = QWheelEvent(QPointF(center), QPointF(target.mapToGlobal(center)),
                            QPoint(*pixels), QPoint(*angle), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, phase, inverted)
        QApplication.sendEvent(target, event)
        return event

    def test_closed_fields_and_spin_editors_scroll_page_once_even_with_focus(self):
        combos = (self.dialog.theme_mode, self.dialog.mode, self.dialog.zoom_mode,
                  self.dialog.horizontal_scroll_mode, self.dialog.cache_location)
        spins = (self.dialog.hold, self.dialog.cache)
        controls = (*combos, *spins)
        targets = (*controls, self.dialog.hold.lineEdit(), self.dialog.cache.lineEdit())
        for pixels, angle in (((0, 0), (0, -120)), ((0, -23), (0, 0)), ((0, -23), (0, -120))):
            midpoint = self.bar.maximum() // 2
            self.bar.setValue(midpoint)
            self.wheel(self.scroll.viewport(), pixels=pixels, angle=angle)
            expected = self.bar.value() - midpoint
            self.assertGreater(expected, 0)
            for focused in (False, True):
                for target in targets:
                    with self.subTest(pixels=pixels, angle=angle, focused=focused, target=target):
                        self.dialog.live_key.setFocus()
                        if focused:
                            target.setFocus()
                        self.application.processEvents()
                        focus = self.application.focusWidget()
                        values = [field.currentIndex() for field in combos] + [field.value() for field in spins]
                        self.bar.setValue(midpoint)
                        event = self.wheel(target, pixels=pixels, angle=angle)
                        self.assertTrue(event.isAccepted())
                        self.assertEqual(self.bar.value() - midpoint, expected)
                        self.assertEqual(values, [field.currentIndex() for field in combos] +
                                         [field.value() for field in spins])
                        self.assertIs(self.application.focusWidget(), focus)
                        self.assertFalse(target.focusPolicy() & Qt.FocusPolicy.WheelFocus == Qt.FocusPolicy.WheelFocus)

    def test_forwarded_wheel_keeps_pixel_angle_phase_and_direction_without_duplicate(self):
        recorder = WheelRecorder(self.scroll.viewport())
        self.scroll.viewport().installEventFilter(recorder)
        self.bar.setValue(self.bar.maximum() // 2)
        self.wheel(self.dialog.cache.lineEdit(), angle=(0, -30), pixels=(0, -7),
                   phase=Qt.ScrollPhase.ScrollMomentum, inverted=True)
        self.assertEqual(recorder.events, [(QPoint(0, -7), QPoint(0, -30),
                                           Qt.ScrollPhase.ScrollMomentum, True)])
        before = self.bar.value()
        self.wheel(self.dialog.hold, angle=(0, 0), pixels=(0, 11), inverted=True)
        self.assertEqual(self.bar.value(), before - 11)

    def test_open_combo_list_scroll_and_click_and_keyboard_edits_are_native(self):
        combo = self.dialog.mode
        combo.addItems([f'Test choice {number}' for number in range(60)])
        combo.setMaxVisibleItems(5)
        self.scroll.ensureWidgetVisible(combo)
        combo.setFocus()
        combo.showPopup()
        self.application.processEvents()
        view = combo.view()
        self.assertTrue(view.isVisible())
        page_before = self.bar.value()
        before = view.verticalScrollBar().value()
        self.wheel(view.viewport())
        self.assertGreater(view.verticalScrollBar().value(), before)
        self.assertEqual(self.bar.value(), page_before)
        index = view.model().index(4, 0)
        view.scrollTo(index)
        self.application.processEvents()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())
        self.assertEqual(combo.currentIndex(), 4)
        self.assertFalse(view.isVisible())
        self.dialog.zoom_mode.setFocus()
        QTest.keyClick(self.dialog.zoom_mode, Qt.Key.Key_Down)
        self.assertEqual(self.dialog.zoom_mode.currentIndex(), 1)
        self.dialog.hold.setFocus()
        QTest.keyClick(self.dialog.hold, Qt.Key.Key_Up)
        self.assertEqual(self.dialog.hold.value(), 350)
        editor = self.dialog.hold.lineEdit()
        editor.selectAll()
        QTest.keyClicks(editor, '450')
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        self.assertEqual(self.dialog.hold.value(), 450)

    def test_switches_keep_checkbox_accessibility_click_label_space_touch_and_disabled(self):
        for switch in (self.dialog.autoplay, self.dialog.muted, self.dialog.recursive,
                       self.dialog.portable, self.dialog.override_keys):
            self.assertIsInstance(switch, PillSwitch)
            self.assertIsInstance(switch, QCheckBox)
        switch = self.dialog.autoplay
        self.scroll.ensureWidgetVisible(switch)
        self.application.processEvents()
        interface = QAccessible.queryAccessibleInterface(switch)
        self.assertEqual(interface.role(), QAccessible.Role.CheckBox)
        self.assertEqual(interface.text(QAccessible.Text.Name), switch.text())
        self.assertFalse(interface.state().checked)
        self.assertGreaterEqual(switch.height(), 36)
        changed = QSignalSpy(switch.toggled)
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(20, switch.height() // 2))
        self.assertTrue(switch.isChecked())
        self.assertTrue(interface.state().checked)
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton,
                         pos=QPoint(switch.width() - 10, switch.height() // 2))
        self.assertFalse(switch.isChecked())
        switch.setFocus()
        QTest.keyClick(switch, Qt.Key.Key_Space)
        self.assertTrue(switch.isChecked())
        device = QTest.createTouchDevice()
        center = switch.rect().center()
        QTest.touchEvent(switch, device).press(0, center, switch).commit()
        QTest.touchEvent(switch, device).release(0, center, switch).commit()
        self.application.processEvents()
        self.assertFalse(switch.isChecked())
        self.assertEqual(changed.count(), 4)
        switch.setEnabled(False)
        QTest.keyClick(switch, Qt.Key.Key_Space)
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=center)
        self.assertFalse(switch.isChecked())
        self.assertTrue(interface.state().disabled)
        self.assertEqual(changed.count(), 4)

    def test_switch_conflict_confirmation_and_cancel_keep_original_preferences(self):
        self.dialog.live_once_key.setKeySequence(QKeySequence('Right'))
        self.dialog.autoplay.click()
        with patch('livegallery.app.QMessageBox.information') as message:
            self.dialog._accept()
        self.assertIn('请开启确认替换', message.call_args.args[2])
        self.assertTrue(self.dialog.override_keys.isVisible())
        self.dialog.override_keys.click()
        self.dialog._accept()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse((self.base / 'data' / 'settings.json').exists())
        self.dialog.reject()
        self.assertFalse(Preferences.load().autoplay)

    def test_multi_select_menu_keeps_tick_checkboxes_and_stays_open_after_each_click(self):
        self.dialog.reject()
        button = MediaTypeFilter()
        button.setStyleSheet(STYLE)
        button.show()
        menu = button.menu
        try:
            menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
            self.application.processEvents()
            changed = QSignalSpy(button.changed)
            for box, expected in ((button.live, False), (button.static, False), (button.pending, True)):
                self.assertIsInstance(box, TickCheckBox)
                self.assertIsInstance(box, QCheckBox)
                self.assertNotIsInstance(box, PillSwitch)
                self.assertEqual(QAccessible.queryAccessibleInterface(box).role(), QAccessible.Role.CheckBox)
                self.assertEqual(box.width(), button.pending.width())
                # The shortest label has unused space to its right. That
                # space belongs to the same option, not to a dead menu area.
                position = QPoint(box.width() - 6, box.height() // 2) if box is button.live else box.rect().center()
                QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=position)
                self.assertEqual(box.isChecked(), expected)
                self.assertTrue(menu.isVisible())
            QTest.mouseClick(button.live, Qt.MouseButton.LeftButton,
                             pos=QPoint(button.live.width() - 6, button.live.height() // 2))
            self.assertTrue(button.live.isChecked())
            self.assertTrue(menu.isVisible())
            QTest.mouseClick(button.pending, Qt.MouseButton.LeftButton,
                             pos=QPoint(20, button.pending.height() // 2))
            self.assertFalse(button.pending.isChecked())
            self.assertTrue(menu.isVisible())
            self.assertEqual(changed.count(), 5)
            self.assertIn('Live Photos', button.text())
        finally:
            menu.close()
            button.close()
            button.deleteLater()


if __name__ == '__main__':
    unittest.main()
