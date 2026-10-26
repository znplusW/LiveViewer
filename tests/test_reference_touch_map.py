"""Real-window mapped input, without claims about physical touch hardware."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QCursor, QImage, QMouseEvent, QWheelEvent
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QVBoxLayout, QWidget

from livegallery.reference_touch_map import ReferenceTouchMap
from livegallery.viewer import ViewerCanvas
from tests.test_viewer import FakePlayer, MousePressAuthorization


class ReferenceTouchMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.video = Path(self.temporary.name) / 'synthetic.mov'
        self.video.touch()
        self.viewer = ViewerCanvas()
        self.viewer.setGeometry(550, 30, 800, 600)
        self.controls = QWidget()
        self.controls.setGeometry(20, 30, 450, 460)
        layout = QVBoxLayout(self.controls)
        self.edit = QLineEdit()
        layout.addWidget(self.edit)
        self.map = ReferenceTouchMap(self.viewer)
        layout.addWidget(self.map, 1)
        self.viewer.set_window_group([self.viewer, self.controls])
        self.viewer.show()
        self.controls.show()
        self.controls.activateWindow()
        self.edit.setFocus()
        QTest.qWait(25)
        self.viewer._player = self.player = FakePlayer()
        self.image = QImage(2400, 1800, QImage.Format.Format_RGB32)
        self.image.fill('#cc5533')
        self.viewer.set_photo(self.image, self.video)
        self.viewer.set_options(hold_ms=100)
        QCoreApplication.processEvents()
        self.target = self.controls.windowHandle()
        self.device = QTest.createTouchDevice()
        self.touch = QTest.touchEvent(self.target, self.device, False)

    def tearDown(self):
        self.viewer.cancel_interaction()
        self.controls.close()
        self.viewer.close()
        self.map.deleteLater()
        self.controls.deleteLater()
        self.viewer.deleteLater()
        QCoreApplication.processEvents()
        self.temporary.cleanup()

    def point(self, viewer_point):
        return self.map.mapTo(self.controls, self.map.map_from_viewer(QPointF(viewer_point)).toPoint())

    def center(self):
        return self.point(self.viewer.viewport().rect().center())

    def wait(self, predicate, seconds=1.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            QTest.qWait(10)
            if predicate():
                return
        self.fail('Mapped gesture did not reach its expected state')

    def test_full_surface_maps_visible_photo_without_thumbnail_or_source_io(self):
        self.assertEqual(self.map.accessibleName(), '预览触控板')
        portrait = QImage(800, 1600, QImage.Format.Format_RGB32)
        portrait.fill('#cc5533')
        self.viewer.set_photo(portrait, self.video)
        self.map._refresh()
        area = self.map.mapped_rect()
        self.assertEqual(area, QRectF(self.map.rect()))
        output = self.map.grab().toImage()
        ratio = self.map.devicePixelRatioF()
        for point in (QPoint(2, 2), self.map.rect().center(), self.map.rect().bottomRight() - QPoint(2, 2)):
            self.assertEqual(output.pixelColor(round(point.x() * ratio), round(point.y() * ratio)),
                             QColor(self.map._colors['surface']))
            self.assertTrue(self.map.image_contains_viewport_point(QPointF(point)))
            self.assertTrue(self.viewer.image_contains_viewport_point(self.map.map_to_viewer(QPointF(point))))
        self.assertFalse(hasattr(self.map, '_thumbnail'))
        self.assertFalse(hasattr(self.map, '_photo_key'))
        with patch('builtins.open', side_effect=AssertionError('Touchpad reread a source file')):
            self.viewer._zoom_at(2, QPointF(400, 300))
            for _ in range(15):
                self.viewer._pan_by(QPointF(3, 2))
                self.map._refresh()
            self.map.grab()
        with patch.object(self.viewer._photo, 'pixmap', side_effect=AssertionError('Touchpad sampled the photo')):
            self.map._refresh()
            self.map.grab()
        self.viewer.start_live()
        frame = QImage(320, 240, QImage.Format.Format_RGB32)
        frame.fill('green')
        for _ in range(8):
            self.viewer._video_frame(QVideoFrame(frame), self.viewer._generation)
            self.map._refresh()
        self.assertFalse(hasattr(self.map, '_thumbnail'))
        self.assertEqual(self.map.grab().toImage(), output)

    def test_touch_hold_plays_existing_player_without_focus_or_os_cursor_change(self):
        player = self.viewer._player
        cursor = QCursor.pos()
        point = self.center()
        self.touch.press(0, point, self.target).commit()
        self.wait(lambda: self.player.play_calls == 1)
        self.assertIs(self.viewer._player, player)
        self.assertIs(QApplication.activeWindow(), self.controls)
        self.assertIs(QApplication.focusWidget(), self.edit)
        self.assertEqual(QCursor.pos(), cursor)
        self.assertIs(self.viewer._input_owner, self.map)
        self.touch.release(0, point, self.target).commit()
        self.assertFalse(self.viewer._play_requested)
        self.assertIsNone(self.viewer._input_owner)

    def test_fit_swipes_both_directions_and_zoomed_swipe_pans_instead(self):
        navigated = []
        self.viewer.navigate_requested.connect(navigated.append)
        center = self.center()
        for offset in (-100, 100):
            self.touch.press(0, center, self.target).commit()
            self.touch.move(0, center + QPoint(offset, 0), self.target).commit()
            self.touch.release(0, center + QPoint(offset, 0), self.target).commit()
        self.assertEqual(navigated, [1, -1])
        self.viewer._zoom_at(2, self.viewer._viewport_center())
        before = self.viewer._scene_center()
        self.touch.press(0, center, self.target).commit()
        self.touch.move(0, center + QPoint(50, 20), self.target).commit()
        self.touch.release(0, center + QPoint(50, 20), self.target).commit()
        self.assertGreater((self.viewer._scene_center() - before).manhattanLength(), 1)
        self.assertEqual(navigated, [1, -1])
        self.assertEqual(self.player.play_calls, 0)

    def test_pinch_keeps_mapped_anchor_and_uses_existing_zoom(self):
        center = self.center() + QPoint(20, -10)
        local = self.map.mapFrom(self.controls, center)
        mapped = self.map.map_to_viewer(QPointF(local))
        subject = self.viewer.mapToScene(mapped.toPoint())
        first, second = center - QPoint(30, 0), center + QPoint(30, 0)
        self.touch.press(0, first, self.target).press(1, second, self.target).commit()
        self.touch.move(0, first - QPoint(30, 0), self.target).move(1, second + QPoint(30, 0), self.target).commit()
        self.assertAlmostEqual(self.viewer.zoom_factor(), 2, delta=.03)
        after = self.viewer.mapToScene(mapped.toPoint())
        self.assertLess((subject - after).manhattanLength(), 7)
        self.touch.release(0, first - QPoint(30, 0), self.target).release(1, second + QPoint(30, 0), self.target).commit()
        self.assertEqual(self.player.play_calls, 0)

    def test_equal_distance_two_finger_rotation_does_not_zoom_or_pan(self):
        self.viewer.resize(1000, 720)
        self.controls.resize(320, 500)
        self.app.processEvents()
        for size, offsets in (
                ((2000, 400), (QPoint(0, 50), QPoint(30, 40), QPoint(40, 30), QPoint(50, 0))),
                ((400, 2000), (QPoint(50, 0), QPoint(40, 30), QPoint(30, 40), QPoint(0, 50)))):
            with self.subTest(image=size):
                image = QImage(*size, QImage.Format.Format_RGB32)
                image.fill('#334455')
                self.viewer.set_photo(image, self.video)
                self.viewer._zoom_at(1.5, self.viewer._viewport_center())
                zoom, subject = self.viewer.zoom_factor(), self.viewer._scene_center()
                center = self.map.mapTo(self.controls, self.map.rect().center())
                offset = offsets[0]
                self.touch.press(0, center - offset, self.target).press(1, center + offset, self.target).commit()
                try:
                    for offset in offsets[1:]:
                        self.touch.move(0, center - offset, self.target).move(1, center + offset, self.target).commit()
                        self.assertAlmostEqual(self.viewer.zoom_factor(), zoom, delta=.03)
                        self.assertLess((self.viewer._scene_center() - subject).manhattanLength(), 7)
                finally:
                    self.touch.release(0, center - offset, self.target).release(1, center + offset, self.target).commit()
                self.assertEqual(self.player.play_calls, 0)
                self.assertIsNone(self.viewer._input_owner)

    def test_pinch_scale_is_independent_of_finger_orientation(self):
        self.viewer.resize(1000, 720)
        self.controls.resize(320, 500)
        self.app.processEvents()
        for size in ((2000, 400), (400, 2000)):
            for initial, final in ((QPoint(0, 30), QPoint(60, 0)),
                                   (QPoint(30, 0), QPoint(0, 60)),
                                   (QPoint(18, 24), QPoint(48, 36))):
                with self.subTest(image=size, initial=initial, final=final):
                    image = QImage(*size, QImage.Format.Format_RGB32)
                    image.fill('#334455')
                    self.viewer.set_photo(image, self.video)
                    center = self.map.mapTo(self.controls, self.map.rect().center())
                    self.touch.press(0, center - initial, self.target).press(1, center + initial, self.target).commit()
                    self.touch.move(0, center - final, self.target).move(1, center + final, self.target).commit()
                    try:
                        self.assertAlmostEqual(self.viewer.zoom_factor(), 2, delta=.03)
                    finally:
                        self.touch.release(0, center - final, self.target).release(1, center + final, self.target).commit()
                    self.assertEqual(self.player.play_calls, 0)
                    self.assertIsNone(self.viewer._input_owner)

    def test_portrait_fit_pinch_centers_fitting_axis_and_preserves_scrollable_axis_anchor(self):
        # Native preview client geometry from the Windows source gate. At 2x
        # this portrait is still narrower than the viewport, so X must center.
        self.viewer.resize(1000, 720)
        self.controls.resize(320, 500)
        self.app.processEvents()
        portrait = QImage(420, 640, QImage.Format.Format_RGB32)
        portrait.fill('#334455')
        self.viewer.set_photo(portrait, self.video)
        # The full touchpad now maps to the photo, so use a clear off-center
        # displacement rather than the old letterboxed-map pixel distance.
        center = self.center() + QPoint(20, -15)
        mapped = self.map.map_to_viewer(QPointF(self.map.mapFrom(self.controls, center)))
        subject = self.viewer.mapToScene(mapped.toPoint())
        first, second = center - QPoint(20, 0), center + QPoint(20, 0)
        self.touch.press(0, first, self.target).commit()
        self.touch.stationary(0).press(1, second, self.target).commit()
        self.touch.move(0, first - QPoint(20, 0), self.target).move(1, second + QPoint(20, 0), self.target).commit()
        after = self.viewer.mapToScene(mapped.toPoint())
        image = self.viewer.viewportTransform().mapRect(self.viewer._image_rect)
        self.assertAlmostEqual(self.viewer.zoom_factor(), 2, delta=.03)
        self.assertLess(image.width(), self.viewer.viewport().width())
        self.assertLess(abs(image.center().x() - self.viewer.viewport().width() / 2), 1)
        self.assertGreater(abs(after.x() - subject.x()), 8)  # Off-center X anchoring is impossible here.
        self.assertLess(abs(after.y() - subject.y()), 2)
        self.touch.release(0, first - QPoint(20, 0), self.target).release(1, second + QPoint(20, 0), self.target).commit()
        self.assertEqual(self.player.play_calls, 0)

    def test_portrait_pinch_preserves_noncentral_two_axis_anchor_when_both_axes_can_pan(self):
        self.viewer.resize(1000, 720)
        self.controls.resize(320, 500)
        self.app.processEvents()
        portrait = QImage(420, 640, QImage.Format.Format_RGB32)
        portrait.fill('#334455')
        self.viewer.set_photo(portrait, self.video)
        self.viewer._zoom_at(2.7, self.viewer._viewport_center())
        image = self.viewer.viewportTransform().mapRect(self.viewer._image_rect)
        self.assertGreater(image.width(), self.viewer.viewport().width())
        self.assertGreater(image.height(), self.viewer.viewport().height())
        center = self.center() + QPoint(20, -15)
        mapped = self.map.map_to_viewer(QPointF(self.map.mapFrom(self.controls, center)))
        self.assertGreater(abs(mapped.x() - self.viewer._viewport_center().x()), 10)
        self.assertGreater(abs(mapped.y() - self.viewer._viewport_center().y()), 10)
        subject = self.viewer.mapToScene(mapped.toPoint())
        first, second = center - QPoint(20, 0), center + QPoint(20, 0)
        self.touch.press(0, first, self.target).commit()
        self.touch.stationary(0).press(1, second, self.target).commit()
        self.touch.move(0, first - QPoint(20, 0), self.target).move(1, second + QPoint(20, 0), self.target).commit()
        after = self.viewer.mapToScene(mapped.toPoint())
        self.assertAlmostEqual(self.viewer.zoom_factor(), 5.4, delta=.05)
        self.assertLess((after - subject).manhattanLength(), 7)
        self.touch.release(0, first - QPoint(20, 0), self.target).release(1, second + QPoint(20, 0), self.target).commit()
        self.assertEqual(self.player.play_calls, 0)

    def test_raw_second_contact_pause_cannot_play_and_no_drag_double_tap_zooms(self):
        point = self.center()
        self.viewer._zoom_at(2, self.viewer._viewport_center())
        scale, before = self.viewer.transform().m11(), self.viewer._scene_center()
        self.touch.press(0, point, self.target).commit()
        self.touch.release(0, point, self.target).commit()
        self.touch.press(0, point + QPoint(2, 0), self.target).commit()
        QTest.qWait(240)
        self.assertEqual(self.player.play_calls, 0)
        self.assertFalse(self.viewer._hold_timer.isActive())
        self.touch.move(0, point + QPoint(45, 10), self.target).commit()
        self.touch.release(0, point + QPoint(45, 10), self.target).commit()
        self.assertEqual(self.viewer.transform().m11(), scale)
        self.assertGreater((self.viewer._scene_center() - before).manhattanLength(), 1)
        self.viewer.fit_image()
        for _ in range(2):
            self.touch.press(0, point, self.target).commit()
            self.touch.release(0, point, self.target).commit()
        self.wait(lambda: not self.viewer.is_zoom_animating())
        self.assertAlmostEqual(self.viewer.zoom_factor(), 2, delta=.01)
        self.assertEqual(self.player.play_calls, 0)

    def test_direct_and_map_input_cannot_steal_each_others_active_gesture(self):
        point = self.center()
        direct_target = self.viewer.windowHandle()
        direct = QTest.touchEvent(direct_target, QTest.createTouchDevice(), False)
        direct_point = self.viewer.viewport().mapTo(self.viewer, self.viewer.viewport().rect().center())
        self.touch.press(0, point, self.target).commit()
        wheel_point = self.viewer._viewport_center()
        wheel = QWheelEvent(wheel_point, QPointF(self.viewer.viewport().mapToGlobal(wheel_point.toPoint())),
                            QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.viewer.viewport(), wheel)
        self.assertAlmostEqual(self.viewer.zoom_factor(), 1)
        direct.press(1, direct_point, direct_target).commit()
        direct.release(1, direct_point, direct_target).commit()
        self.assertIs(self.viewer._input_owner, self.map)
        self.wait(lambda: self.player.play_calls == 1)
        self.touch.release(0, point, self.target).commit()
        self.viewer.activateWindow()
        self.viewer.setFocus()
        QCoreApplication.processEvents()
        direct.press(1, direct_point, direct_target).commit()
        self.touch.press(0, point, self.target).commit()
        self.touch.release(0, point, self.target).commit()
        self.assertIs(self.viewer._input_owner, self.viewer._DIRECT_INPUT)
        self.wait(lambda: self.player.play_calls == 2)
        direct.release(1, direct_point, direct_target).commit()

    def test_source_change_resize_hide_and_cancel_discard_late_releases(self):
        point = self.center()
        changes = (lambda: self.viewer.set_photo(self.image, self.video),
                   lambda: self.viewer.replace_photo(self.image.scaled(1200, 900)),
                   lambda: self.viewer.resize(810, 600),
                   lambda: QApplication.sendEvent(self.viewer, QEvent(QEvent.Type.DevicePixelRatioChange)),
                   lambda: self.map.resize(self.map.width() + 1, self.map.height()),
                   self.map.hide, self.map.cancel_interaction)
        for change in changes:
            with self.subTest(change=change):
                self.map.show()
                self.touch.press(0, point, self.target).commit()
                self.assertIs(self.viewer._input_owner, self.map)
                change()
                QCoreApplication.processEvents()
                self.assertIsNone(self.viewer._input_owner)
                self.touch.release(0, point, self.target).commit()
                QTest.qWait(140)
                self.assertEqual(self.player.play_calls, 0)
                self.assertFalse(self.viewer.is_zoom_animating())
                self.assertFalse(self.viewer._click_timer.isActive())

    def test_internal_focus_switch_preserves_hold_but_external_and_modal_cancel(self):
        point = self.center()
        self.touch.press(0, point, self.target).commit()
        self.viewer.activateWindow()
        self.viewer.setFocus()
        QCoreApplication.processEvents()
        self.wait(lambda: self.player.play_calls == 1)
        self.controls.activateWindow()
        self.edit.setFocus()
        QCoreApplication.processEvents()
        self.assertTrue(self.viewer._play_requested)
        self.touch.release(0, point, self.target).commit()
        outsider = QWidget()
        try:
            self.touch.press(0, point, self.target).commit()
            outsider.show()
            outsider.activateWindow()
            QCoreApplication.processEvents()
            self.viewer.defer_group_focus_check()
            QCoreApplication.processEvents()
            self.assertIsNone(self.viewer._input_owner)
            self.assertFalse(self.viewer._hold_timer.isActive())
            self.touch.release(0, point, self.target).commit()
        finally:
            outsider.close()
        self.controls.activateWindow()
        QCoreApplication.processEvents()
        modal = QDialog(self.controls)
        modal.setModal(True)
        try:
            self.touch.press(0, point, self.target).commit()
            modal.show()
            self.viewer.defer_group_focus_check()
            QCoreApplication.processEvents()
            self.assertIsNone(self.viewer._input_owner)
            self.touch.release(0, point, self.target).commit()
        finally:
            modal.close()

    def test_preview_navigator_has_no_hidden_touchpad_hitbox(self):
        self.viewer._zoom_at(3, self.viewer._viewport_center())
        self.viewer._pan_by(QPointF(120, 80))
        navigator = self.viewer.zoom_navigator
        self.assertTrue(navigator.isVisible())
        self.assertTrue(self.map.navigator_rect().isEmpty())
        before = self.viewer._scene_center()
        local = self.map.map_from_viewer(QPointF(navigator.geometry().center()))
        point = self.map.mapTo(self.controls, local.toPoint())
        with patch.object(self.viewer, '_navigate_from_thumbnail', side_effect=AssertionError('Hidden navigator')):
            self.touch.press(0, point, self.target).commit()
            self.assertEqual(before, self.viewer._scene_center())
            self.wait(lambda: self.player.play_calls == 1)
            self.touch.move(0, point + QPoint(8, 12), self.target).commit()
        self.touch.release(0, point + QPoint(8, 12), self.target).commit()
        self.assertGreater((self.viewer._scene_center() - before).manhattanLength(), 1)
        self.assertEqual(self.player.play_calls, 1)
        self.assertFalse(self.viewer._play_requested)

    def test_every_corner_and_edge_plays_even_when_preview_has_black_bars(self):
        for width, height in ((421, 641), (1801, 419)):
            portrait = QImage(width, height, QImage.Format.Format_RGB32)
            portrait.fill('#cc5533')
            self.viewer.set_photo(portrait, self.video)
            rect = self.map.rect()
            points = (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight(),
                      QPoint(rect.center().x(), 0), QPoint(rect.center().x(), rect.bottom()),
                      QPoint(0, rect.center().y()), QPoint(rect.right(), rect.center().y()))
            for local in points:
                with self.subTest(image=(width, height), point=local):
                    # Separate contacts exercise holds, not intentionally recognized double taps.
                    self.viewer.cancel_interaction()
                    self.viewer._last_tap_time = self.viewer._last_tap_timestamp = 0
                    before = self.player.play_calls
                    self.assertTrue(self.map.image_contains_viewport_point(QPointF(local)))
                    point = self.map.mapTo(self.controls, local)
                    self.touch.press(0, point, self.target).commit()
                    self.wait(lambda: self.player.play_calls == before + 1)
                    self.touch.release(0, point, self.target).commit()
                    self.assertFalse(self.viewer._play_requested)
                    self.assertIsNone(self.viewer._input_owner)

    def test_wheel_uses_mapped_anchor(self):
        point = self.map.map_from_viewer(QPointF(470, 260))
        mapped = self.map.map_to_viewer(point)
        subject = self.viewer.mapToScene(mapped.toPoint())
        event = QWheelEvent(point, QPointF(self.map.mapToGlobal(point.toPoint())), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.map, event)
        self.assertGreater(self.viewer.zoom_factor(), 1)
        self.assertLess((subject - self.viewer.mapToScene(mapped.toPoint())).manhattanLength(), 7)

    def test_letterboxed_preview_holds_play_but_physical_right_click_opens_menu(self):
        portrait = QImage(800, 1600, QImage.Format.Format_RGB32)
        portrait.fill('#cc5533')
        self.viewer.set_photo(portrait, self.video)
        point = self.map.mapTo(self.controls, QPoint(1, self.map.height() // 2))
        global_point = self.controls.mapToGlobal(point)
        menus = QSignalSpy(self.map.contextMenuRequested)
        self.touch.press(0, point, self.target).commit()
        self.wait(lambda: self.player.play_calls == 1)
        local = self.map.mapFrom(self.controls, point)
        QApplication.sendEvent(self.map, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local, global_point))
        self.assertEqual(menus.count(), 0)
        self.touch.release(0, point, self.target).commit()
        self.assertFalse(self.viewer._play_requested)
        QTest.mousePress(self.map, Qt.MouseButton.RightButton, pos=local)
        QApplication.sendEvent(self.map, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local, global_point))
        QTest.mouseRelease(self.map, Qt.MouseButton.RightButton, pos=local)
        self.assertEqual(menus.count(), 1)
        self.assertEqual(menus.at(0)[0], global_point)

    def test_mouse_hold_and_second_contact_drag_reuse_guard_without_preview_focus(self):
        point = self.map.map_from_viewer(self.viewer._viewport_center()).toPoint()
        QTest.mousePress(self.map, Qt.MouseButton.LeftButton, pos=point)
        self.wait(lambda: self.player.play_calls == 1)
        self.assertIs(QApplication.focusWidget(), self.edit)
        QTest.mouseRelease(self.map, Qt.MouseButton.LeftButton, pos=point)
        self.assertFalse(self.viewer._play_requested)
        self.viewer._zoom_at(2, self.viewer._viewport_center())
        scale, center = self.viewer.transform().m11(), self.viewer._scene_center()

        def send(kind, position, timestamp):
            event = QMouseEvent(kind, QPointF(position), QPointF(self.map.mapToGlobal(position)),
                                Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton,
                                Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
            event.setTimestamp(timestamp)
            QApplication.sendEvent(self.map, event)

        send(QEvent.Type.MouseButtonPress, point, 1000)
        send(QEvent.Type.MouseButtonRelease, point, 1020)
        send(QEvent.Type.MouseButtonPress, point, 1100)
        QTest.qWait(230)
        self.assertEqual(self.player.play_calls, 1)
        self.assertFalse(self.viewer._hold_timer.isActive())
        send(QEvent.Type.MouseMove, point + QPoint(30, 10), 1340)
        send(QEvent.Type.MouseButtonRelease, point + QPoint(30, 10), 1360)
        self.assertEqual(self.viewer.transform().m11(), scale)
        self.assertGreater((self.viewer._scene_center() - center).manhattanLength(), 1)
        self.assertIs(QApplication.focusWidget(), self.edit)

    def test_native_tap_drag_uses_mapping_window_permission_without_first_click(self):
        self.viewer._zoom_at(2, self.viewer._viewport_center())
        point = self.map.map_from_viewer(self.viewer._viewport_center()).toPoint()
        scale, center = self.viewer.transform().m11(), self.viewer._scene_center()
        permission = MousePressAuthorization(False)
        calls = []

        def authorize(event, hwnd):
            calls.append((hwnd, event.timestamp(), event.globalPosition()))
            return permission

        monitor = SimpleNamespace(authorize=authorize, is_current=lambda token: token is permission)

        def send(kind, position, timestamp):
            event = QMouseEvent(kind, QPointF(position), QPointF(self.map.mapToGlobal(position)),
                                Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton,
                                Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
            event.setTimestamp(timestamp)
            QApplication.sendEvent(self.map, event)

        with patch.object(self.viewer, '_native_pointer_input', monitor, create=True):
            self.assertEqual(self.viewer._last_mouse_click_time, 0)
            send(QEvent.Type.MouseButtonPress, point, 18488031)
            QTest.qWait(180)
            self.assertEqual(self.player.play_calls, 0)
            self.assertFalse(self.viewer._hold_timer.isActive())
            self.viewer._hold_timeout()
            self.assertEqual(self.player.play_calls, 0)
            send(QEvent.Type.MouseMove, point + QPoint(30, 10), 18488500)
            send(QEvent.Type.MouseButtonRelease, point + QPoint(30, 10), 18489812)
            self.assertEqual(self.player.play_calls, 0)
            self.assertEqual(self.viewer.transform().m11(), scale)
            self.assertGreater((self.viewer._scene_center() - center).manhattanLength(), 1)
            self.assertIs(QApplication.focusWidget(), self.edit)
            self.assertEqual(calls, [(int(self.controls.winId()), 18488031,
                                     QPointF(self.map.mapToGlobal(point)))])
            self.assertNotEqual(int(self.controls.winId()), int(self.viewer.winId()))
            self.assertIsNone(self.viewer._input_owner)
            self.assertFalse(self.map._active or self.viewer._play_requested)

    def test_adding_second_finger_then_lifting_one_cannot_restart_hold_or_swipe(self):
        point = self.center()
        self.touch.press(0, point, self.target).commit()
        self.touch.stationary(0).press(1, point + QPoint(40, 0), self.target).commit()
        self.touch.stationary(0).release(1, point + QPoint(40, 0), self.target).commit()
        QTest.qWait(230)
        self.assertEqual(self.player.play_calls, 0)
        self.assertFalse(self.viewer._hold_timer.isActive())
        navigated = QSignalSpy(self.viewer.navigate_requested)
        self.touch.move(0, point + QPoint(-150, 0), self.target).commit()
        self.touch.release(0, point + QPoint(-150, 0), self.target).commit()
        self.assertEqual(navigated.count(), 0)
        self.assertFalse(self.viewer.is_zoom_animating())

    def test_touch_history_from_direct_preview_cannot_turn_map_hold_into_second_tap(self):
        self.viewer._zoom_at(2, self.viewer._viewport_center())
        target = self.viewer.windowHandle()
        sequence = QTest.touchEvent(target, self.device, False)
        point = self.viewer.viewport().mapTo(self.viewer, self.viewer.viewport().rect().center())
        sequence.press(0, point, target).commit()
        sequence.release(0, point, target).commit()
        remote = self.center()
        self.touch.press(0, remote, self.target).commit()
        self.wait(lambda: self.player.play_calls == 1)
        self.touch.release(0, remote, self.target).commit()
        self.assertFalse(self.viewer.is_zoom_animating())

    def test_continuous_pinch_freezes_mapping_then_next_gesture_uses_new_crop(self):
        portrait = QImage(421, 641, QImage.Format.Format_RGB32)
        portrait.fill('#cc5533')
        self.viewer.set_photo(portrait, self.video)
        local = QPointF(self.map.rect().center()) + QPointF(12, -20)
        center = self.map.mapTo(self.controls, local.toPoint())
        first, second = center - QPoint(20, 0), center + QPoint(20, 0)
        before = self.map.map_to_viewer(local)
        self.touch.press(0, first, self.target).press(1, second, self.target).commit()
        for distance in (30, 40, 60):
            self.touch.move(0, center - QPoint(distance, 0), self.target).move(
                1, center + QPoint(distance, 0), self.target).commit()
            self.assertAlmostEqual(self.viewer.zoom_factor(), distance / 20, delta=.03)
            self.assertEqual(self.map.map_to_viewer(local), before)
            self.assertLess((self.map.map_from_viewer(before) - local).manhattanLength(), 1e-8)
        self.touch.release(0, center - QPoint(60, 0), self.target).release(
            1, center + QPoint(60, 0), self.target).commit()
        self.assertTrue(self.map._gesture_target_rect.isEmpty())
        self.assertGreater((self.map.map_to_viewer(local) - before).manhattanLength(), 1)
        self.assertEqual(self.player.play_calls, 0)

    def test_group_can_forget_a_destroyed_control_window(self):
        temporary = QWidget()
        self.viewer.set_window_group([self.viewer, self.controls, temporary])
        temporary.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.viewer.set_window_group([self.viewer, self.controls])
        self.assertTrue(self.viewer.is_window_group_active())

    def test_selection_change_during_playback_stop_cannot_rearm_old_contact(self):
        self.viewer.start_live()
        self.viewer._video_frame(self.frame_for_reentrant_test(), self.viewer._generation)
        changed = []

        def select_another_photo(playing):
            if not playing and not changed:
                changed.append(True)
                replacement = QImage(800, 600, QImage.Format.Format_RGB32)
                replacement.fill('blue')
                self.viewer.set_photo(replacement, self.video)

        self.viewer.playback_changed.connect(select_another_photo)
        point = self.center()
        self.touch.press(0, point, self.target).commit()
        self.assertTrue(changed)
        self.assertIsNone(self.viewer._input_owner)
        self.assertFalse(self.viewer._hold_timer.isActive())
        self.touch.release(0, point, self.target).commit()
        QTest.qWait(150)
        self.assertEqual(self.player.play_calls, 1)

    def frame_for_reentrant_test(self):
        return QVideoFrame(self.image.scaled(320, 240))

    def test_hidden_unavailable_preview_cannot_play_its_old_photo_from_map(self):
        point = self.center()
        self.viewer.hide()
        self.touch.press(0, point, self.target).commit()
        QTest.qWait(150)
        self.touch.release(0, point, self.target).commit()
        self.assertIsNone(self.viewer._input_owner)
        self.assertEqual(self.player.play_calls, 0)
        output = self.map.grab().toImage()
        local = self.map.map_from_viewer(self.viewer._viewport_center())
        ratio = self.map.devicePixelRatioF()
        self.assertEqual(output.pixelColor(round(local.x() * ratio), round(local.y() * ratio)),
                         QColor(self.map._colors['surface']))

    def test_mapped_horizontal_wheel_always_navigates_at_fit_and_obeys_zoomed_setting(self):
        navigation = []
        self.viewer.navigate_requested.connect(navigation.append)
        point = self.map.map_from_viewer(self.viewer._viewport_center())

        def wheel(delta):
            event = QWheelEvent(point, QPointF(self.map.mapToGlobal(point.toPoint())), QPoint(), QPoint(delta, 0),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(self.map, event)

        self.viewer.set_options(horizontal_scroll_mode='pan')
        wheel(-120)
        self.assertEqual(navigation, [1])
        self.viewer._zoom_at(2, self.viewer._viewport_center())
        before = self.viewer._scene_center()
        wheel(-120)
        self.assertGreater(self.viewer._scene_center().x(), before.x())
        self.assertEqual(navigation, [1])
        self.viewer.set_options(horizontal_scroll_mode='navigate')
        wheel(120)
        self.assertEqual(navigation, [1, -1])


if __name__ == '__main__':
    unittest.main()
