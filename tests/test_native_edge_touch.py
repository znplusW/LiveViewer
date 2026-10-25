"""Native edge ownership without relying on synthesized QWidget touch events."""
import ctypes
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from livegallery.fullscreen_edge_gesture import FullscreenEdgeGesture
from livegallery.native_edge_touch import (NativeEdgeTouch, PointerInfo, DOWN, UPDATE, UP,
                                           ENTER, CAPTURE_CHANGED, NEW, INCONTACT, CANCELED)


class Backend:
    def __init__(self, window):
        self.window = window
        self.point = QPointF()
        self.count = 1
        self.flags = INCONTACT | NEW
        self.kind = 2

    def info(self, pointer):
        return SimpleNamespace(pointerType=self.kind, pointerFlags=self.flags)

    def contacts(self, pointer):
        return self.count

    def root(self, hwnd):
        return hwnd

    def position(self, window, info):
        return self.point


class NativeEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.showFullScreen()
        self.app.processEvents()
        self.cancelled = []
        self.owner = FullscreenEdgeGesture(lambda: (self.window,), enabled_edges=('top', 'left', 'right'),
                                           cancel_active=lambda: self.cancelled.append(True))
        self.backend = Backend(self.window)
        self.native = NativeEdgeTouch(self.owner, self.backend)
        self.signal = QSignalSpy(self.owner.escapeRequested)
        self.hwnd = int(self.window.winId())

    def tearDown(self):
        self.native.close()
        self.owner.close()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def send(self, kind, x, y=200, pointer=1):
        self.backend.point = QPointF(x, y)
        return self.native.handle(self.hwnd, kind, pointer)

    def test_exact_physical_boundaries_accept_left_right_top_once(self):
        for x, y, dx, dy in ((0, 200, 100, 0), (self.window.width(), 200, -100, 0), (300, 0, 0, 100)):
            before = self.signal.count()
            self.assertTrue(self.send(DOWN, x, y))
            self.assertTrue(self.send(UPDATE, x + dx, y + dy))
            self.assertTrue(self.send(UP, x + dx, y + dy))
            self.assertEqual(self.signal.count(), before + 1)
            self.assertFalse(self.native.ids)
        self.assertEqual(len(self.cancelled), 3)

    def test_new_contact_enter_can_start_at_bezel_but_update_cannot_steal_swipe(self):
        self.assertFalse(self.send(UPDATE, 0))
        self.backend.flags = INCONTACT
        self.assertFalse(self.send(ENTER, 0))
        self.backend.flags |= NEW
        self.assertTrue(self.send(ENTER, 0))
        self.assertTrue(self.send(DOWN, 10))
        self.assertTrue(self.send(UPDATE, 100))
        self.assertTrue(self.send(UP, 100))
        self.assertEqual(self.signal.count(), 1)

    def test_whole_normal_contact_stays_with_qt_even_when_it_moves_to_edge(self):
        self.assertFalse(self.send(DOWN, 200))
        self.assertFalse(self.send(UPDATE, 0))
        self.assertFalse(self.send(UP, 100))
        self.assertEqual(self.signal.count(), 0)

    def test_two_fingers_cancel_and_drain_owned_contacts(self):
        self.assertTrue(self.send(DOWN, 0))
        self.backend.count = 2
        self.assertTrue(self.send(DOWN, 120, pointer=2))
        self.assertTrue(self.send(UPDATE, 100))
        self.assertTrue(self.send(UP, 100))
        self.backend.count = 1
        self.assertTrue(self.send(UP, 220, pointer=2))
        self.assertFalse(self.native.ids)
        self.assertEqual(self.signal.count(), 0)
        self.backend.count = 2
        self.assertFalse(self.send(DOWN, 0))

    def test_capture_loss_and_canceled_contact_never_exit_or_leave_stale_owner(self):
        self.send(DOWN, 0)
        self.assertTrue(self.send(CAPTURE_CHANGED, 100))
        self.assertFalse(self.native.ids)
        self.assertFalse(self.send(UPDATE, 200))
        self.send(DOWN, 0)
        self.backend.flags |= CANCELED
        self.assertTrue(self.send(UPDATE, 120))
        self.assertTrue(self.send(UP, 120))
        self.assertFalse(self.native.ids)
        self.assertEqual(self.signal.count(), 0)

    def test_disabled_windowed_pen_control_and_missing_frame_are_not_claimed(self):
        self.owner.set_enabled_edges([])
        self.assertFalse(self.send(DOWN, 0))
        self.owner.set_enabled_edges(['left'])
        self.backend.kind = 3
        self.assertFalse(self.send(DOWN, 0))
        self.backend.kind, self.backend.count = 2, None
        self.assertFalse(self.send(DOWN, 0))
        self.backend.count = 1
        button = QPushButton('Close', self.window)
        button.setGeometry(0, 170, 80, 60)
        button.show()
        self.assertFalse(self.send(DOWN, 2))
        button.hide()
        self.window.showNormal()
        self.assertFalse(self.send(DOWN, 0))

    def test_fullscreen_exit_drains_tail_and_later_contact_is_normal(self):
        self.owner.escapeRequested.connect(lambda _: self.window.showNormal())
        self.assertTrue(self.send(DOWN, 0))
        self.assertTrue(self.send(UPDATE, 120))
        self.assertFalse(self.window.isFullScreen())
        self.assertTrue(self.send(UPDATE, 150))
        self.assertTrue(self.send(UP, 160))
        self.assertFalse(self.send(DOWN, 0))
        self.assertEqual(self.signal.count(), 1)

    def test_wrong_direction_short_and_deactivation_cancel(self):
        self.send(DOWN, 0)
        self.send(UPDATE, 10, 320)
        self.send(UP, 120)
        self.assertEqual(self.signal.count(), 0)
        self.send(DOWN, 0)
        self.native.observe(self.window, QEvent(QEvent.Type.ApplicationDeactivate))
        self.send(UP, 120)
        self.assertEqual(self.signal.count(), 0)
        self.send(DOWN, 0)
        self.send(UP, 40)
        self.assertEqual(self.signal.count(), 0)

    def test_pointer_info_x64_layout_is_windows_abi(self):
        self.assertEqual(ctypes.sizeof(PointerInfo), 96)
        self.assertEqual(PointerInfo.ptPixelLocationRaw.offset, 48)
        self.assertEqual(PointerInfo.PerformanceCount.offset, 80)
