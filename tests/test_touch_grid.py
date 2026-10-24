"""Overview touch gestures exercise real view scrollbars and clicked signals."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QContextMenuEvent, QFocusEvent, QMouseEvent, QStandardItem, QStandardItemModel, QTouchEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListView

from livegallery.touch_grid import OverviewGrid


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


if __name__ == '__main__':
    unittest.main()
