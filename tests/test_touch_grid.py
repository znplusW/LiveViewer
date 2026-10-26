"""Overview touch gestures exercise real view scrollbars and clicked signals."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QContextMenuEvent, QEventPoint, QFocusEvent, QImage, QMouseEvent, QStandardItem, QStandardItemModel, QTouchEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QListView

from livegallery.touch_grid import OverviewGrid
from livegallery.catalog_model import CatalogModel, ThumbnailDelegate
from livegallery.models import MediaItem
from pathlib import Path


class RecordedPoint:
    """The position/state interface copied from a delivered hardware Qt point."""
    def __init__(self, contact, state, position):
        self._contact, self._state, self._position = contact, state, QPointF(position)

    def id(self):
        return self._contact

    def state(self):
        return self._state

    def position(self):
        return self._position


class RecordedTouch:
    """Replay a delivered event shape into the handler, without Qt regrouping."""
    def __init__(self, kind, device, points):
        self._kind, self._device, self._points = kind, device, points
        self.accepted = False

    def type(self):
        return self._kind

    def pointingDevice(self):
        return self._device

    def points(self):
        return self._points

    def accept(self):
        self.accepted = True


class OverviewTouchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.grid = OverviewGrid()
        self.grid.resize(440, 340)
        self.model = QStandardItemModel(self.grid)
        for row in range(3000):
            self.model.appendRow(QStandardItem(f'Photo {row}'))
        self.grid.setModel(self.model)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setGridSize(QSize(100, 90))
        self.grid.show()
        self.app.processEvents()
        self.clicked = []
        self.grid.clicked.connect(lambda index: self.clicked.append(index.row()))
        self.device = QTest.createTouchDevice()
        self.sequence = QTest.touchEvent(self.grid.viewport(), self.device, False)

    def tearDown(self):
        self.grid.close()
        self.grid.deleteLater()
        self.app.processEvents()

    def touch(self, method, position):
        getattr(self.sequence, method)(0, position, self.grid.viewport())
        self.sequence.commit()
        self.app.processEvents()

    def test_single_finger_swipe_scrolls_both_directions_without_opening(self):
        self.assertGreater(self.grid.verticalScrollBar().maximum(), 1000)
        self.touch('press', QPoint(200, 280))
        self.touch('move', QPoint(200, 180))
        self.touch('release', QPoint(200, 80))
        after_up = self.grid.verticalScrollBar().value()
        self.assertGreater(after_up, 0)
        self.assertEqual(self.clicked, [])
        self.touch('press', QPoint(200, 70))
        self.touch('move', QPoint(200, 170))
        self.touch('release', QPoint(200, 250))
        self.assertLess(self.grid.verticalScrollBar().value(), after_up)
        self.assertEqual(self.clicked, [])

    def test_tap_opens_once_and_following_synthetic_mouse_is_ignored(self):
        center = self.grid.visualRect(self.model.index(0, 0)).center()
        self.touch('press', center)
        self.touch('release', center)
        self.assertEqual(self.clicked, [0])
        for kind, button, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                                      (QEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)):
            mouse = QMouseEvent(kind, center, center, center, button, buttons,
                                Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventSynthesizedByQt)
            QApplication.sendEvent(self.grid.viewport(), mouse)
        self.assertEqual(self.clicked, [0])

    def test_cancelled_touch_never_opens_and_physical_mouse_still_works(self):
        center = self.grid.visualRect(self.model.index(0, 0)).center()
        self.touch('press', center)
        QApplication.sendEvent(self.grid.viewport(), QTouchEvent(QEvent.Type.TouchCancel, self.device))
        self.touch('release', center)
        self.assertEqual(self.clicked, [])
        QTest.mouseClick(self.grid.viewport(), Qt.MouseButton.LeftButton, pos=center)
        self.assertEqual(self.clicked, [0])

    def test_mouse_wheel_and_context_menu_keep_standard_item_view_behavior(self):
        center = self.grid.viewport().rect().center()
        wheel = QWheelEvent(QPointF(center), QPointF(self.grid.viewport().mapToGlobal(center)),
                            QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.grid.viewport(), wheel)
        self.assertGreater(self.grid.verticalScrollBar().value(), 0)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        context_requests = []
        self.grid.customContextMenuRequested.connect(context_requests.append)
        context = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, center,
                                    self.grid.viewport().mapToGlobal(center))
        QApplication.sendEvent(self.grid.viewport(), context)
        self.assertEqual(len(context_requests), 1)
        self.assertEqual(self.clicked, [])

    def test_focus_loss_cancels_pending_tap_before_late_release(self):
        center = self.grid.visualRect(self.model.index(0, 0)).center()
        self.touch('press', center)
        QApplication.sendEvent(self.grid, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
        self.touch('release', center)
        self.assertEqual(self.clicked, [])

    def prepare_zoom(self):
        self.grid.resize(900, 500)
        self.catalog = CatalogModel(self.grid)
        path = Path('/synthetic-thumbnail.png')
        self.catalog.set_items([MediaItem(str(row), path) for row in range(3000)])
        image = QImage(80, 60, QImage.Format.Format_RGB32)
        image.fill('#406078')
        self.catalog.put_thumbnail(path, image)
        self.grid.setModel(self.catalog)
        self.grid.setItemDelegate(ThumbnailDelegate(self.grid, large=True))
        self.grid.setSpacing(5)
        self.grid.set_thumbnail_width(204)
        self.grid.setCurrentIndex(self.catalog.index(1200))
        self.grid.scrollTo(self.catalog.index(1200))
        self.app.processEvents()
        return self.grid.viewport().rect().center()

    def test_pinch_reflows_actual_tiles_without_reset_and_preserves_center_photo(self):
        center = self.prepare_zoom()
        baseline = self.grid.visualRect(self.catalog.index(1200)).size()
        reset = QSignalSpy(self.catalog.modelReset)
        changed = QSignalSpy(self.grid.thumbnail_size_changed)
        requested = QSignalSpy(self.catalog.thumbnail_requested)
        self.sequence.press(0, center - QPoint(60, 0), self.grid.viewport())
        self.sequence.press(1, center + QPoint(60, 0), self.grid.viewport()).commit()
        self.app.processEvents()
        anchor, fraction, target_y = self.grid._pinch_anchor
        original_row = anchor.row()
        self.assertGreater(original_row, 1000)
        self.sequence.move(0, center - QPoint(120, 0), self.grid.viewport())
        self.sequence.move(1, center + QPoint(120, 0), self.grid.viewport()).commit()
        self.app.processEvents()
        self.assertEqual(self.grid.thumbnail_width, 408)
        enlarged = self.grid.visualRect(self.catalog.index(1200)).size()
        self.assertGreater(enlarged.width(), baseline.width())
        self.assertGreater(enlarged.height(), baseline.height())
        rect = self.grid.visualRect(anchor)
        self.assertLessEqual(abs(rect.top() + fraction * rect.height() - target_y), 1)
        self.sequence.move(0, center - QPoint(90, 0), self.grid.viewport())
        self.sequence.move(1, center + QPoint(90, 0), self.grid.viewport()).commit()
        self.app.processEvents()
        self.assertEqual(self.grid.thumbnail_width, 306)
        self.assertGreater(self.grid.verticalScrollBar().value(), 10000)
        self.assertEqual(self.grid.currentIndex().row(), 1200)
        self.assertEqual(reset.count(), 0)
        self.assertGreaterEqual(changed.count(), 2)
        self.assertEqual(self.catalog.rowCount(), 3000)
        self.assertEqual(len(self.catalog.thumbnails), 1)
        self.assertEqual(requested.count(), 0)
        self.sequence.release(1, center + QPoint(90, 0), self.grid.viewport())
        self.sequence.stationary(0).commit()
        self.sequence.move(0, center, self.grid.viewport()).commit()
        self.sequence.release(0, center, self.grid.viewport()).commit()
        self.app.processEvents()
        self.assertEqual(self.clicked, [])

    def test_second_finger_cancels_tap_and_zoom_limits_empty_and_cancel_are_safe(self):
        center = self.prepare_zoom()
        self.sequence.press(0, center - QPoint(40, 0), self.grid.viewport()).commit()
        self.sequence.stationary(0).press(1, center + QPoint(40, 0), self.grid.viewport()).commit()
        self.sequence.move(0, center - QPoint(180, 0), self.grid.viewport())
        self.sequence.move(1, center + QPoint(180, 0), self.grid.viewport()).commit()
        self.app.processEvents()
        self.assertEqual(self.grid.thumbnail_width, self.grid.MAX_WIDTH)
        QApplication.sendEvent(self.grid.viewport(), QTouchEvent(QEvent.Type.TouchCancel, self.device))
        self.sequence.release(0, center, self.grid.viewport()).release(1, center, self.grid.viewport()).commit()
        self.app.processEvents()
        self.assertEqual(self.clicked, [])
        self.grid.set_thumbnail_width(-100)
        self.assertEqual(self.grid.thumbnail_width, self.grid.MIN_WIDTH)
        self.grid.zoom_step(-1)
        self.assertEqual(self.grid.thumbnail_width, self.grid.MIN_WIDTH)
        self.catalog.set_items([])
        self.grid.set_thumbnail_width(900)
        self.assertEqual(self.grid.thumbnail_width, self.grid.MAX_WIDTH)

    def recorded_touch(self, *points, kind=QEvent.Type.TouchUpdate, device=None):
        event = RecordedTouch(kind, device or self.device,
                              [RecordedPoint(contact, state, point) for contact, state, point in points])
        self.assertTrue(self.grid.viewportEvent(event))
        self.assertTrue(event.accepted)

    def test_update_pressed_pinch_owns_new_ids_and_ends_despite_unowned_stationary(self):
        # Hardware06 QWidget delivery: 220 Pressed + old177 Stationary, then
        # 220 Updated + 221 Pressed +177; both releases are TouchUpdate.
        center = self.prepare_zoom()
        state = QEventPoint.State
        old = (177, state.Stationary, center + QPoint(20, 170))
        self.recorded_touch((220, state.Pressed, center - QPoint(60, 0)), old)
        self.recorded_touch((220, state.Stationary, center - QPoint(60, 0)),
                            (221, state.Pressed, center + QPoint(60, 0)), old)
        self.recorded_touch((220, state.Updated, center - QPoint(120, 0)),
                            (221, state.Updated, center + QPoint(120, 0)), old)
        self.assertEqual(self.grid.thumbnail_width, 408)
        # Point-list order cannot reset the same pair's pinch baseline.
        self.recorded_touch((221, state.Updated, center + QPoint(90, 0)), old,
                            (220, state.Updated, center - QPoint(90, 0)))
        self.assertEqual(self.grid.thumbnail_width, 306)
        self.recorded_touch((220, state.Stationary, center - QPoint(90, 0)),
                            (221, state.Released, center + QPoint(90, 0)), old)
        self.assertTrue(self.grid._touch_active)
        self.recorded_touch((220, state.Released, center - QPoint(90, 0)), old)
        self.assertFalse(self.grid._touch_active)
        self.assertEqual(self.clicked, [])

    def test_update_only_scroll_release_finishes_and_next_explicit_tap_opens_once(self):
        state = QEventPoint.State
        old = (177, state.Stationary, QPoint(300, 300))
        self.recorded_touch((220, state.Pressed, QPoint(200, 280)), old)
        self.recorded_touch((220, state.Updated, QPoint(200, 180)), old)
        self.recorded_touch((220, state.Released, QPoint(200, 80)), old)
        self.assertGreater(self.grid.verticalScrollBar().value(), 0)
        self.assertFalse(self.grid._touch_active)
        self.assertEqual(self.clicked, [])
        self.grid.verticalScrollBar().setValue(0)
        center = self.grid.visualRect(self.model.index(0, 0)).center()
        self.recorded_touch((221, state.Pressed, center), old)
        self.recorded_touch((221, state.Released, center), old)
        self.assertEqual(self.clicked, [0])
        self.assertFalse(self.grid._touch_active)

    def test_unknown_updates_and_cancelled_tail_cannot_start_but_fresh_ids_can(self):
        center = self.prepare_zoom()
        state = QEventPoint.State
        self.recorded_touch((10, state.Updated, center - QPoint(60, 0)),
                            (11, state.Stationary, center + QPoint(60, 0)))
        self.assertFalse(self.grid._touch_active)
        self.assertEqual(self.grid.thumbnail_width, 204)
        self.recorded_touch((10, state.Pressed, center - QPoint(60, 0)), kind=QEvent.Type.TouchBegin)
        QApplication.sendEvent(self.grid, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
        self.recorded_touch((10, state.Updated, center - QPoint(120, 0)))
        self.recorded_touch((10, state.Released, center - QPoint(120, 0)))
        self.assertEqual(self.clicked, [])
        self.assertFalse(self.grid._touch_active)
        self.recorded_touch((20, state.Pressed, center - QPoint(60, 0)),
                            (21, state.Pressed, center + QPoint(60, 0)),
                            (10, state.Stationary, center + QPoint(200, 100)))
        self.recorded_touch((20, state.Updated, center - QPoint(120, 0)),
                            (21, state.Updated, center + QPoint(120, 0)),
                            (10, state.Updated, center + QPoint(300, 100)))
        self.assertEqual(self.grid.thumbnail_width, 408)
        self.recorded_touch((20, state.Released, center - QPoint(120, 0)),
                            (21, state.Released, center + QPoint(120, 0)))
        self.assertFalse(self.grid._touch_active)
        self.assertEqual(self.clicked, [])

    def test_second_device_cannot_join_resize_or_cancel_owned_gesture(self):
        center = self.prepare_zoom()
        state = QEventPoint.State
        other = QTest.createTouchDevice()
        self.recorded_touch((4, state.Pressed, center - QPoint(60, 0)), kind=QEvent.Type.TouchBegin)
        self.recorded_touch((4, state.Stationary, center - QPoint(60, 0)),
                            (5, state.Pressed, center + QPoint(60, 0)), device=other)
        self.recorded_touch((4, state.Updated, center - QPoint(180, 0)),
                            (5, state.Updated, center + QPoint(180, 0)), device=other)
        self.recorded_touch((4, state.Released, center - QPoint(180, 0)),
                            (5, state.Released, center + QPoint(180, 0)), device=other)
        self.recorded_touch(kind=QEvent.Type.TouchCancel, device=other)
        self.assertTrue(self.grid._touch_active)
        self.assertEqual(self.grid.thumbnail_width, 204)
        self.recorded_touch((4, state.Stationary, center - QPoint(60, 0)),
                            (5, state.Pressed, center + QPoint(60, 0)))
        self.recorded_touch((4, state.Updated, center - QPoint(120, 0)),
                            (5, state.Updated, center + QPoint(120, 0)))
        self.assertEqual(self.grid.thumbnail_width, 408)
        self.recorded_touch((4, state.Released, center - QPoint(120, 0)),
                            (5, state.Released, center + QPoint(120, 0)))
        self.assertFalse(self.grid._touch_active)
        self.assertEqual(self.clicked, [])

    def test_hide_cancels_owned_points_and_late_release_never_activates(self):
        center = self.grid.visualRect(self.model.index(0, 0)).center()
        state = QEventPoint.State
        self.recorded_touch((4, state.Pressed, center))
        self.grid.hide()
        self.grid.show()
        self.app.processEvents()
        self.recorded_touch((4, state.Updated, center))
        self.recorded_touch((4, state.Released, center))
        self.assertFalse(self.grid._touch_active)
        self.assertEqual(self.clicked, [])
        self.recorded_touch((5, state.Pressed, center))
        # The cancelled old point's late release does not end the fresh owner.
        self.recorded_touch((4, state.Released, center), (5, state.Stationary, center))
        self.assertTrue(self.grid._touch_active)
        self.recorded_touch((5, state.Released, center))
        self.assertFalse(self.grid._touch_active)
        self.assertEqual(self.clicked, [0])


if __name__ == '__main__':
    unittest.main()
