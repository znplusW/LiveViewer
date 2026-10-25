"""Actual settings wheel, checkbox accessibility and input behavior."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QAccessible, QColor, QImage, QKeySequence, QTouchEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QSpinBox,
                               QStyle, QStyleOptionComboBox, QStyleOptionSpinBox,
                               QVBoxLayout, QWidget)

from livegallery.app import MediaTypeFilter, SettingsDialog, STYLE
from livegallery.settings import Preferences
from livegallery.settings_controls import PillSwitch, TickCheckBox
from livegallery import theme as appearance


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

    def page_touch(self, control, *, delta=QPoint(), cancel=False):
        self.dialog.activateWindow()
        QTest.qWait(20)
        self.scroll.ensureWidgetVisible(control)
        self.application.processEvents()
        target = self.dialog.windowHandle()
        position = target.mapFromGlobal(control.mapToGlobal(control.rect().center()))
        device = QTest.createTouchDevice()
        sequence = QTest.touchEvent(target, device, False)
        sequence.press(0, position, target).commit()
        if not delta.isNull():
            sequence.move(0, position + delta, target).commit()
        if cancel:
            QApplication.sendEvent(self.scroll.viewport(), QTouchEvent(QEvent.Type.TouchCancel, device))
        sequence.release(0, position + delta, target).commit()
        QTest.qWait(20)

    def test_window_routed_swipes_over_settings_controls_scroll_without_edits(self):
        controls = (self.dialog.theme_mode, self.dialog.autoplay, self.dialog.hold,
                    self.dialog.hold.lineEdit(), self.dialog.live_key)
        for control in controls:
            with self.subTest(control=type(control).__name__):
                self.scroll.ensureWidgetVisible(control)
                self.application.processEvents()
                before = self.bar.value()
                values = (self.dialog.theme_mode.currentIndex(), self.dialog.autoplay.isChecked(),
                          self.dialog.hold.value(), self.dialog.live_key.keySequence())
                self.page_touch(control, delta=QPoint(3, -90))
                self.assertEqual(self.bar.value(), before + 90)
                self.assertIsNone(QApplication.activePopupWidget())
                self.assertEqual(values, (self.dialog.theme_mode.currentIndex(), self.dialog.autoplay.isChecked(),
                                         self.dialog.hold.value(), self.dialog.live_key.keySequence()))
        # Drag down starting on ordinary page text, not just an input control.
        label = self.dialog.horizontal_scroll_help
        self.scroll.ensureWidgetVisible(label)
        self.application.processEvents()
        before = self.bar.value()
        self.page_touch(label, delta=QPoint(0, 70))
        self.assertEqual(self.bar.value(), max(0, before - 70))

    def test_window_routed_control_taps_keep_switch_combo_and_text_editing(self):
        before = self.dialog.autoplay.isChecked()
        self.page_touch(self.dialog.autoplay)
        self.assertEqual(self.dialog.autoplay.isChecked(), not before)
        self.page_touch(self.dialog.theme_mode)
        self.assertTrue(self.dialog.theme_mode.view().isVisible())
        self.dialog.theme_mode.hidePopup()
        self.page_touch(self.dialog.hold.lineEdit())
        self.assertTrue(self.dialog.hold.lineEdit().hasFocus())
        self.dialog.hold.lineEdit().selectAll()
        QTest.keyClicks(self.dialog.hold.lineEdit(), '650')
        QTest.keyClick(self.dialog.hold.lineEdit(), Qt.Key.Key_Tab)
        self.assertEqual(self.dialog.hold.value(), 650)
        self.page_touch(self.dialog.live_key)
        self.assertTrue(self.dialog.live_key.hasFocus())
        QTest.keyClick(self.dialog.live_key, Qt.Key.Key_J)
        self.assertEqual(self.dialog.live_key.keySequence(), QKeySequence('J'))

    def test_routed_touch_uses_page_children_when_desktop_hit_test_is_unavailable(self):
        # On Windows, native QWindow touch delivery can be valid while the
        # desktop-wide widgetAt lookup returns None (e.g. after Explorer use).
        # The receiver is already known; a second desktop lookup must not lose
        # taps or redirect them to a different window at the same coordinates.
        unrelated = QWidget()
        try:
            for desktop_target in (None, unrelated):
                with self.subTest(desktop_target=desktop_target), \
                        patch.object(QApplication, 'widgetAt', return_value=desktop_target):
                    before = self.dialog.autoplay.isChecked()
                    changed = QSignalSpy(self.dialog.autoplay.toggled)
                    clicked = QSignalSpy(self.dialog.autoplay.clicked)
                    self.page_touch(self.dialog.autoplay)
                    self.assertEqual(self.dialog.autoplay.isChecked(), not before)
                    self.assertEqual((changed.count(), clicked.count()), (1, 1))
                    self.page_touch(self.dialog.theme_mode)
                    self.assertTrue(self.dialog.theme_mode.view().isVisible())
                    self.dialog.theme_mode.hidePopup()
                    self.page_touch(self.dialog.hold.lineEdit())
                    self.assertTrue(self.dialog.hold.lineEdit().hasFocus())
                    before = self.dialog.autoplay.isChecked()
                    start = self.bar.value()
                    self.page_touch(self.dialog.hold.lineEdit(), delta=QPoint(0, -90))
                    self.assertEqual(self.bar.value(), start + 90)
                    self.assertEqual(self.dialog.autoplay.isChecked(), before)
        finally:
            unrelated.deleteLater()

    def test_cancelled_settings_contact_does_not_toggle_and_mouse_remains_usable(self):
        before = self.dialog.autoplay.isChecked()
        self.page_touch(self.dialog.autoplay, cancel=True)
        self.assertEqual(self.dialog.autoplay.isChecked(), before)
        QTest.mouseClick(self.dialog.autoplay, Qt.MouseButton.LeftButton)
        self.assertEqual(self.dialog.autoplay.isChecked(), not before)

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


class ControlButtonAppearanceTests(unittest.TestCase):
    """Render real Qt subcontrols: a valid QSS string alone cannot prove an arrow is visible."""
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.layout = QVBoxLayout(self.window)
        self.combo = QComboBox()
        self.combo.addItems(['First choice', 'Second choice'])
        self.spin = QSpinBox()
        self.spin.setRange(0, 10)
        self.spin.setValue(5)
        self.layout.addWidget(self.combo)
        self.layout.addWidget(self.spin)
        self.window.resize(320, 150)

    def tearDown(self):
        self.combo.hidePopup()
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()

    def use_theme(self, mode, *, override=''):
        self.window.setPalette(appearance.theme_palette(mode))
        self.window.setStyleSheet(appearance.stylesheet_for(mode) + override)
        self.window.show()
        self.window.activateWindow()
        QTest.mouseMove(self.window, QPoint(1, 1))
        self.application.processEvents()

    def button_rect(self, field, subcontrol):
        option = QStyleOptionComboBox() if field is self.combo else QStyleOptionSpinBox()
        field.initStyleOption(option)
        control = QStyle.ComplexControl.CC_ComboBox if field is self.combo else QStyle.ComplexControl.CC_SpinBox
        return field.style().subControlRect(control, option, subcontrol, field)

    def render(self, field, ratio=1):
        image = QImage(field.width() * ratio, field.height() * ratio, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        field.render(image)
        return image

    def color(self, field, position):
        return self.render(field).pixelColor(position).name()

    def assert_visible_arrow(self, field, rect, color, ratio=1):
        image = self.render(field, ratio)
        target = QColor(color)
        center = rect.center()
        region = QRect((center.x() - 9) * ratio, (center.y() - 6) * ratio, 19 * ratio, 13 * ratio)
        ink = []
        for y in range(region.top(), region.bottom() + 1):
            for x in range(region.left(), region.right() + 1):
                pixel = image.pixelColor(x, y)
                if max(abs(pixel.red() - target.red()), abs(pixel.green() - target.green()),
                       abs(pixel.blue() - target.blue())) <= 20:
                    ink.append((x, y))
        self.assertGreaterEqual(len(ink), 12 * ratio * ratio, 'arrow glyph is missing or too faint')
        self.assertGreaterEqual(max(x for x, _ in ink) - min(x for x, _ in ink) + 1, 10 * ratio)
        self.assertGreaterEqual(max(y for _, y in ink) - min(y for _, y in ink) + 1, 5 * ratio)

    def test_visible_large_arrows_and_buttons_work_at_one_and_two_times_resolution(self):
        for mode in ('light', 'dark'):
            with self.subTest(theme=mode):
                self.use_theme(mode)
                colors = appearance.theme_tokens(mode)
                controls = ((self.combo, QStyle.SubControl.SC_ComboBoxArrow),
                            (self.spin, QStyle.SubControl.SC_SpinBoxUp),
                            (self.spin, QStyle.SubControl.SC_SpinBoxDown))
                for field, subcontrol in controls:
                    rect = self.button_rect(field, subcontrol)
                    self.assertGreaterEqual(rect.width(), 34)
                    self.assertGreaterEqual(rect.height(), 18)
                    self.assertGreaterEqual(field.height(), 40)
                    # The arrow area has a separate background from the editable field.
                    self.assertEqual(self.color(field, QPoint(rect.left() + 3, rect.center().y())),
                                     colors['control_button'])
                    for ratio in (1, 2):
                        self.assert_visible_arrow(field, rect, colors['control_arrow'], ratio)
                before = self.spin.value()
                QTest.mouseClick(self.spin, Qt.MouseButton.LeftButton,
                                 pos=self.button_rect(self.spin, QStyle.SubControl.SC_SpinBoxUp).center())
                self.assertEqual(self.spin.value(), before + 1)
                QTest.mouseClick(self.spin, Qt.MouseButton.LeftButton,
                                 pos=self.button_rect(self.spin, QStyle.SubControl.SC_SpinBoxDown).center())
                self.assertEqual(self.spin.value(), before)
                QTest.mouseClick(self.combo, Qt.MouseButton.LeftButton,
                                 pos=self.button_rect(self.combo, QStyle.SubControl.SC_ComboBoxArrow).center())
                self.assertTrue(self.combo.view().isVisible())
                self.combo.hidePopup()

    def test_button_hover_pressed_disabled_limit_and_focus_states_are_visible(self):
        for mode in ('light', 'dark'):
            with self.subTest(theme=mode):
                self.use_theme(mode)
                colors = appearance.theme_tokens(mode)
                self.spin.setEnabled(True)
                self.spin.setValue(5)
                self.combo.setEnabled(True)
                combo_rect = self.button_rect(self.combo, QStyle.SubControl.SC_ComboBoxArrow)
                combo_sample = QPoint(combo_rect.left() + 3, combo_rect.center().y())
                QTest.mouseMove(self.combo, combo_rect.center())
                self.application.processEvents()
                self.assertEqual(self.color(self.combo, combo_sample), colors['control_button_hover'])
                self.combo.showPopup()
                self.application.processEvents()
                self.assertEqual(self.color(self.combo, combo_sample), colors['control_button_pressed'])
                self.combo.hidePopup()
                self.window.activateWindow()
                self.combo.setFocus()
                self.application.processEvents()
                self.assertTrue(self.combo.hasFocus())
                self.assertEqual(self.color(self.combo, QPoint(0, self.combo.height() // 2)), colors['focus'])
                self.combo.setEnabled(False)
                self.assertEqual(self.color(self.combo, combo_sample), colors['control_button_disabled'])
                self.assert_visible_arrow(self.combo, combo_rect, colors['control_arrow_disabled'])
                QTest.mouseClick(self.combo, Qt.MouseButton.LeftButton, pos=combo_rect.center())
                self.assertFalse(self.combo.view().isVisible())
                self.combo.setEnabled(True)
                rect = self.button_rect(self.spin, QStyle.SubControl.SC_SpinBoxUp)
                sample = QPoint(rect.left() + 3, rect.center().y())
                QTest.mouseMove(self.spin, rect.center())
                self.application.processEvents()
                self.assertEqual(self.color(self.spin, sample), colors['control_button_hover'])
                QTest.mousePress(self.spin, Qt.MouseButton.LeftButton, pos=rect.center())
                self.assertEqual(self.color(self.spin, sample), colors['control_button_pressed'])
                QTest.mouseRelease(self.spin, Qt.MouseButton.LeftButton, pos=rect.center())
                self.spin.setFocus()
                self.application.processEvents()
                self.assertEqual(self.color(self.spin, QPoint(0, self.spin.height() // 2)), colors['focus'])
                self.spin.setValue(self.spin.maximum())
                QTest.mouseMove(self.window, QPoint(1, 1))
                self.assertEqual(self.color(self.spin, sample), colors['control_button_disabled'])
                self.assert_visible_arrow(self.spin, rect, colors['control_arrow_disabled'])
                self.spin.setEnabled(False)
                self.assertEqual(self.color(self.spin, sample), colors['control_button_disabled'])
                self.assert_visible_arrow(self.spin, rect, colors['control_arrow_disabled'])
                before = self.spin.value()
                QTest.mouseClick(self.spin, Qt.MouseButton.LeftButton, pos=rect.center())
                self.assertEqual(self.spin.value(), before)
                self.spin.setEnabled(True)
                self.spin.setValue(5)

    def test_arrow_resources_load_from_source_and_frozen_directories_containing_spaces(self):
        with tempfile.TemporaryDirectory(prefix='LiveViewer resource path ') as temporary:
            base = Path(temporary).resolve()
            source = Path(appearance.__file__).resolve().parents[1] / 'assets' / 'controls'
            shutil.copytree(source, base / 'assets' / 'controls')
            for frozen in (False, True):
                with self.subTest(frozen=frozen), \
                     patch.object(appearance, '__file__', str(base / 'livegallery' / 'theme.py')), \
                     patch.object(appearance.sys, '_MEIPASS', str(base) if frozen else None, create=True):
                    url = appearance._control_icon_url('light', 'down')
                    self.assertEqual(url, 'url("' + (base / 'assets/controls/chevron-down-light.svg').as_posix() + '")')
                    self.use_theme('light', override=appearance._control_stylesheet('light'))
                    for field, subcontrol in ((self.combo, QStyle.SubControl.SC_ComboBoxArrow),
                                              (self.spin, QStyle.SubControl.SC_SpinBoxUp),
                                              (self.spin, QStyle.SubControl.SC_SpinBoxDown)):
                        self.assert_visible_arrow(field, self.button_rect(field, subcontrol),
                                                  appearance.theme_tokens('light')['control_arrow'], 2)
                    self.spin.setEnabled(False)
                    self.assert_visible_arrow(self.spin,
                        self.button_rect(self.spin, QStyle.SubControl.SC_SpinBoxUp),
                        appearance.theme_tokens('light')['control_arrow_disabled'], 2)
                    self.spin.setEnabled(True)

    def test_compact_dropdown_edit_fields_fit_scope_and_sort_labels_beside_buttons(self):
        for mode in ('light', 'dark'):
            self.use_theme(mode)
            for text, width in (('已选文件夹', 145), ('文件名排序', 150)):
                with self.subTest(theme=mode, label=text):
                    self.combo.setItemText(0, text)
                    self.combo.setCurrentIndex(0)
                    self.combo.setFixedWidth(width)
                    self.application.processEvents()
                    option = QStyleOptionComboBox()
                    self.combo.initStyleOption(option)
                    edit = self.combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox,
                        option, QStyle.SubControl.SC_ComboBoxEditField, self.combo)
                    self.assertGreaterEqual(edit.width(), self.combo.fontMetrics().horizontalAdvance(text) + 2)
                    button = self.button_rect(self.combo, QStyle.SubControl.SC_ComboBoxArrow)
                    self.assertGreaterEqual(button.width(), 34)
                    self.assertLess(edit.right(), button.left())


if __name__ == '__main__':
    unittest.main()
