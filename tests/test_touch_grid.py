"""Overview touch gestures exercise real view scrollbars and clicked signals."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QContextMenuEvent, QFocusEvent, QImage, QMouseEvent, QStandardItem, QStandardItemModel, QTouchEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QListView

from livegallery.touch_grid import OverviewGrid
from livegallery.catalog_model import CatalogModel, ThumbnailDelegate
from livegallery.models import MediaItem
from pathlib import Path


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


if __name__ == '__main__':
    unittest.main()
