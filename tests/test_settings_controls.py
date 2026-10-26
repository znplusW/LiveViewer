"""Actual settings wheel, checkbox accessibility and input behavior."""
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QAccessible, QColor, QEventPoint, QImage, QKeySequence, QMouseEvent, QTouchEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QSpinBox,
                               QStyle, QStyleOptionComboBox, QStyleOptionSpinBox,
                               QVBoxLayout, QWidget)
from shiboken6 import isValid

from livegallery.app import MediaTypeFilter, SettingsDialog, STYLE
from livegallery.settings import Preferences
from livegallery.settings_controls import PillSwitch, SettingsScrollArea, TickCheckBox
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

    def touch_drag(self, start, distance):
        target = self.dialog.windowHandle()
        device = QTest.createTouchDevice()
        touch = QTest.touchEvent(target, device, False)
        touch.press(0, start, target).commit()
        QTest.qWait(20)
        for fraction in (.2, .45, .7, 1):
            touch.move(0, start + QPoint(0, round(distance * fraction)), target).commit()
            QTest.qWait(20)
        during = self.bar.value()
        touch.release(0, start + QPoint(0, distance), target).commit()
        QTest.qWait(80)
        return during

    def raw_touch(self, device, kind, points):
        # Qt's QWidget notify path drops orphan Update before event filters.
        # Exercise the owned-contact state machine directly; the QWindow
        # tests below separately verify real Qt routing and control effects.
        viewport = self.scroll.viewport()
        event = QTouchEvent(kind, device, Qt.KeyboardModifier.NoModifier,
            [QEventPoint(identifier, state, QPointF(position), viewport.mapToGlobal(QPointF(position)))
             for identifier, state, position in points])
        self.scroll._filter_touch_event(viewport, event)
        return event

    def send_mouse_click(self, target, position, source):
        local = QPointF(position)
        global_position = target.mapToGlobal(local)
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            event = QMouseEvent(kind, local, target.window().mapFromGlobal(global_position), global_position,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier, source)
            QApplication.sendEvent(target, event)

    def test_single_finger_settings_drag_scrolls_both_directions_before_release(self):
        for distance in (-100, 100):
            with self.subTest(distance=distance):
                self.bar.setValue(self.bar.maximum() // 2)
                self.application.processEvents()
                before = self.bar.value()
                point = self.scroll.viewport().mapTo(self.dialog,
                    QPoint(6, self.scroll.viewport().height() // 2))
                during = self.touch_drag(point, distance)
                self.assertGreater((during - before) * -distance, 0)

    def test_touch_drag_from_switch_scrolls_without_click_and_next_tap_toggles_once(self):
        control = self.dialog.autoplay
        center = QPoint(20, control.height() // 2)
        self.bar.setValue(control.mapTo(self.scroll.widget(), center).y() - self.scroll.viewport().height() // 2)
        self.application.processEvents()
        before = self.bar.value()
        checked = control.isChecked()
        toggled = QSignalSpy(control.toggled)
        clicked = QSignalSpy(control.clicked)
        during = self.touch_drag(control.mapTo(self.dialog, center), -90)
        self.assertGreater(during, before)
        self.assertEqual((control.isChecked(), toggled.count(), clicked.count()), (checked, 0, 0))
        self.assertFalse(control.isDown())
        self.scroll.ensureWidgetVisible(control)
        self.application.processEvents()
        target = self.dialog.windowHandle()
        point = control.mapTo(self.dialog, center)
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.release(0, point, target).commit()
        QTest.qWait(80)
        self.assertEqual((control.isChecked(), toggled.count(), clicked.count()), (not checked, 1, 1))

    def test_touch_drag_from_closed_dropdown_scrolls_without_opening_or_changing_it(self):
        control = self.dialog.mode
        center = control.rect().center()
        self.bar.setValue(control.mapTo(self.scroll.widget(), center).y() - self.scroll.viewport().height() // 2)
        self.application.processEvents()
        before, index = self.bar.value(), control.currentIndex()
        during = self.touch_drag(control.mapTo(self.dialog, center), -90)
        self.assertGreater(during, before)
        self.assertEqual(control.currentIndex(), index)
        self.assertFalse(control.view().isVisible())

    def test_sparse_update_pressed_and_released_ignore_unknown_stationary_contacts(self):
        # Replay the observed Qt event shape at the settings viewport; this is
        # a state-machine regression, not a claim about physical OS injection.
        self.bar.setValue(self.bar.maximum() // 2)
        before = self.bar.value()
        device = QTest.createTouchDevice()
        point = QPointF(8, self.scroll.viewport().height() // 2)
        old = (177, QEventPoint.State.Stationary, QPointF(500, 600))
        self.raw_touch(device, QEvent.Type.TouchUpdate, [old, (187, QEventPoint.State.Pressed, point)])
        self.raw_touch(device, QEvent.Type.TouchUpdate,
                       [old, (187, QEventPoint.State.Updated, point - QPointF(0, 80))])
        self.assertEqual(self.bar.value(), before + 80)
        self.raw_touch(device, QEvent.Type.TouchUpdate,
                       [old, (187, QEventPoint.State.Released, point - QPointF(0, 80))])
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(177, QEventPoint.State.Updated, point)])
        self.assertEqual(self.bar.value(), before + 80)
        control = self.dialog.autoplay
        self.scroll.ensureWidgetVisible(control)
        self.application.processEvents()
        point = control.mapTo(self.scroll.viewport(), control.rect().center())
        checked = control.isChecked()
        changed = QSignalSpy(control.toggled)
        self.raw_touch(device, QEvent.Type.TouchUpdate, [old, (188, QEventPoint.State.Pressed, point)])
        self.assertEqual(changed.count(), 0)
        self.raw_touch(device, QEvent.Type.TouchUpdate, [old, (188, QEventPoint.State.Released, point)])
        self.assertEqual((control.isChecked(), changed.count()), (not checked, 1))

    def test_multitouch_cancel_and_hidden_page_never_finish_as_control_clicks(self):
        control = self.dialog.autoplay
        self.scroll.ensureWidgetVisible(control)
        self.application.processEvents()
        changed = QSignalSpy(control.toggled)
        for cancelled_by in ('second-finger', 'cancel', 'hide'):
            with self.subTest(cancelled_by=cancelled_by):
                device = QTest.createTouchDevice()
                point = QPointF(control.mapTo(self.scroll.viewport(), control.rect().center()))
                before = self.bar.value()
                self.raw_touch(device, QEvent.Type.TouchUpdate, [(1, QEventPoint.State.Pressed, point)])
                if cancelled_by == 'second-finger':
                    self.raw_touch(device, QEvent.Type.TouchUpdate,
                        [(1, QEventPoint.State.Stationary, point),
                         (2, QEventPoint.State.Pressed, point + QPointF(40, 0))])
                    self.raw_touch(device, QEvent.Type.TouchUpdate,
                        [(1, QEventPoint.State.Updated, point - QPointF(0, 80)),
                         (2, QEventPoint.State.Released, point + QPointF(40, -80))])
                elif cancelled_by == 'cancel':
                    self.raw_touch(device, QEvent.Type.TouchCancel, [])
                else:
                    self.dialog.hide()
                    self.dialog.show()
                    self.application.processEvents()
                self.raw_touch(device, QEvent.Type.TouchUpdate, [(1, QEventPoint.State.Released, point)])
                self.assertEqual(self.bar.value(), before)
                self.assertEqual(changed.count(), 0)
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(3, QEventPoint.State.Pressed, point)])
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(3, QEventPoint.State.Released, point)])
        self.assertEqual(changed.count(), 1)

    def test_drag_tail_is_scoped_and_physical_mouse_wheel_and_open_popup_still_work(self):
        control = self.dialog.autoplay
        center = control.rect().center()
        self.bar.setValue(control.mapTo(self.scroll.widget(), center).y() - self.scroll.viewport().height() // 2)
        self.application.processEvents()
        self.touch_drag(control.mapTo(self.dialog, center), -60)
        self.scroll.ensureWidgetVisible(control)
        changed = QSignalSpy(control.toggled)
        for source in (Qt.MouseEventSource.MouseEventSynthesizedBySystem,
                       Qt.MouseEventSource.MouseEventSynthesizedByQt):
            self.send_mouse_click(control, center, source)
        self.assertEqual(changed.count(), 0)
        before = self.bar.value()
        self.wheel(self.scroll.viewport(), pixels=(0, -15), angle=(0, 0))
        self.assertEqual(self.bar.value(), before + 15)
        self.send_mouse_click(control, center, Qt.MouseEventSource.MouseEventNotSynthesized)
        self.assertEqual(changed.count(), 1)
        other = PillSwitch('Other window')
        other.show()
        try:
            self.send_mouse_click(other, other.rect().center(), Qt.MouseEventSource.MouseEventSynthesizedBySystem)
            self.assertTrue(other.isChecked())
        finally:
            other.close()
            other.deleteLater()
        combo = self.dialog.mode
        self.scroll.ensureWidgetVisible(combo)
        combo.showPopup()
        self.application.processEvents()
        index = combo.model().index(1, 0)
        self.send_mouse_click(combo.view().viewport(), combo.view().visualRect(index).center(),
                              Qt.MouseEventSource.MouseEventSynthesizedByQt)
        self.assertEqual(combo.currentIndex(), 1)
        self.assertFalse(combo.view().isVisible())

    def test_touch_tap_opens_dropdown_on_release_and_popup_keeps_native_touch_selection(self):
        combo = self.dialog.mode
        self.scroll.ensureWidgetVisible(combo)
        self.application.processEvents()
        target = self.dialog.windowHandle()
        point = combo.mapTo(self.dialog, combo.rect().center())
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        self.assertFalse(combo.view().isVisible())
        touch.release(0, point, target).commit()
        self.application.processEvents()
        self.assertTrue(combo.view().isVisible())
        view = combo.view()
        # Keep the actual popup QWidget alive while using its native handle.
        popup = view.window()
        target = popup.windowHandle()
        point = view.viewport().mapTo(popup, view.visualRect(view.model().index(1, 0)).center())
        popup_touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        popup_touch.press(0, point, target).commit()
        popup_touch.release(0, point, target).commit()
        self.application.processEvents()
        self.assertEqual(combo.currentIndex(), 1)
        self.assertFalse(combo.view().isVisible())

    def test_touch_tap_opens_fullscreen_selector_once_without_blocking_release_replay(self):
        selector = self.dialog.fullscreen_exit_gestures
        self.scroll.ensureWidgetVisible(selector)
        self.application.processEvents()
        shown = QSignalSpy(selector.menu.aboutToShow)
        changed = QSignalSpy(selector.selectionChanged)
        watchdog_fired = []
        watchdog = QTimer(self.dialog)
        watchdog.setSingleShot(True)
        def recover_nested_menu():
            watchdog_fired.append({'menu_visible': selector.menu.isVisible(),
                                   'replaying_touch_click': self.scroll._replaying_touch_click})
            selector.menu.close()
        watchdog.timeout.connect(recover_nested_menu)
        target = self.dialog.windowHandle()
        point = selector.mapTo(self.dialog, selector.rect().center())
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        self.assertFalse(selector.menu.isVisible())
        self.assertEqual(len(self.scroll._touch_contacts), 1)
        try:
            watchdog.start(250)
            touch.release(0, point, target).commit()
            watchdog.stop()
            self.application.processEvents()
            self.assertEqual(watchdog_fired, [], 'Touch release blocked inside menu mouse replay')
            self.assertTrue(selector.menu.isVisible(), 'One completed finger tap must leave the menu open')
            self.assertEqual(shown.count(), 1)
            self.assertEqual(changed.count(), 0)
            self.assertEqual(self.scroll._touch_contacts, set())
            self.assertFalse(self.scroll._replaying_touch_click)
        finally:
            watchdog.stop()
            selector.menu.close()

    def test_fullscreen_selector_promoted_original_touch_tail_does_not_close_popup(self):
        selector = self.dialog.fullscreen_exit_gestures
        self.scroll.ensureWidgetVisible(selector)
        self.application.processEvents()
        target = self.dialog.windowHandle()
        center = selector.rect().center()
        point = selector.mapTo(self.dialog, center)
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        shown = QSignalSpy(selector.menu.aboutToShow)
        changed = QSignalSpy(selector.selectionChanged)
        try:
            touch.press(0, point, target).commit()
            touch.release(0, point, target).commit()
            self.application.processEvents()
            self.assertTrue(selector.menu.isVisible())
            self.assertEqual(shown.count(), 1)
            # After a popup opens, Qt can route the promoted mouse tail to
            # that popup rather than the original settings-page child.
            global_position = selector.mapToGlobal(center)
            for source in (Qt.MouseEventSource.MouseEventSynthesizedBySystem,
                           Qt.MouseEventSource.MouseEventSynthesizedByQt):
                self.send_mouse_click(selector.menu, selector.menu.mapFromGlobal(global_position), source)
                self.assertTrue(selector.menu.isVisible(), 'Original finger tail closed the newly opened menu')
                self.assertEqual(shown.count(), 1)
                self.assertEqual(changed.count(), 0)
            QTest.keyClick(selector.menu, Qt.Key.Key_Escape)
            self.assertFalse(selector.menu.isVisible())
            self.send_mouse_click(selector, center, Qt.MouseEventSource.MouseEventSynthesizedByQt)
            self.assertFalse(selector.menu.isVisible(), 'Late touch tail reopened the dismissed menu')
        finally:
            selector.menu.close()

    def test_fullscreen_selector_window_routed_tail_is_guarded_but_next_menu_touch_is_native(self):
        selector = self.dialog.fullscreen_exit_gestures
        self.scroll.ensureWidgetVisible(selector)
        self.application.processEvents()
        shown = QSignalSpy(selector.menu.aboutToShow)
        changed = QSignalSpy(selector.selectionChanged)
        target = self.dialog.windowHandle()
        point = selector.mapTo(self.dialog, selector.rect().center())
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.release(0, point, target).commit()
        try:
            self.assertTrue(selector.menu.isVisible())
            # Exercise QWidgetWindow's popup retargeting too, rather than
            # only sendEvent directly to the QMenu widget.
            global_position = QPointF(selector.mapToGlobal(selector.rect().center()))
            for window in (target, selector.menu.windowHandle()):
                for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                             QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease):
                    event = QMouseEvent(kind, window.mapFromGlobal(global_position),
                        window.mapFromGlobal(global_position), global_position,
                        Qt.MouseButton.LeftButton,
                        Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease
                        else Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventSynthesizedBySystem)
                    QApplication.sendEvent(window, event)
                    self.assertTrue(selector.menu.isVisible())
            checkbox = selector.checkboxes['top']
            checked = checkbox.isChecked()
            popup_window = selector.menu.windowHandle()
            point = checkbox.mapTo(selector.menu, checkbox.rect().center())
            menu_touch = QTest.touchEvent(popup_window, QTest.createTouchDevice(), False)
            menu_touch.press(0, point, popup_window).commit()
            menu_touch.release(0, point, popup_window).commit()
            self.application.processEvents()
            self.assertEqual(checkbox.isChecked(), not checked)
            self.assertEqual((shown.count(), changed.count()), (1, 1))
            self.assertTrue(selector.menu.isVisible())
            self.assertIsNone(self.scroll._touch_popup)
        finally:
            selector.menu.close()

    def test_fullscreen_selector_drag_and_cancel_never_open_menu_and_next_tap_opens_once(self):
        selector = self.dialog.fullscreen_exit_gestures
        center = selector.rect().center()
        self.bar.setValue(selector.mapTo(self.scroll.widget(), center).y() - self.scroll.viewport().height() // 2)
        self.application.processEvents()
        shown = QSignalSpy(selector.menu.aboutToShow)
        changed = QSignalSpy(selector.selectionChanged)
        before = self.bar.value()
        during = self.touch_drag(selector.mapTo(self.dialog, center), -80)
        self.assertGreater(during, before)
        self.assertFalse(selector.menu.isVisible())
        self.assertEqual((shown.count(), changed.count()), (0, 0))
        self.scroll.ensureWidgetVisible(selector)
        self.application.processEvents()
        self.send_mouse_click(selector, center, Qt.MouseEventSource.MouseEventSynthesizedBySystem)
        self.assertFalse(selector.menu.isVisible())
        point = selector.mapTo(self.scroll.viewport(), center)
        device = QTest.createTouchDevice()
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(1, QEventPoint.State.Pressed, point)])
        self.raw_touch(device, QEvent.Type.TouchCancel, [])
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(1, QEventPoint.State.Released, point)])
        self.assertEqual((shown.count(), changed.count()), (0, 0))
        target = self.dialog.windowHandle()
        point = selector.mapTo(self.dialog, center)
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.release(0, point, target).commit()
        try:
            self.assertTrue(selector.menu.isVisible())
            self.assertEqual((shown.count(), changed.count()), (1, 0))
        finally:
            selector.menu.close()

    def test_fullscreen_selector_tail_guard_preserves_physical_mouse_keyboard_and_other_popup(self):
        selector = self.dialog.fullscreen_exit_gestures
        self.scroll.ensureWidgetVisible(selector)
        self.application.processEvents()
        target = self.dialog.windowHandle()
        point = selector.mapTo(self.dialog, selector.rect().center())
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.release(0, point, target).commit()
        self.assertTrue(selector.menu.isVisible())
        checkbox = selector.checkboxes['top']
        changed = QSignalSpy(selector.selectionChanged)
        checked = checkbox.isChecked()
        try:
            self.send_mouse_click(checkbox, checkbox.rect().center(),
                                  Qt.MouseEventSource.MouseEventNotSynthesized)
            self.assertEqual((checkbox.isChecked(), changed.count()), (not checked, 1))
            self.assertTrue(selector.menu.isVisible())
            self.assertIsNone(self.scroll._touch_popup)
            selector.menu.setActiveAction(selector.actions_by_gesture['top'])
            QTest.keyClick(selector.menu, Qt.Key.Key_Space)
            self.assertEqual((checkbox.isChecked(), changed.count()), (checked, 2))
            QTest.keyClick(selector.menu, Qt.Key.Key_Escape)
            self.assertFalse(selector.menu.isVisible())
            # A second popup is never covered by the selector's old tail.
            combo = self.dialog.mode
            self.scroll.ensureWidgetVisible(combo)
            combo.showPopup()
            self.application.processEvents()
            index = combo.model().index(1, 0)
            self.send_mouse_click(combo.view().viewport(), combo.view().visualRect(index).center(),
                                  Qt.MouseEventSource.MouseEventSynthesizedByQt)
            self.assertEqual(combo.currentIndex(), 1)
            self.assertFalse(combo.view().isVisible())
        finally:
            selector.menu.close()

    def test_fullscreen_selector_tail_guard_expires_and_releases_popup_when_page_hides(self):
        selector = self.dialog.fullscreen_exit_gestures
        self.scroll.ensureWidgetVisible(selector)
        self.application.processEvents()
        target = self.dialog.windowHandle()
        point = selector.mapTo(self.dialog, selector.rect().center())
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        with patch('livegallery.settings_controls.time.monotonic', return_value=100):
            touch.press(0, point, target).commit()
            touch.release(0, point, target).commit()
        self.assertTrue(selector.menu.isVisible())
        self.assertIs(self.scroll._touch_popup(), selector.menu)
        # The bound remains the existing 0.8-second mouse-tail window.
        with patch('livegallery.settings_controls.time.monotonic', return_value=100.81):
            self.send_mouse_click(selector.menu,
                selector.menu.mapFromGlobal(selector.mapToGlobal(selector.rect().center())),
                Qt.MouseEventSource.MouseEventSynthesizedBySystem)
        self.assertFalse(selector.menu.isVisible())
        self.assertIsNone(self.scroll._touch_popup)
        target = self.dialog.windowHandle()
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.release(0, point, target).commit()
        self.assertTrue(selector.menu.isVisible())
        self.dialog.hide()
        self.assertIsNone(self.scroll._touch_popup)
        self.assertIsNone(self.scroll._touch_popup_origin)
        self.assertFalse(self.scroll._touch_filter_installed)
        selector.menu.close()

    def test_touch_spin_drag_does_not_edit_and_disabled_switch_does_not_activate(self):
        spin = self.dialog.hold
        editor = spin.lineEdit()
        center = editor.rect().center()
        self.bar.setValue(editor.mapTo(self.scroll.widget(), center).y() - self.scroll.viewport().height() // 2)
        self.application.processEvents()
        before, value = self.bar.value(), spin.value()
        during = self.touch_drag(editor.mapTo(self.dialog, center), -70)
        self.assertEqual(during, before + 70)
        self.assertEqual(spin.value(), value)
        control = self.dialog.autoplay
        self.scroll.ensureWidgetVisible(control)
        control.setEnabled(False)
        self.application.processEvents()
        before = control.isChecked()
        changed = QSignalSpy(control.toggled)
        point = control.mapTo(self.scroll.viewport(), control.rect().center())
        device = QTest.createTouchDevice()
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(4, QEventPoint.State.Pressed, point)])
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(4, QEventPoint.State.Released, point)])
        self.assertEqual((control.isChecked(), changed.count()), (before, 0))

    def test_foreign_touch_cancel_does_not_interrupt_owned_settings_drag(self):
        self.bar.setValue(self.bar.maximum() // 2)
        self.application.processEvents()
        before = self.bar.value()
        target = self.dialog.windowHandle()
        point = self.scroll.viewport().mapTo(self.dialog, QPoint(6, self.scroll.viewport().height() // 2))
        owner = QTest.createTouchDevice()
        touch = QTest.touchEvent(target, owner, False)
        touch.press(0, point, target).commit()
        self.raw_touch(QTest.createTouchDevice(), QEvent.Type.TouchCancel, [])
        touch.move(0, point - QPoint(0, 70), target).commit()
        self.assertEqual(self.bar.value(), before + 70)
        touch.release(0, point - QPoint(0, 70), target).commit()

    def test_window_deactivation_cancels_pending_touch_click_until_release(self):
        control = self.dialog.autoplay
        self.scroll.ensureWidgetVisible(control)
        self.application.processEvents()
        changed = QSignalSpy(control.toggled)
        target = self.dialog.windowHandle()
        point = control.mapTo(self.dialog, control.rect().center())
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        QApplication.sendEvent(self.dialog, QEvent(QEvent.Type.WindowDeactivate))
        touch.release(0, point, target).commit()
        self.assertEqual(changed.count(), 0)

    def test_modal_popup_cancels_pending_touch_and_next_contact_remains_usable(self):
        control = self.dialog.autoplay
        self.scroll.ensureWidgetVisible(control)
        self.application.processEvents()
        changed = QSignalSpy(control.toggled)
        point = control.mapTo(self.scroll.viewport(), control.rect().center())
        device = QTest.createTouchDevice()
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(7, QEventPoint.State.Pressed, point)])
        modal = QDialog(self.dialog)
        modal.setModal(True)
        modal.show()
        try:
            self.application.processEvents()
            self.raw_touch(device, QEvent.Type.TouchUpdate, [(7, QEventPoint.State.Released, point)])
            self.assertEqual(changed.count(), 0)
        finally:
            modal.close()
            modal.deleteLater()
            self.application.processEvents()
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(8, QEventPoint.State.Pressed, point)])
        self.raw_touch(device, QEvent.Type.TouchUpdate, [(8, QEventPoint.State.Released, point)])
        self.assertEqual(changed.count(), 1)

    def test_page_application_filter_only_lives_while_shown_and_reopens_with_touch(self):
        probe = QObject()
        deliveries = []

        class ObservedScrollArea(SettingsScrollArea):
            def _filter_touch_event(area, receiver, event):
                if receiver is probe and event.type() == QEvent.Type.User:
                    deliveries.append(True)
                return super()._filter_touch_event(receiver, event)

        with patch('livegallery.app.SettingsScrollArea', ObservedScrollArea):
            dialog = SettingsDialog(Preferences())
        dialog.resize(680, 480)
        try:
            QApplication.sendEvent(probe, QEvent(QEvent.Type.User))
            self.assertEqual(len(deliveries), 0, 'Unshown page installed an application filter')
            for cycle in range(3):
                dialog.show()
                dialog.show()
                self.application.processEvents()
                QApplication.sendEvent(probe, QEvent(QEvent.Type.User))
                self.assertEqual(len(deliveries), cycle + 1)
                control = dialog.autoplay
                dialog.scroll_area.ensureWidgetVisible(control)
                self.application.processEvents()
                toggles = QSignalSpy(control.toggled)
                clicks = QSignalSpy(control.clicked)
                target = dialog.windowHandle()
                point = control.mapTo(dialog, control.rect().center())
                touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
                touch.press(0, point, target).commit()
                touch.release(0, point, target).commit()
                self.assertEqual((toggles.count(), clicks.count()), (1, 1))
                dialog.hide()
                QApplication.sendEvent(probe, QEvent(QEvent.Type.User))
                self.assertEqual(len(deliveries), cycle + 1, 'Hidden page retained its application filter')
            dialog.show()
            self.application.processEvents()
            QApplication.sendEvent(probe, QEvent(QEvent.Type.User))
            self.assertEqual(len(deliveries), 4)
            observer = dialog.scroll_area._touch_event_filter
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
            self.assertFalse(isValid(dialog))
            self.assertFalse(isValid(observer))
            QApplication.sendEvent(probe, QEvent(QEvent.Type.User))
            self.assertEqual(len(deliveries), 4)
        finally:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
            self.application.processEvents()

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

    def test_source_settings_check_waits_for_actual_delayed_popup_before_selection(self):
        from livegallery.selftest import _check_settings_controls

        combo = self.dialog.mode
        original_popup = combo.showPopup
        timer = QTimer(self.dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: original_popup())
        opened = []

        def delayed_popup():
            self.assertFalse(combo.view().isVisible())
            opened.append(True)
            timer.start(50)

        filters = MediaTypeFilter()
        try:
            with patch.object(combo, 'showPopup', side_effect=delayed_popup):
                result = _check_settings_controls(
                    self.dialog, SimpleNamespace(kind_filter=filters), self.base)
            self.assertEqual(opened, [True])
            self.assertTrue(result['opened_dropdown_selectable'])
            self.assertEqual(result['closed_control_wheel_events'], 20)
            self.assertEqual(result['touch_toggle_count'], 1)
            self.assertEqual(result['touch_click_count'], 1)
        finally:
            timer.stop()
            filters.deleteLater()

    def test_fullscreen_exit_setting_only_offers_cancellable_screen_top_swipe(self):
        selector = self.dialog.fullscreen_exit_gestures
        self.assertEqual(selector.selected_gestures(), ['top'])
        self.assertEqual(set(selector.checkboxes), {'top'})
        self.assertEqual(selector.checkboxes['top'].text(), '屏幕上沿下滑')
        self.assertEqual(len(selector.menu.actions()), 1)
        changes = QSignalSpy(selector.selectionChanged)
        selector.checkboxes['top'].click()
        self.assertEqual(selector.selected_gestures(), [])
        selector.checkboxes['top'].click()
        self.assertEqual(selector.selected_gestures(), ['top'])
        self.assertEqual([changes.at(index)[0] for index in range(changes.count())], [[], ['top']])

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
