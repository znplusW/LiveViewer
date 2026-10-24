"""Real touch events scroll the filmstrip without selecting on drag release."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QContextMenuEvent, QFocusEvent, QMouseEvent, QStandardItem, QStandardItemModel, QTouchEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QListView, QVBoxLayout, QWidget

from livegallery.touch_filmstrip import FilmstripResizeHandle, TouchFilmstrip


class FilmstripTouchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.strip = TouchFilmstrip()
        self.strip.resize(480, 150)
        self.model = QStandardItemModel(self.strip)
        for row in range(3000):
            self.model.appendRow(QStandardItem(f'Photo {row}'))
        self.strip.setModel(self.model)
        self.strip.setFlow(QListView.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListView.Movement.Static)
        self.strip.setUniformItemSizes(True)
        self.strip.setGridSize(QSize(120, 90))
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.show()
        self.app.processEvents()
        self.clicked = []
        self.strip.clicked.connect(lambda index: self.clicked.append(index.row()))
        self.device = QTest.createTouchDevice()
        self.sequence = QTest.touchEvent(self.strip.viewport(), self.device, False)

    def tearDown(self):
        self.strip.close()
        self.strip.deleteLater()
        self.app.processEvents()

    def touch(self, method, position, identity=0):
        getattr(self.sequence, method)(identity, position, self.strip.viewport())
        self.sequence.commit()
        self.app.processEvents()

    def test_horizontal_swipes_from_thumbnail_and_blank_space_do_not_select(self):
        self.assertGreater(self.strip.horizontalScrollBar().maximum(), 10000)
        self.strip.setCurrentIndex(self.model.index(0, 0))
        selected = self.strip.currentIndex()
        first = self.strip.visualRect(self.model.index(2, 0))
        start = QPoint(first.center().x(), first.center().y())
        self.touch('press', start)
        self.touch('move', start - QPoint(70, 0))
        self.touch('release', start - QPoint(140, 0))
        after_left = self.strip.horizontalScrollBar().value()
        self.assertGreater(after_left, 0)
        self.assertEqual(self.strip.currentIndex(), selected)
        self.assertEqual(self.clicked, [])
        blank = next(QPoint(x, self.strip.viewport().height() - 6)
                     for x in range(40, 200)
                     if not self.strip.indexAt(QPoint(x, self.strip.viewport().height() - 6)).isValid())
        self.assertFalse(self.strip.indexAt(blank).isValid())
        self.touch('press', blank)
        self.touch('move', blank + QPoint(60, 0))
        self.touch('release', blank + QPoint(120, 0))
        self.assertLess(self.strip.horizontalScrollBar().value(), after_left)
        self.assertEqual(self.strip.currentIndex(), selected)
        self.assertEqual(self.clicked, [])
        self.assertEqual(self.model.rowCount(), 3000)

    def test_tap_selects_once_and_touch_mouse_duplicates_are_suppressed(self):
        target = self.strip.visualRect(self.model.index(2, 0)).center()
        self.touch('press', target)
        self.touch('release', target)
        self.assertEqual(self.clicked, [2])
        self.assertEqual(self.strip.currentIndex().row(), 2)
        for kind, button, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                                      (QEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)):
            mouse = QMouseEvent(kind, target, target, target, button, buttons,
                                Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventSynthesizedByQt)
            QApplication.sendEvent(self.strip.viewport(), mouse)
        self.assertEqual(self.clicked, [2])
        QTest.mouseClick(self.strip.viewport(), Qt.MouseButton.LeftButton, pos=target)
        self.assertEqual(self.clicked, [2, 2])

    def test_multifinger_join_and_leave_never_turns_into_a_tap(self):
        first = self.strip.visualRect(self.model.index(0, 0)).center()
        second = first + QPoint(70, 0)
        self.touch('press', first)
        self.sequence.stationary(0).press(1, second, self.strip.viewport()).commit()
        self.app.processEvents()
        self.sequence.stationary(0).release(1, second, self.strip.viewport()).commit()
        self.app.processEvents()
        self.touch('release', first)
        self.assertEqual(self.clicked, [])
        self.assertEqual(self.strip.horizontalScrollBar().value(), 0)

    def test_cancel_hide_focus_loss_and_vertical_drag_never_activate(self):
        center = self.strip.visualRect(self.model.index(0, 0)).center()
        for cancel in (
                lambda: QApplication.sendEvent(self.strip.viewport(), QTouchEvent(QEvent.Type.TouchCancel, self.device)),
                self.strip.hide,
                lambda: QApplication.sendEvent(self.strip, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))):
            self.strip.show()
            self.app.processEvents()
            self.touch('press', center)
            cancel()
            self.touch('release', center)
            self.assertEqual(self.clicked, [])
        self.strip.show()
        self.app.processEvents()
        self.touch('press', center)
        self.touch('move', center + QPoint(0, 30))
        self.touch('release', center)
        self.assertEqual(self.clicked, [])
        self.assertEqual(self.strip.horizontalScrollBar().value(), 0)

    def test_model_reset_or_disabled_item_cannot_activate_stale_touch(self):
        center = self.strip.visualRect(self.model.index(0, 0)).center()
        self.touch('press', center)
        self.model.clear()
        self.touch('release', center)
        self.assertEqual(self.clicked, [])
        item = QStandardItem('Disabled')
        item.setEnabled(False)
        self.model.appendRow(item)
        self.app.processEvents()
        center = self.strip.visualRect(self.model.index(0, 0)).center()
        self.touch('press', center)
        self.touch('release', center)
        self.assertEqual(self.clicked, [])

    def test_touch_focus_keeps_manually_scrolled_position_with_distant_selection(self):
        window = QWidget()
        layout = QVBoxLayout(window)
        editor = QLineEdit()
        layout.addWidget(editor)
        layout.addWidget(self.strip)
        window.resize(500, 220)
        window.show()
        window.activateWindow()
        self.app.processEvents()
        try:
            self.strip.setCurrentIndex(self.model.index(1000, 0))
            self.strip.scrollTo(self.strip.currentIndex())
            self.app.processEvents()
            self.strip.horizontalScrollBar().setValue(650)
            editor.setFocus()
            self.app.processEvents()
            self.touch('press', QPoint(300, 40))
            self.assertEqual(self.strip.horizontalScrollBar().value(), 650)
            self.touch('move', QPoint(220, 40))
            self.touch('release', QPoint(220, 40))
            self.assertEqual(self.strip.horizontalScrollBar().value(), 730)
            self.assertEqual(self.strip.currentIndex().row(), 1000)
            self.assertEqual(self.clicked, [])
        finally:
            self.strip.setParent(None)
            window.close()

    def test_mouse_wheel_keyboard_context_and_drag_keep_native_behavior(self):
        self.strip.setCurrentIndex(self.model.index(0, 0))
        QTest.keyClick(self.strip, Qt.Key.Key_Right)
        self.assertEqual(self.strip.currentIndex().row(), 1)

        center = self.strip.viewport().rect().center()
        wheel = QWheelEvent(QPointF(center), QPointF(self.strip.viewport().mapToGlobal(center)),
                            QPoint(), QPoint(-120, 0), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.strip.viewport(), wheel)
        self.assertGreater(self.strip.horizontalScrollBar().value(), 0)
        self.strip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        requests = []
        self.strip.customContextMenuRequested.connect(requests.append)
        context = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, center,
                                    self.strip.viewport().mapToGlobal(center))
        QApplication.sendEvent(self.strip.viewport(), context)
        self.assertEqual(len(requests), 1)
        # The subclass handles only touch and touch-synthesized mouse events;
        # a physical mouse drag remains the ordinary item-view selection path.
        self.strip.horizontalScrollBar().setValue(0)
        first = self.strip.visualRect(self.model.index(0, 0)).center()
        second = self.strip.visualRect(self.model.index(1, 0)).center()
        QTest.mousePress(self.strip.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(self.strip.viewport(), second)
        QTest.mouseRelease(self.strip.viewport(), Qt.MouseButton.LeftButton, pos=second)
        self.assertEqual(self.strip.currentIndex().row(), 1)

    def test_viewport_height_notification_uses_actual_viewport_without_model_reset(self):
        heights = []
        resets = []
        self.strip.viewportHeightChanged.connect(heights.append)
        self.model.modelReset.connect(lambda: resets.append(True))
        self.strip.resize(480, 220)
        self.app.processEvents()
        self.assertTrue(heights)
        self.assertEqual(heights[-1], self.strip.viewport().height())
        self.assertGreater(heights[-1], 150)
        self.assertEqual(self.model.rowCount(), 3000)
        self.assertEqual(resets, [])


class FilmstripHandleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(QWidget(), 1)
        self.film = QWidget()
        self.film.setFixedHeight(160)
        film_layout = QVBoxLayout(self.film)
        film_layout.setContentsMargins(0, 0, 0, 0)
        film_layout.setSpacing(0)
        self.handle = FilmstripResizeHandle(self.film)
        film_layout.addWidget(self.handle)
        film_layout.addWidget(QWidget(), 1)
        layout.addWidget(self.film)
        self.requests = []
        self.handle.heightRequested.connect(self.resize_film)
        self.window.resize(600, 480)
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()
        self.device = QTest.createTouchDevice()
        self.sequence = QTest.touchEvent(self.handle, self.device, False)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def resize_film(self, height):
        self.requests.append(height)
        self.film.setFixedHeight(height)

    def touch_global(self, method, global_position, identity=0):
        local = self.handle.mapFromGlobal(global_position)
        getattr(self.sequence, method)(identity, local, self.handle)
        self.sequence.commit()
        self.app.processEvents()

    def mouse_global(self, kind, global_position, *, button=Qt.MouseButton.NoButton,
                     buttons=Qt.MouseButton.NoButton,
                     source=Qt.MouseEventSource.MouseEventNotSynthesized):
        local = self.handle.mapFromGlobal(global_position)
        event = QMouseEvent(kind, QPointF(local), QPointF(local), QPointF(global_position),
                            button, buttons, Qt.KeyboardModifier.NoModifier, source)
        QApplication.sendEvent(self.handle, event)
        self.app.processEvents()

    def test_mouse_drag_changes_real_parent_height_without_local_coordinate_feedback(self):
        start = self.handle.mapToGlobal(self.handle.rect().center())
        self.mouse_global(QEvent.Type.MouseButtonPress, start,
                          button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self.mouse_global(QEvent.Type.MouseMove, start - QPoint(0, 40), buttons=Qt.MouseButton.LeftButton)
        self.assertEqual(self.film.height(), 200)
        self.assertEqual(self.handle.mapToGlobal(self.handle.rect().center()), start - QPoint(0, 40))
        self.mouse_global(QEvent.Type.MouseMove, start - QPoint(0, 40), buttons=Qt.MouseButton.LeftButton)
        self.assertEqual(self.requests, [200])
        self.mouse_global(QEvent.Type.MouseMove, start + QPoint(0, 10), buttons=Qt.MouseButton.LeftButton)
        self.mouse_global(QEvent.Type.MouseButtonRelease, start + QPoint(0, 10), button=Qt.MouseButton.LeftButton)
        self.assertEqual(self.film.height(), 150)
        self.assertEqual(self.requests, [200, 150])
        self.assertFalse(self.handle._mouse_dragging)

    def test_touch_drag_is_stable_and_synthetic_mouse_cannot_resize_again(self):
        start = self.handle.mapToGlobal(self.handle.rect().center())
        self.touch_global('press', start)
        self.touch_global('move', start - QPoint(0, 30))
        self.assertEqual(self.film.height(), 190)
        self.touch_global('move', start - QPoint(0, 30))
        self.assertEqual(self.requests, [190])
        self.touch_global('release', start - QPoint(0, 30))
        synthetic = Qt.MouseEventSource.MouseEventSynthesizedByQt
        self.mouse_global(QEvent.Type.MouseButtonPress, start, button=Qt.MouseButton.LeftButton,
                          buttons=Qt.MouseButton.LeftButton, source=synthetic)
        self.mouse_global(QEvent.Type.MouseMove, start - QPoint(0, 80),
                          buttons=Qt.MouseButton.LeftButton, source=synthetic)
        self.mouse_global(QEvent.Type.MouseButtonRelease, start - QPoint(0, 80),
                          button=Qt.MouseButton.LeftButton, source=synthetic)
        self.assertEqual(self.requests, [190])
        # A real mouse remains usable immediately after the touch finishes.
        start = self.handle.mapToGlobal(self.handle.rect().center())
        self.mouse_global(QEvent.Type.MouseButtonPress, start,
                          button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self.mouse_global(QEvent.Type.MouseMove, start - QPoint(0, 10), buttons=Qt.MouseButton.LeftButton)
        self.mouse_global(QEvent.Type.MouseButtonRelease, start - QPoint(0, 10), button=Qt.MouseButton.LeftButton)
        self.assertEqual(self.requests, [190, 200])

    def test_second_finger_stops_resize_until_all_fingers_release(self):
        start = self.handle.mapToGlobal(self.handle.rect().center())
        self.touch_global('press', start)
        self.touch_global('move', start - QPoint(0, 20))
        self.assertEqual(self.film.height(), 180)
        second = self.handle.mapFromGlobal(start + QPoint(40, -20))
        self.sequence.stationary(0).press(1, second, self.handle).commit()
        self.app.processEvents()
        first = self.handle.mapFromGlobal(start - QPoint(0, 80))
        self.sequence.move(0, first, self.handle).stationary(1).commit()
        self.app.processEvents()
        self.sequence.stationary(0).release(1, second, self.handle).commit()
        self.app.processEvents()
        self.touch_global('release', start - QPoint(0, 80))
        self.assertEqual(self.requests, [180])
        self.assertFalse(self.handle._touch_active)

    def test_cancel_hide_and_focus_loss_stop_touch_and_mouse_drags(self):
        for cancel in (
                lambda: QApplication.sendEvent(self.handle, QTouchEvent(QEvent.Type.TouchCancel, self.device)),
                self.handle.hide,
                lambda: QApplication.sendEvent(self.handle, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))):
            self.handle.show()
            self.app.processEvents()
            start = self.handle.mapToGlobal(self.handle.rect().center())
            self.touch_global('press', start)
            cancel()
            self.touch_global('release', start - QPoint(0, 60))
            self.assertEqual(self.requests, [])
        self.handle.show()
        for kind in (QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse, QEvent.Type.FocusOut):
            start = self.handle.mapToGlobal(self.handle.rect().center())
            self.mouse_global(QEvent.Type.MouseButtonPress, start,
                              button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            QApplication.sendEvent(self.handle, QEvent(kind))
            self.mouse_global(QEvent.Type.MouseMove, start - QPoint(0, 60), buttons=Qt.MouseButton.LeftButton)
            self.mouse_global(QEvent.Type.MouseButtonRelease, start - QPoint(0, 60), button=Qt.MouseButton.LeftButton)
            self.assertEqual(self.requests, [])

    def test_tap_and_right_mouse_do_not_resize(self):
        start = self.handle.mapToGlobal(self.handle.rect().center())
        self.touch_global('press', start)
        self.touch_global('release', start)
        self.mouse_global(QEvent.Type.MouseButtonPress, start,
                          button=Qt.MouseButton.RightButton, buttons=Qt.MouseButton.RightButton)
        self.mouse_global(QEvent.Type.MouseButtonRelease, start, button=Qt.MouseButton.RightButton)
        self.assertEqual(self.requests, [])



if __name__ == '__main__':
    unittest.main()
