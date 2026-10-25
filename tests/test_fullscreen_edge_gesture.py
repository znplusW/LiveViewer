"""Screen-edge reservation with real Qt-window touch injection."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QContextMenuEvent, QMouseEvent, QPixmap, QTouchEvent
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

    def test_three_edges_corner_directions_default_left_and_explicit_empty(self):
        default = FullscreenEdgeGesture(lambda: ())
        try:
            self.assertEqual(default.enabled_edges, frozenset({'left'}))
        finally:
            default.close()
        cases = [({'left'}, QPoint(4, 160), QPoint(100, 2)),
                 ({'right'}, QPoint(self.window.width() - 4, 160), QPoint(-100, 2)),
                 ({'top'}, QPoint(160, 4), QPoint(2, 100)),
                 ({'top', 'left'}, QPoint(4, 4), QPoint(100, 2)),
                 ({'top', 'left'}, QPoint(4, 4), QPoint(2, 100)),
                 ({'top', 'right'}, QPoint(self.window.width() - 4, 4), QPoint(-100, 2))]
        for index, (edges, start, delta) in enumerate(cases):
            with self.subTest(edges=edges, delta=delta):
                self.filter.set_enabled_edges(edges)
                self.touch.press(0, start, self.target).commit()
                self.touch.move(0, start + delta, self.target).commit()
                self.touch.release(0, start + delta, self.target).commit()
                self.assertEqual(self.escape.count(), index + 1)
        count = len(self.cancelled)
        self.filter.set_enabled_edges([])
        self.window.touch_events.clear()
        self.touch.press(0, self.start, self.target).commit()
        self.touch.move(0, self.start + QPoint(0, 120), self.target).commit()
        self.touch.release(0, self.start + QPoint(0, 120), self.target).commit()
        self.assertEqual(self.escape.count(), len(cases))
        self.assertEqual(len(self.cancelled), count)
        self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)

    def test_side_bands_accept_24_36_48_logical_pixel_starts_in_both_directions(self):
        self.filter.set_enabled_edges({'left', 'right'})
        for edge in ('left', 'right'):
            for inset in (24, 36, 48):
                with self.subTest(edge=edge, inset=inset):
                    before = self.escape.count()
                    self.window.touch_events.clear()
                    start = QPoint(inset if edge == 'left' else self.window.width() - inset,
                                   self.window.height() // 2)
                    delta = QPoint(110 if edge == 'left' else -110, 2)
                    self.touch.press(0, start, self.target).commit()
                    claimed = self.filter._target is not None
                    self.touch.move(0, start + delta, self.target).commit()
                    self.touch.release(0, start + delta, self.target).commit()
                    self.assertTrue(claimed)
                    self.assertEqual(self.escape.count(), before + 1)
                    self.assertEqual(self.window.touch_events, [])
                    self.assertEqual(self.window.mouse_events, [])

    def test_side_49_and_top_24_remain_content_and_updates_never_claim_half_contact(self):
        self.filter.set_enabled_edges({'top', 'left', 'right'})
        cases = ((QPoint(49, 160), QPoint(110, 0)),
                 (QPoint(self.window.width() - 49, 160), QPoint(-110, 0)),
                 (QPoint(self.window.width() // 2, 24), QPoint(0, 110)))
        for start, delta in cases:
            self.window.touch_events.clear()
            self.touch.press(0, start, self.target).commit()
            self.touch.move(0, start + delta, self.target).commit()
            self.touch.release(0, start + delta, self.target).commit()
            self.assertIn(QEvent.Type.TouchBegin, self.window.touch_events)
            self.assertEqual(self.escape.count(), 0)
        # A contact delivered to the photo stays there if it later enters the
        # wider band; there is no late takeover or use of guessed press data.
        start = QPoint(70, 160)
        self.touch.press(0, start, self.target).commit()
        self.touch.move(0, QPoint(24, 160), self.target).commit()
        self.touch.move(0, QPoint(140, 160), self.target).commit()
        self.touch.release(0, QPoint(140, 160), self.target).commit()
        self.assertEqual(self.escape.count(), 0)
        self.assertFalse(self.cancelled)

    def test_wider_side_band_keeps_single_finger_direction_and_control_exemptions(self):
        self.filter.set_enabled_edges({'left'})
        start = QPoint(36, 160)
        for delta in (QPoint(50, 0), QPoint(-100, 0), QPoint(100, 90)):
            self.touch.press(0, start, self.target).commit()
            self.touch.move(0, start + delta, self.target).commit()
            self.touch.release(0, start + delta, self.target).commit()
            self.assertEqual(self.escape.count(), 0)
        self.touch.press(0, start, self.target).commit()
        self.touch.stationary(0).press(1, start + QPoint(60, 0), self.target).commit()
        self.touch.move(0, start + QPoint(120, 0), self.target).move(1, start + QPoint(180, 0), self.target).commit()
        self.touch.release(0, start + QPoint(120, 0), self.target).release(1, start + QPoint(180, 0), self.target).commit()
        self.assertEqual(self.escape.count(), 0)
        window = QWidget()
        self.windows.append(window)
        button = QPushButton('靠侧边按钮', window)
        button.setGeometry(24, 100, 120, 38)
        window.showFullScreen()
        window.activateWindow()
        QCoreApplication.processEvents()
        clicked = QSignalSpy(button.clicked)
        target = window.windowHandle()
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, QPoint(36, 120), target).commit()
        touch.release(0, QPoint(36, 120), target).commit()
        self.assertEqual(clicked.count(), 1)
        self.assertEqual(self.escape.count(), 0)

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

    def test_left_scrollbar_touch_drag_is_exempt_from_enabled_left_edge(self):
        window = QWidget()
        self.windows.append(window)
        bar = QScrollBar(Qt.Orientation.Vertical, window)
        bar.setGeometry(0, 40, 18, 300)
        bar.setRange(0, 200)
        bar.setPageStep(30)
        bar.setValue(100)
        window.showFullScreen()
        window.activateWindow()
        QCoreApplication.processEvents()
        self.filter.set_enabled_edges({'left'})
        option = QStyleOptionSlider()
        bar.initStyleOption(option)
        thumb = bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                           QStyle.SubControl.SC_ScrollBarSlider, bar)
        point = bar.mapTo(window, thumb.center())
        target = window.windowHandle()
        touch = QTest.touchEvent(target, QTest.createTouchDevice(), False)
        touch.press(0, point, target).commit()
        touch.move(0, point + QPoint(0, 70), target).commit()
        touch.release(0, point + QPoint(0, 70), target).commit()
        self.assertGreater(bar.value(), 100)
        self.assertFalse(self.cancelled)
        self.assertEqual(self.escape.count(), 0)

    def test_zoom_navigator_touch_is_exempt_from_overlapping_right_edge_band(self):
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
        self.filter.set_enabled_edges({'right'})
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

    def test_filmstrip_and_map_shared_resize_handle_keep_left_edge_touch_drag(self):
        window = QWidget()
        self.windows.append(window)
        film = QWidget(window)
        film.setGeometry(0, 120, 400, 220)
        handle = FilmstripResizeHandle(film)
        handle.resize(400, 14)
        window.showFullScreen()
        window.activateWindow()
        QCoreApplication.processEvents()
        self.filter.set_enabled_edges({'left'})
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
