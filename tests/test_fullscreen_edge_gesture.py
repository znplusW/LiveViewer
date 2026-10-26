"""Screen-edge reservation with real Qt-window touch injection."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QContextMenuEvent, QEventPoint, QMouseEvent, QPixmap, QTouchEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QPushButton, QScrollBar, QStyle, QStyleOptionSlider, QWidget

from livegallery.fullscreen_edge_gesture import FullscreenEdgeGesture
from livegallery.zoom_navigator import ZoomNavigator
from livegallery.touch_filmstrip import FilmstripResizeHandle


class TouchTarget(QWidget):
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.touch_events = []
        self.mouse_events = []
        self.contexts = 0

    def event(self, event):
        if event.type() in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                            QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            self.touch_events.append(event.type())
            event.accept()
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        self.mouse_events.append(('press', event.source()))
        event.accept()

    def mouseReleaseEvent(self, event):
        self.mouse_events.append(('release', event.source()))
        event.accept()

    def contextMenuEvent(self, event):
        self.contexts += 1
        event.accept()


class FullscreenEdgeGestureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = TouchTarget()
        self.window.showFullScreen()
        self.window.activateWindow()
        QCoreApplication.processEvents()
        self.other = TouchTarget()
        self.other.resize(300, 250)
        self.other.show()
        self.window.activateWindow()
        QCoreApplication.processEvents()
        self.cancelled = []
        self.windows = [self.window, self.other]
        self.filter = FullscreenEdgeGesture(lambda: self.windows,
                                             enabled_edges=('top',),
                                             cancel_active=lambda: self.cancelled.append(True))
        self.escape = QSignalSpy(self.filter.escapeRequested)
        self.target = self.window.windowHandle()
        self.device = QTest.createTouchDevice()
        self.touch = QTest.touchEvent(self.target, self.device, False)
        self.start = QPoint(self.window.width() // 2, 4)

    def tearDown(self):
        self.filter.close()
        for window in self.windows:
            from shiboken6 import isValid
            if isValid(window):
                window.close()
                window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_native_down_swipe_exits_once_and_release_never_reaches_normal_content(self):
        self.filter.escapeRequested.connect(lambda window: window.showNormal())
        self.touch.press(0, self.start, self.target).commit()
        self.assertEqual(len(self.cancelled), 1)
        self.touch.move(0, self.start + QPoint(4, 100), self.target).commit()
        self.assertEqual(self.escape.count(), 1)
        self.assertIs(self.escape.at(0)[0], self.window)
        self.assertFalse(self.window.isFullScreen())
        self.touch.move(0, self.start + QPoint(5, 140), self.target).commit()
        self.touch.release(0, self.start + QPoint(5, 140), self.target).commit()
        self.assertEqual(self.escape.count(), 1)
        self.assertEqual(self.window.touch_events, [])
        self.assertEqual(self.window.mouse_events, [])

    def test_non_edge_and_non_fullscreen_sequences_are_untouched(self):
        point = self.start + QPoint(0, 40)
        self.touch.press(0, point, self.target).commit()
        self.touch.move(0, point + QPoint(0, 120), self.target).commit()
        self.touch.release(0, point + QPoint(0, 120), self.target).commit()
        self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)
        self.assertEqual(self.escape.count(), 0)
        self.assertFalse(self.cancelled)
        self.window.showNormal()
        QCoreApplication.processEvents()
        self.touch.press(0, self.start, self.target).commit()
        self.touch.move(0, self.start + QPoint(0, 120), self.target).commit()
        self.touch.release(0, self.start + QPoint(0, 120), self.target).commit()
        self.assertEqual(self.escape.count(), 0)
        self.assertFalse(self.cancelled)

    def test_removed_left_and_right_swipes_leave_normal_content_available(self):
        # The user removed side-edge exit. Historical left-edge hardware
        # traces remain evidence of the old behavior, not the current goal.
        self.filter.set_enabled_edges({'top', 'left', 'right'})
        self.assertEqual(self.filter.enabled_edges, frozenset({'top'}))
        for start, delta in ((QPoint(34, 170), QPoint(120, 2)),
                             (QPoint(self.window.width() - 34, 170), QPoint(-120, 2))):
            with self.subTest(start=start):
                self.window.touch_events.clear()
                self.touch.press(0, start, self.target).commit()
                self.touch.move(0, start + delta, self.target).commit()
                self.touch.release(0, start + delta, self.target).commit()
                self.assertEqual(self.escape.count(), 0)
                self.assertFalse(self.cancelled)
                self.assertIsNone(self.filter._target)
                self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)
                self.assertIn(QEvent.Type.TouchEnd, self.window.touch_events)

    def test_touch_band_boundary_and_new_band_rejection_guards(self):
        self.filter.set_enabled_edges({'top'})
        for y, should_exit in ((40, True), (41, False)):
            with self.subTest(y=y):
                count = self.escape.count()
                start = QPoint(170, y)
                self.touch.press(0, start, self.target).commit()
                self.touch.move(0, start + QPoint(0, 100), self.target).commit()
                self.touch.release(0, start + QPoint(0, 100), self.target).commit()
                self.assertEqual(self.escape.count() - count, int(should_exit))
        self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)
        self.window.touch_events.clear()
        start = QPoint(170, 34)
        for delta in (QPoint(100, 0), QPoint(0, -34), QPoint(0, 70)):
            with self.subTest(delta=delta):
                count = self.escape.count()
                self.touch.press(0, start, self.target).commit()
                self.touch.move(0, start + delta, self.target).commit()
                self.touch.release(0, start + delta, self.target).commit()
                self.assertEqual(self.escape.count(), count)
                self.assertEqual(self.window.touch_events, [])
                self.assertEqual(self.window.mouse_events, [])

    def test_wrong_direction_short_swipe_and_cancel_do_not_replay_half_contact(self):
        for delta in (QPoint(100, 4), QPoint(2, -100), QPoint(0, 50)):
            with self.subTest(delta=delta):
                self.touch.press(0, self.start, self.target).commit()
                self.touch.move(0, self.start + delta, self.target).commit()
                self.touch.release(0, self.start + delta, self.target).commit()
                self.assertEqual(self.escape.count(), 0)
                self.assertEqual(self.window.touch_events, [])
        self.touch.press(0, self.start, self.target).commit()
        QApplication.sendEvent(self.target, QTouchEvent(QEvent.Type.TouchCancel, self.device))
        self.touch.release(0, self.start + QPoint(0, 100), self.target).commit()
        self.assertEqual(self.escape.count(), 0)
        self.assertEqual(self.window.touch_events, [])

    def test_expanded_touch_band_keeps_controls_and_initial_two_fingers_available(self):
        self.filter.set_enabled_edges({'top'})
        # A plain parent lets Qt promote touch for QAbstractButton; the
        # TouchTarget fixture deliberately consumes touch before promotion.
        window = QWidget()
        self.windows.append(window)
        button = QPushButton('边缘控件', window)
        button.setGeometry(100, 24, 100, 40)
        window.showFullScreen()
        window.activateWindow()
        QCoreApplication.processEvents()
        target = window.windowHandle()
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        clicked = QSignalSpy(button.clicked)
        point = QPoint(120, 34)
        touch.press(0, point, target).commit()
        touch.release(0, point, target).commit()
        QCoreApplication.processEvents()
        self.assertEqual(clicked.count(), 1)
        self.assertFalse(self.cancelled)
        self.window.activateWindow()
        QCoreApplication.processEvents()
        point = QPoint(170, 34)
        self.touch.press(0, point, self.target).press(1, point + QPoint(50, 0), self.target).commit()
        self.touch.release(0, point, self.target).release(1, point + QPoint(50, 0), self.target).commit()
        self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)
        self.assertFalse(self.cancelled)
        self.assertEqual(self.escape.count(), 0)

    def test_second_finger_cancels_claim_and_initial_two_fingers_never_claim(self):
        self.touch.press(0, self.start, self.target).commit()
        self.touch.stationary(0).press(1, self.start + QPoint(50, 0), self.target).commit()
        self.touch.move(0, self.start + QPoint(0, 120), self.target).move(1, self.start + QPoint(50, 120), self.target).commit()
        self.touch.release(0, self.start + QPoint(0, 120), self.target).release(1, self.start + QPoint(50, 120), self.target).commit()
        self.assertEqual(self.escape.count(), 0)
        self.assertEqual(self.window.touch_events, [])
        count = len(self.cancelled)
        self.touch.press(0, self.start, self.target).press(1, self.start + QPoint(50, 0), self.target).commit()
        self.touch.release(0, self.start, self.target).release(1, self.start + QPoint(50, 0), self.target).commit()
        self.assertEqual(len(self.cancelled), count)
        self.assertEqual(self.escape.count(), 0)

    def test_pending_edge_blocks_other_group_touch_until_its_own_end(self):
        self.touch.press(0, self.start, self.target).commit()
        other_target = self.other.windowHandle()
        other = QTest.touchEvent(other_target, QTest.createTouchDevice(), False)
        other.press(1, QPoint(80, 80), other_target).commit()
        self.touch.release(0, self.start, self.target).commit()
        other.move(1, QPoint(100, 100), other_target).commit()
        other.release(1, QPoint(100, 100), other_target).commit()
        self.assertEqual(self.other.touch_events, [])
        self.assertEqual(self.escape.count(), 0)
        other.press(1, QPoint(80, 80), other_target).commit()
        other.release(1, QPoint(80, 80), other_target).commit()
        self.assertIn(QEvent.Type.TouchBegin, self.other.touch_events)

    def test_late_synthetic_mouse_and_context_are_suppressed_real_mouse_is_unchanged(self):
        self.touch.press(0, self.start, self.target).commit()
        self.touch.release(0, self.start, self.target).commit()
        point = QPointF(self.start)
        for source in (Qt.MouseEventSource.MouseEventSynthesizedByQt,
                       Qt.MouseEventSource.MouseEventSynthesizedBySystem):
            for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                event = QMouseEvent(kind, point, point, QPointF(self.window.mapToGlobal(self.start)),
                                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier, source)
                QApplication.sendEvent(self.window, event)
        QApplication.sendEvent(self.window, QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                                            self.start, self.window.mapToGlobal(self.start)))
        self.assertEqual(self.window.mouse_events, [])
        self.assertEqual(self.window.contexts, 0)
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, pos=self.start)
        self.assertEqual(len(self.window.mouse_events), 2)
        QTest.mousePress(self.window, Qt.MouseButton.RightButton, pos=self.start)
        QApplication.sendEvent(self.window, QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                                            self.start, self.window.mapToGlobal(self.start)))
        QTest.mouseRelease(self.window, Qt.MouseButton.RightButton, pos=self.start)
        self.assertEqual(self.window.contexts, 1)

    def test_top_button_touch_click_remains_available(self):
        window = QWidget()
        self.windows.append(window)
        button = QPushButton('顶部按钮', window)
        button.setGeometry(50, 0, 100, 35)
        window.showFullScreen()
        window.activateWindow()
        QCoreApplication.processEvents()
        target = window.windowHandle()
        sequence = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        clicked = QSignalSpy(button.clicked)
        sequence.press(0, QPoint(100, 4), target).commit()
        sequence.release(0, QPoint(100, 4), target).commit()
        QCoreApplication.processEvents()
        self.assertEqual(clicked.count(), 1)
        self.assertFalse(self.cancelled)
        self.assertEqual(self.escape.count(), 0)

    def test_resize_dpi_external_deactivation_and_window_close_cancel_pending(self):
        for kind in (QEvent.Type.DevicePixelRatioChange, QEvent.Type.ApplicationDeactivate):
            with self.subTest(kind=kind):
                self.touch.press(0, self.start, self.target).commit()
                QApplication.sendEvent(self.window, QEvent(kind))
                self.touch.move(0, self.start + QPoint(0, 120), self.target).commit()
                self.touch.release(0, self.start + QPoint(0, 120), self.target).commit()
                self.assertEqual(self.escape.count(), 0)
        self.touch.press(0, self.start, self.target).commit()
        self.window.close()
        self.touch.release(0, self.start + QPoint(0, 120), self.target).commit()
        self.assertEqual(self.escape.count(), 0)

    def test_deleted_window_and_filter_close_leave_no_stuck_owner_or_cpp_access(self):
        self.touch.press(0, self.start, self.target).commit()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(self.filter._target)
        self.other.showFullScreen()
        QCoreApplication.processEvents()
        target = self.other.windowHandle()
        sequence = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        point = QPoint(self.other.width() // 2, 4)
        sequence.press(1, point, target).commit()
        sequence.move(1, point + QPoint(0, 120), target).commit()
        sequence.release(1, point + QPoint(0, 120), target).commit()
        self.assertEqual(self.escape.count(), 1)
        self.filter.close()
        self.filter.close()
        sequence.press(1, point, target).commit()
        sequence.release(1, point, target).commit()
        self.assertIn(QEvent.Type.TouchBegin, self.other.touch_events)

    def test_destroyed_preview_tail_is_consumed_in_surviving_window(self):
        self.touch.press(0, self.start, self.target).commit()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(self.filter._target)
        self.assertEqual(len(self.filter._retired), 1)
        point = QPointF(120, 120)
        global_point = QPointF(self.other.mapToGlobal(point.toPoint()))
        for kind, state in ((QEvent.Type.TouchUpdate, QEventPoint.State.Updated),
                            (QEvent.Type.TouchEnd, QEventPoint.State.Released)):
            tail = QTouchEvent(kind, self.device, Qt.KeyboardModifier.NoModifier,
                               [QEventPoint(0, state, point, global_point)])
            # Qt discards manually sent updates without a surviving native
            # begin target; exercise the filter's late-delivery boundary here.
            self.assertTrue(self.filter.eventFilter(self.other, tail))
        self.assertEqual(self.other.touch_events, [])
        self.assertEqual(self.filter._retired, [])
        self.assertEqual(self.escape.count(), 0)
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            mouse = QMouseEvent(kind, point, point, global_point,
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier,
                                Qt.MouseEventSource.MouseEventSynthesizedBySystem)
            QApplication.sendEvent(self.other, mouse)
        self.assertEqual(self.other.mouse_events, [])

    def test_destroyed_preview_missing_end_does_not_block_new_touch_sequence(self):
        self.touch.press(0, self.start, self.target).commit()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        point = QPointF(120, 120)
        global_point = QPointF(self.other.mapToGlobal(point.toPoint()))
        event = QTouchEvent(QEvent.Type.TouchBegin, self.device, Qt.KeyboardModifier.NoModifier,
                            [QEventPoint(0, QEventPoint.State.Pressed, point, global_point)])
        QApplication.sendEvent(self.other, event)
        self.assertEqual(self.filter._retired, [])
        self.assertIn(QEvent.Type.TouchBegin, self.other.touch_events)

    def _raw_points(self, window, kind, contacts):
        points = [QEventPoint(identifier, state, QPointF(position), window.mapToGlobal(QPointF(position)))
                  for identifier, state, position in contacts]
        event = QTouchEvent(kind, self.device, Qt.KeyboardModifier.NoModifier, points)
        return self.filter.eventFilter(window.windowHandle(), event)

    def _retire_preview_contact(self):
        self.touch.press(0, self.start, self.target).commit()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(self.filter._target)
        self.assertEqual(self.filter._retired[0][1], {0})
        self.other.showFullScreen()
        QCoreApplication.processEvents()

    def test_destroyed_preview_missing_end_recovers_new_pressed_update_and_keeps_old_tail(self):
        # Windows hardware session 05: preview point0 lost its End after HWND
        # destruction; the next physical DOWN arrived as TouchUpdate+Pressed.
        self._retire_preview_contact()
        point = QPointF(160, 34)
        for identifier in (1, 19):  # IDs are contact identities, not a sequence.
            self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
                [(identifier, QEventPoint.State.Pressed, point)]))
            self.assertIsNotNone(self.filter._target)
            self.assertIs(self.filter._target(), self.other)
            self.assertEqual(self.filter._contacts, {identifier})
            self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
                [(identifier, QEventPoint.State.Updated, point + QPointF(1, 100))]))
            self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
                [(identifier, QEventPoint.State.Released, point + QPointF(2, 130))]))
            self.assertIsNone(self.filter._target)
            self.assertEqual(self.filter._retired[0][1], {0})
        self.assertEqual(self.escape.count(), 2)
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Updated, QPointF(120, 120))]))
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Released, QPointF(120, 120))]))
        self.assertEqual(self.filter._retired, [])
        self.assertEqual(self.other.touch_events, [])
        self.assertEqual(self.other.mouse_events, [])

    def test_retired_old_and_new_contact_in_same_frame_remain_blocked_as_multitouch(self):
        self._retire_preview_contact()
        point = QPointF(160, 34)
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Stationary, QPointF(120, 120)),
             (1, QEventPoint.State.Pressed, point)]))
        self.assertIsNone(self.filter._target)
        self.assertEqual(self.filter._retired[0][1], {0, 1})
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Released, QPointF(120, 120)),
             (1, QEventPoint.State.Released, point + QPointF(0, 120))]))
        self.assertEqual(self.filter._retired, [])
        self.assertEqual(self.escape.count(), 0)
        self.assertEqual(self.other.touch_events, [])

    def test_retired_contact_needs_fresh_single_press_to_start_another_edge(self):
        self._retire_preview_contact()
        point = QPointF(160, 34)
        # An unknown movement is not evidence of a new press, even at the top.
        self.assertFalse(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(9, QEventPoint.State.Updated, point)]))
        self.assertFalse(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(9, QEventPoint.State.Updated, point + QPointF(0, 140))]))
        self.assertIsNone(self.filter._target)
        self.assertEqual(self.filter._retired[0][1], {0})
        self.assertEqual(self.escape.count(), 0)
        # Two fresh presses cannot become a one-finger edge gesture either.
        self.assertFalse(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Pressed, point),
             (19, QEventPoint.State.Pressed, point + QPointF(50, 0))]))
        self.assertIsNone(self.filter._target)
        self.assertEqual(self.filter._retired[0][1], {0})
        # Neither case released or reinterpreted the missing old contact.
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Updated, point + QPointF(0, 160))]))
        self.assertEqual(self.escape.count(), 0)
        self.assertEqual(self.other.touch_events, [])

    def test_consecutive_destroyed_previews_keep_all_unreleased_contact_ids(self):
        self._retire_preview_contact()
        point = QPointF(160, 34)
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Pressed, point)]))
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Updated, point + QPointF(0, 100))]))
        self.other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(self.filter._target)
        self.assertEqual(self.filter._retired[0][1], {0, 1})
        survivor = TouchTarget()
        self.windows.append(survivor)
        survivor.showFullScreen()
        QCoreApplication.processEvents()
        for identifier in (0, 1):
            self.assertTrue(self._raw_points(survivor, QEvent.Type.TouchUpdate,
                [(identifier, QEventPoint.State.Updated, QPointF(100, 100))]))
        self.assertEqual(survivor.touch_events, [])
        self.assertEqual(self.escape.count(), 1)
        self.assertTrue(self._raw_points(survivor, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Released, QPointF(100, 100))]))
        self.assertEqual(self.filter._retired[0][1], {1})
        self.assertTrue(self._raw_points(survivor, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Released, QPointF(100, 100))]))
        self.assertEqual(self.filter._retired, [])

    def test_fresh_unowned_press_restores_button_promotion_but_old_tail_rearms_guard(self):
        self._retire_preview_contact()
        button = QPushButton('普通新接触', self.other)
        button.setGeometry(120, 20, 100, 40)
        button.show()
        point = QPointF(160, 34)
        clicked = QSignalSpy(button.clicked)

        def mouse(kind):
            global_point = self.other.mapToGlobal(point)
            local = button.mapFromGlobal(global_point)
            event = QMouseEvent(kind, local, point, global_point,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventSynthesizedBySystem)
            QApplication.sendEvent(button, event)

        # An unknown update must not authorize ordinary promoted mouse input.
        self.assertFalse(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(9, QEventPoint.State.Updated, point)]))
        mouse(QEvent.Type.MouseButtonPress)
        mouse(QEvent.Type.MouseButtonRelease)
        self.assertEqual(clicked.count(), 0)
        # The new Pressed ID is independent of retired point0 and over a
        # genuine exempt control. Its release/promotion must remain usable.
        self.assertFalse(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Pressed, point)]))
        mouse(QEvent.Type.MouseButtonPress)
        self.assertFalse(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Released, point)]))
        mouse(QEvent.Type.MouseButtonRelease)
        self.assertEqual(clicked.count(), 1)
        self.assertEqual(self.filter._retired[0][1], {0})
        # No old physical Up was inferred: its late update is still consumed
        # and restores protection for its associated promoted mouse tail.
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Updated, QPointF(120, 120))]))
        mouse(QEvent.Type.MouseButtonPress)
        mouse(QEvent.Type.MouseButtonRelease)
        self.assertEqual(clicked.count(), 1)
        self.assertEqual(self.escape.count(), 0)

    def test_old_tail_cancels_recovered_owner_without_poisoning_its_contact_set(self):
        self._retire_preview_contact()
        point = QPointF(160, 34)
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Pressed, point)]))
        self.assertIsNotNone(self.filter._target)
        self.assertIs(self.filter._target(), self.other)
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Updated, QPointF(120, 120)),
             (1, QEventPoint.State.Stationary, point)]))
        self.assertTrue(self.filter._cancelled)
        self.assertEqual(self.filter._contacts, {1})
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(1, QEventPoint.State.Released, point + QPointF(0, 120))]))
        self.assertIsNone(self.filter._target)
        self.assertEqual(self.escape.count(), 0)
        self.assertEqual(self.filter._retired[0][1], {0})
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(9, QEventPoint.State.Pressed, point)]))
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(0, QEventPoint.State.Released, QPointF(120, 120))]))
        self.assertEqual(self.filter._contacts, {9})
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(9, QEventPoint.State.Updated, point + QPointF(0, 100))]))
        self.assertTrue(self._raw_points(self.other, QEvent.Type.TouchUpdate,
            [(9, QEventPoint.State.Released, point + QPointF(0, 130))]))
        self.assertEqual(self.escape.count(), 1)
        self.assertIsNone(self.filter._target)
        self.assertEqual(self.filter._retired, [])
        self.assertEqual(self.other.touch_events, [])

    def test_destroyed_preview_keeps_slow_contact_until_release_then_expires_mouse_tail(self):
        self.touch.press(0, self.start, self.target).commit()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        held_time = self.filter._tail_until + 2
        point = QPointF(120, 120)
        global_point = QPointF(self.other.mapToGlobal(point.toPoint()))
        event = QTouchEvent(QEvent.Type.TouchUpdate, self.device, Qt.KeyboardModifier.NoModifier,
                            [QEventPoint(0, QEventPoint.State.Updated, point, global_point)])

        def promoted_press_and_context():
            mouse = QMouseEvent(QEvent.Type.MouseButtonPress, point, point, global_point,
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier,
                                Qt.MouseEventSource.MouseEventSynthesizedBySystem)
            QApplication.sendEvent(self.other, mouse)
            QApplication.sendEvent(self.other, QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, point.toPoint(), global_point.toPoint()))

        with patch('livegallery.fullscreen_edge_gesture.time.monotonic', return_value=held_time):
            promoted_press_and_context()
            self.assertEqual(self.other.mouse_events, [])
            self.assertEqual(self.other.contexts, 0)
            self.assertTrue(self.filter.eventFilter(self.other, event))
            self.assertEqual(len(self.filter._retired), 1)
            release = QTouchEvent(QEvent.Type.TouchEnd, self.device, Qt.KeyboardModifier.NoModifier,
                                 [QEventPoint(0, QEventPoint.State.Released, point, global_point)])
            self.assertTrue(self.filter.eventFilter(self.other, release))
        self.assertEqual(self.filter._retired, [])
        with patch('livegallery.fullscreen_edge_gesture.time.monotonic', return_value=held_time + .5):
            promoted_press_and_context()
            self.assertEqual(self.other.mouse_events, [])
            self.assertEqual(self.other.contexts, 0)
        with patch('livegallery.fullscreen_edge_gesture.time.monotonic', return_value=held_time + 1):
            promoted_press_and_context()
            self.assertEqual(len(self.other.mouse_events), 1)
            self.assertEqual(self.other.contexts, 1)

    def test_destroyed_preview_slow_contact_cancel_releases_reservation(self):
        self.touch.press(0, self.start, self.target).commit()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        with patch('livegallery.fullscreen_edge_gesture.time.monotonic',
                   return_value=self.filter._tail_until + 2):
            cancel = QTouchEvent(QEvent.Type.TouchCancel, self.device)
            self.assertTrue(self.filter.eventFilter(self.other, cancel))
        self.assertEqual(self.filter._retired, [])

    def test_only_top_option_corner_directions_default_and_explicit_empty(self):
        default = FullscreenEdgeGesture(lambda: ())
        try:
            self.assertEqual(default.enabled_edges, frozenset({'top'}))
        finally:
            default.close()
        cases = [({'top'}, QPoint(160, 4), QPoint(2, 100)),
                 ({'top', 'left'}, QPoint(4, 4), QPoint(2, 100)),
                 ({'top', 'right'}, QPoint(self.window.width() - 4, 4), QPoint(-2, 100))]
        for index, (edges, start, delta) in enumerate(cases):
            with self.subTest(edges=edges, delta=delta):
                self.filter.set_enabled_edges(edges)
                self.assertEqual(self.filter.enabled_edges, frozenset({'top'}))
                self.touch.press(0, start, self.target).commit()
                self.touch.move(0, start + delta, self.target).commit()
                self.touch.release(0, start + delta, self.target).commit()
                self.assertEqual(self.escape.count(), index + 1)
        for edges in ({'left'}, {'right'}, {'left', 'right'}, set()):
            with self.subTest(disabled=edges):
                self.filter.set_enabled_edges(edges)
                self.assertEqual(self.filter.enabled_edges, frozenset())
                self.window.touch_events.clear()
                self.touch.press(0, self.start, self.target).commit()
                self.touch.move(0, self.start + QPoint(0, 100), self.target).commit()
                self.touch.release(0, self.start + QPoint(0, 100), self.target).commit()
                self.assertEqual(self.escape.count(), len(cases))
                self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)
        count = len(self.cancelled)
        self.filter.set_enabled_edges([])
        self.window.touch_events.clear()
        self.touch.press(0, self.start, self.target).commit()
        self.touch.move(0, self.start + QPoint(0, 120), self.target).commit()
        self.touch.release(0, self.start + QPoint(0, 120), self.target).commit()
        self.assertEqual(self.escape.count(), len(cases))
        self.assertEqual(len(self.cancelled), count)
        self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)

    def test_disabled_side_and_setting_change_leave_no_half_contact(self):
        self.filter.set_enabled_edges({'left'})
        self.touch.press(0, self.start, self.target).commit()
        self.touch.move(0, self.start + QPoint(0, 100), self.target).commit()
        self.touch.release(0, self.start + QPoint(0, 100), self.target).commit()
        self.assertFalse(self.cancelled)
        self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)
        self.window.touch_events.clear()
        self.filter.set_enabled_edges({'top'})
        self.touch.press(0, self.start, self.target).commit()
        self.filter.set_enabled_edges({'left'})
        self.touch.move(0, self.start + QPoint(0, 120), self.target).commit()
        self.touch.release(0, self.start + QPoint(0, 120), self.target).commit()
        self.assertEqual(self.escape.count(), 0)
        self.assertEqual(self.window.touch_events, [])

    def test_top_scrollbar_touch_drag_is_exempt_from_enabled_top_edge(self):
        window = QWidget()
        self.windows.append(window)
        bar = QScrollBar(Qt.Orientation.Horizontal, window)
        bar.setGeometry(40, 24, 300, 18)
        bar.setRange(0, 200)
        bar.setPageStep(30)
        bar.setValue(100)
        window.showFullScreen()
        window.activateWindow()
        QCoreApplication.processEvents()
        self.filter.set_enabled_edges({'top'})
        option = QStyleOptionSlider()
        bar.initStyleOption(option)
        thumb = bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                           QStyle.SubControl.SC_ScrollBarSlider, bar)
        point = bar.mapTo(window, thumb.center())
        target = window.windowHandle()
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.move(0, point + QPoint(70, 0), target).commit()
        touch.release(0, point + QPoint(70, 0), target).commit()
        self.assertGreater(bar.value(), 100)
        self.assertFalse(self.cancelled)
        self.assertEqual(self.escape.count(), 0)

    def test_zoom_navigator_right_side_touch_remains_available(self):
        window = QWidget()
        self.windows.append(window)
        window.showFullScreen()
        QCoreApplication.processEvents()
        navigator = ZoomNavigator(window)
        photo = QPixmap(400, 300)
        photo.fill('blue')
        navigator.set_photo(photo, QRectF(0, 0, 400, 300))
        navigator.set_view(QRectF(100, 75, 200, 150))
        navigator.move(window.width() - navigator.width() - 12, window.height() - navigator.height() - 12)
        navigator.show()
        window.activateWindow()
        QCoreApplication.processEvents()
        self.filter.set_enabled_edges({'top'})
        requested = QSignalSpy(navigator.centerRequested)
        point = navigator.mapTo(window, QPoint(navigator.width() - 3, navigator.height() // 2))
        self.assertLessEqual(window.width() - point.x(), self.filter.edge_height)
        target = window.windowHandle()
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.move(0, point + QPoint(-20, 0), target).commit()
        touch.release(0, point + QPoint(-20, 0), target).commit()
        self.assertGreater(requested.count(), 0)
        self.assertFalse(self.cancelled)
        self.assertEqual(self.escape.count(), 0)

    def test_filmstrip_and_map_shared_resize_handle_keep_top_edge_touch_drag(self):
        window = QWidget()
        self.windows.append(window)
        film = QWidget(window)
        film.setGeometry(0, 24, 400, 220)
        handle = FilmstripResizeHandle(film)
        handle.resize(400, 14)
        window.showFullScreen()
        window.activateWindow()
        QCoreApplication.processEvents()
        self.filter.set_enabled_edges({'top'})
        requested = QSignalSpy(handle.thicknessRequested)
        point = handle.mapTo(window, QPoint(4, 7))
        target = window.windowHandle()
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.move(0, point + QPoint(0, -90), target).commit()
        touch.release(0, point + QPoint(0, -90), target).commit()
        self.assertGreater(requested.count(), 0)
        self.assertFalse(self.cancelled)
        self.assertEqual(self.escape.count(), 0)


if __name__ == '__main__':
    unittest.main()
