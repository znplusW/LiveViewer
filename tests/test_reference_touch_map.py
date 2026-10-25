"""Real-window mapped input, without claims about physical touch hardware."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QImage, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMenu, QVBoxLayout, QWidget

from livegallery.reference_touch_map import ReferenceTouchMap
from livegallery.viewer import ViewerCanvas
from tests.test_viewer import FakePlayer


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

    def test_reference_matches_current_crop_black_bars_and_bounded_photo_cache(self):
        portrait = QImage(800, 1600, QImage.Format.Format_RGB32)
        portrait.fill('#cc5533')
        self.viewer.set_photo(portrait, self.video)
        self.map._refresh()
        area = self.map.mapped_rect()
        self.assertAlmostEqual(area.width() / area.height(), 800 / 600)
        output = self.map.grab().toImage()
        middle = self.map.map_from_viewer(QPointF(400, 300)).toPoint()
        blank = self.map.map_from_viewer(QPointF(30, 300)).toPoint()
        ratio = self.map.devicePixelRatioF()
        self.assertEqual(output.pixelColor(round(middle.x() * ratio), round(middle.y() * ratio)).name(), '#cc5533')
        self.assertEqual(output.pixelColor(round(blank.x() * ratio), round(blank.y() * ratio)), self.viewer.backgroundBrush().color())
        self.assertFalse(self.map.image_contains_viewport_point(QPointF(blank)))
        self.assertTrue(self.map.image_contains_viewport_point(QPointF(middle)))
        revision = self.map.thumbnail_revision
        key = self.map._thumbnail.cacheKey()
        self.assertLessEqual(max(self.map._thumbnail.width(), self.map._thumbnail.height()), 1024)
        with patch('builtins.open', side_effect=AssertionError('Reference reread a source file')):
            self.viewer._zoom_at(2, QPointF(400, 300))
            for _ in range(15):
                self.viewer._pan_by(QPointF(3, 2))
                self.map._refresh()
            self.map.grab()
        self.assertEqual(self.map.thumbnail_revision, revision)
        self.assertEqual(self.map._thumbnail.cacheKey(), key)
        self.viewer.start_live()
        frame = QImage(320, 240, QImage.Format.Format_RGB32)
        frame.fill('green')
        for _ in range(8):
            self.viewer._video_frame(QVideoFrame(frame), self.viewer._generation)
            self.map._refresh()
        self.assertEqual(self.map.thumbnail_revision, revision)
        self.assertEqual(self.map._thumbnail.toImage().pixelColor(10, 10).name(), '#cc5533')

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

    def test_portrait_fit_pinch_centers_fitting_axis_and_preserves_scrollable_axis_anchor(self):
        # Native preview client geometry from the Windows source gate. At 2x
        # this portrait is still narrower than the viewport, so X must center.
        self.viewer.resize(1000, 720)
        self.controls.resize(320, 500)
        self.app.processEvents()
        portrait = QImage(420, 640, QImage.Format.Format_RGB32)
        portrait.fill('#334455')
        self.viewer.set_photo(portrait, self.video)
        center = self.center() + QPoint(8, -5)
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
        center = self.center() + QPoint(8, -5)
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

    def test_navigation_overlay_is_reachable_from_the_map(self):
        self.viewer._zoom_at(3, self.viewer._viewport_center())
        self.viewer._pan_by(QPointF(120, 80))
        area = self.map.navigator_rect()
        self.assertFalse(area.isEmpty())
        before = self.viewer._scene_center()
        point = self.map.mapTo(self.controls, (area.topLeft() + QPointF(4, 4)).toPoint())
        self.touch.press(0, point, self.target).commit()
        self.touch.move(0, point + QPoint(8, 12), self.target).commit()
        self.touch.release(0, point + QPoint(8, 12), self.target).commit()
        self.assertGreater((self.viewer._scene_center() - before).manhattanLength(), 1)
        self.assertEqual(self.player.play_calls, 0)

    def test_padding_is_not_an_input_surface_and_wheel_uses_mapped_anchor(self):
        padding = self.map.mapTo(self.controls, QPoint(1, 1))
        self.touch.press(0, padding, self.target).commit()
        QTest.qWait(160)
        self.touch.release(0, padding, self.target).commit()
        self.assertIsNone(self.viewer._input_owner)
        self.assertEqual(self.player.play_calls, 0)
        point = self.map.map_from_viewer(QPointF(470, 260))
        event = QWheelEvent(point, QPointF(self.map.mapToGlobal(point.toPoint())), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.map, event)
        self.assertGreater(self.viewer.zoom_factor(), 1)

    def test_blank_hold_menu_uses_map_finger_and_original_release_keeps_popup(self):
        portrait = QImage(800, 1600, QImage.Format.Format_RGB32)
        portrait.fill('#cc5533')
        self.viewer.set_photo(portrait, self.video)
        point = self.point(QPoint(80, 300))
        global_point = self.controls.mapToGlobal(point)
        results, errors = {}, []

        def popup(position):
            results['position'] = position
            self.viewer.stop_live()  # MainWindow cancels before the real menu.exec.
            menu = QMenu(self.controls)
            menu.addAction('当前图片测试项')

            def release():
                try:
                    self.touch.release(0, point, self.target).commit()
                    results['visible_after_release'] = menu.isVisible()
                except Exception as error:
                    errors.append(error)
                QTimer.singleShot(40, menu.close)

            QTimer.singleShot(160, release)
            menu.exec(position)

        self.map.contextMenuRequested.connect(popup)
        hints = QApplication.styleHints()
        with patch.object(hints, 'mousePressAndHoldInterval', return_value=130):
            self.touch.press(0, point, self.target).commit()
            self.wait(lambda: 'visible_after_release' in results, 2)
        self.assertFalse(errors)
        self.assertEqual(results['position'], global_point)
        self.assertTrue(results['visible_after_release'])
        self.assertEqual(self.player.play_calls, 0)

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

    def test_reference_crop_samples_match_scene_under_zoom_and_pan(self):
        image = QImage(2400, 1800, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        for x, y, color in ((0, 0, '#ee2233'), (1200, 0, '#22bb44'),
                             (0, 900, '#3344ee'), (1200, 900, '#eebb33')):
            painter.fillRect(x, y, 1200, 900, QColor(color))
        painter.end()
        self.viewer.set_photo(image, self.video)
        self.viewer._zoom_at(2.5, QPointF(300, 200))
        self.viewer._pan_by(QPointF(-80, -70))
        output, ratio = self.map.grab().toImage(), self.map.devicePixelRatioF()
        for point in (QPoint(200, 160), QPoint(500, 180), QPoint(210, 410)):
            source = self.viewer.mapToScene(point).toPoint()
            local = self.map.map_from_viewer(QPointF(point))
            actual = output.pixelColor(round(local.x() * ratio), round(local.y() * ratio))
            self.assertEqual(actual, image.pixelColor(source))

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
                         self.viewer.backgroundBrush().color())

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
