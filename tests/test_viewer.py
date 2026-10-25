"""Canvas input tests; these do not replace testing on a physical touchscreen."""

import os
import ctypes
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QEvent, QPoint, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QContextMenuEvent, QEventPoint, QImage, QMouseEvent, QTouchEvent, QWheelEvent
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QtVideo
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMenu

from livegallery.viewer import ViewerCanvas
from livegallery.native_touch import (
    GC_ALLGESTURES, ImageTouchGestureFilter, TABLET_DISABLE_PRESSANDHOLD,
    WM_GESTURENOTIFY, WM_TABLET_QUERYSYSTEMGESTURESTATUS,
    _GestureConfig, _GestureNotify, _Message, _Point,
    native_client_to_viewport, signed_screen_point,
)


class FakePlayer:
    """Keep input tests independent of an operating system's media devices."""

    def __init__(self):
        self.url = QUrl()
        self.play_calls = 0
        self.stop_calls = 0

    def setSource(self, url):
        self.url = url

    def source(self):
        return self.url

    def setPosition(self, value):
        self.position = value

    def setLoops(self, value):
        self.loops = value

    def play(self):
        self.play_calls += 1

    def stop(self):
        self.stop_calls += 1


class CanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.video_path = Path(self.temporary.name) / "test.mov"
        self.video_path.touch()
        self.canvas = ViewerCanvas()
        self.canvas.resize(800, 600)
        self.canvas.show()
        self.canvas.activateWindow()
        self.canvas.setFocus()
        QTest.qWait(30)
        self.app.processEvents()
        self.canvas._player = self.player = FakePlayer()
        self.image = QImage(2400, 1800, QImage.Format.Format_RGB32)
        self.image.fill("#334455")
        self.canvas.set_photo(self.image, self.video_path)
        self.canvas.set_options(hold_ms=100)
        self.center = QPoint(400, 300)
        self.touch_device = QTest.createTouchDevice()
        self.touch_sequence = QTest.touchEvent(self.canvas.viewport(), self.touch_device, False)

    def tearDown(self):
        self.canvas.close()
        self.canvas.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def frame(self):
        return QVideoFrame(self.image.scaled(320, 240))

    def wait_for_play_calls(self, count):
        # Windows runners can deliver a GUI timer after its nominal deadline.
        # Pump for a bounded interval without re-pressing or forcing the timer;
        # an actual lost hold/focus cancellation still fails with diagnostics.
        deadline = time.monotonic() + 1.5
        while self.player.play_calls < count and time.monotonic() < deadline:
            QTest.qWait(10)
        active = QApplication.activeWindow()
        self.assertEqual(
            self.player.play_calls, count,
            f"hold timer active={self.canvas._hold_timer.isActive()}, "
            f"mouse_down={self.canvas._mouse_down}, consumed={self.canvas._hold_consumed}, "
            f"requested={self.canvas._play_requested}, "
            f"active_window_is_canvas={active is self.canvas}",
        )

    def finish_zoom_animation(self):
        deadline = time.monotonic() + 1.5
        while self.canvas.is_zoom_animating() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.canvas.is_zoom_animating(), "zoom animation did not finish")

    def touch(self, event_type, *points):
        if event_type == QEvent.Type.TouchCancel:
            QApplication.sendEvent(
                self.canvas.viewport(), QTouchEvent(event_type, self.touch_device)
            )
            return
        sequence = self.touch_sequence
        for point_id, state, x, y in points:
            if state == QEventPoint.State.Stationary:
                sequence.stationary(point_id)
            else:
                method = {
                    QEventPoint.State.Pressed: sequence.press,
                    QEventPoint.State.Updated: sequence.move,
                    QEventPoint.State.Released: sequence.release,
                }[state]
                method(point_id, QPoint(x, y), self.canvas.viewport())
        sequence.commit()
        self.app.processEvents()

    def wheel(self, angle=(0, 0), pixels=(0, 0), phase=Qt.ScrollPhase.NoScrollPhase, inverted=False):
        event = QWheelEvent(
            QPointF(self.center), QPointF(self.canvas.viewport().mapToGlobal(self.center)),
            QPoint(*pixels), QPoint(*angle), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, phase, inverted,
        )
        QApplication.sendEvent(self.canvas.viewport(), event)
        return event

    def portrait_with_letterboxing(self):
        image = QImage(600, 1200, QImage.Format.Format_RGB32)
        image.fill("red")
        self.canvas.set_photo(image, self.video_path)
        self.assertFalse(self.canvas.image_contains_viewport_point(QPointF(20, 300)))
        return image

    def test_quick_release_never_starts_a_late_video(self):
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.qWait(25)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.qWait(120)
        self.assertEqual(self.player.play_calls, 0)
        self.assertTrue(self.canvas._photo.isVisible())

    def test_hold_waits_for_frame_then_release_immediately_restores_photo(self):
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.wait_for_play_calls(1)
        self.assertTrue(self.canvas._photo.isVisible())
        token = self.canvas._generation
        self.canvas._video_frame(self.frame(), token)
        self.assertTrue(self.canvas._video.isVisible())
        self.assertFalse(self.canvas._photo.isVisible())
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.assertFalse(self.canvas._play_requested)
        self.assertTrue(self.canvas._photo.isVisible())
        self.canvas._video_frame(self.frame(), token)
        self.assertFalse(self.canvas._video.isVisible())

    def test_single_play_requires_a_new_press(self):
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.wait_for_play_calls(1)
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        self.canvas._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        QTest.qWait(140)
        self.assertEqual(self.player.play_calls, 1)
        self.assertTrue(self.canvas._photo.isVisible())
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.wait_for_play_calls(2)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)

    def test_temporary_full_play_restores_transform_and_center(self):
        self.canvas.zoom_in()
        self.canvas.zoom_in()
        self.canvas._pan_by(QPointF(-70, 30))
        before = self.canvas.transform()
        center = self.canvas._scene_center()
        self.canvas.set_options(preserve_zoom=False)
        self.canvas.start_live()
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0)
        self.canvas.stop_live()
        self.assertEqual(self.canvas.transform(), before)
        self.assertLess((self.canvas._scene_center() - center).manhattanLength(), 5)

    def test_preserved_zoom_and_photo_change_invalidates_previous_frame(self):
        self.canvas.zoom_in()
        before = self.canvas.transform()
        self.canvas.start_live()
        token = self.canvas._generation
        self.canvas._video_frame(self.frame(), token)
        self.assertEqual(self.canvas.transform(), before)
        self.canvas.set_photo(self.image)
        self.canvas._video_frame(self.frame(), token)
        self.assertFalse(self.canvas._video.isVisible())
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0)

    def test_progressive_upgrade_preserves_relative_zoom_and_subject_center(self):
        self.canvas.zoom_in()
        self.canvas.zoom_in()
        self.canvas._pan_by(QPointF(60, -30))
        before_zoom = self.canvas.zoom_factor()
        old_center = self.canvas._scene_center()
        image = QImage(4800, 3600, QImage.Format.Format_RGB32)
        image.fill("#334455")
        self.canvas.replace_photo(image)
        center = self.canvas._scene_center()
        self.assertAlmostEqual(self.canvas.zoom_factor(), before_zoom)
        self.assertLess(abs(center.x() / 4800 - old_center.x() / 2400), 0.005)
        self.assertLess(abs(center.y() / 3600 - old_center.y() / 1800), 0.005)
        self.assertEqual(self.canvas._video_path, self.video_path)
        self.assertEqual(self.player.play_calls, 0)

    def test_mouse_double_click_animates_to_exactly_twice_fit_around_subject(self):
        position = QPoint(500, 340)
        subject = self.canvas.mapToScene(position)
        changed = QSignalSpy(self.canvas.zoom_animation_changed)
        QTest.mouseDClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=position)
        self.assertTrue(self.canvas.is_zoom_animating())
        self.canvas._zoom_animation.setCurrentTime(100)
        self.assertGreater(self.canvas.zoom_factor(), 1.0)
        self.assertLess(self.canvas.zoom_factor(), 2.0)
        self.finish_zoom_animation()
        self.assertAlmostEqual(self.canvas.zoom_factor(), 2.0, places=8)
        self.assertLess((self.canvas.mapFromScene(subject) - position).manhattanLength(), 4)
        self.assertEqual(changed.count(), 2)
        QTest.mouseDClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=position)
        self.canvas._zoom_animation.setCurrentTime(100)
        self.assertGreater(self.canvas.zoom_factor(), 1.0)
        self.assertLess(self.canvas.zoom_factor(), 2.0)
        self.finish_zoom_animation()
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0, places=8)
        self.assertTrue(self.canvas._fit_mode)

    def test_zoomed_trackpad_tap_then_held_second_press_never_starts_live(self):
        self.canvas._zoom_at(2.0, QPointF(self.center))
        scale = self.canvas.transform().m11()
        center = self.canvas._scene_center()
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        # Tap-to-drag can hold the second press still (or below drag distance)
        # longer than the user's Live hold delay before its first drag update.
        QTest.mouseMove(self.canvas.viewport(), self.center + QPoint(1, 0))
        QTest.qWait(180)
        self.assertEqual(self.player.play_calls, 0, 'Second tap briefly started Live playback')
        QTest.mouseMove(self.canvas.viewport(), self.center + QPoint(80, 25))
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                           pos=self.center + QPoint(80, 25))
        self.assertLess((self.canvas._scene_center() - center).x(), -20)
        self.assertAlmostEqual(self.canvas.transform().m11(), scale)
        self.assertFalse(self.canvas.is_zoom_animating())
        self.assertFalse(self.canvas._click_timer.isActive())

    def test_zoomed_qt_double_click_held_second_press_pans_without_live_or_zoom(self):
        self.canvas._zoom_at(2.0, QPointF(self.center))
        original = self.canvas.transform().m11()
        center = self.canvas._scene_center()
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        # Qt's standard sequence replaces the second press with DoubleClick.
        QTest.mouseDClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.qWait(180)
        self.assertEqual(self.player.play_calls, 0)
        self.assertFalse(self.canvas.is_zoom_animating())
        self.assertAlmostEqual(self.canvas.transform().m11(), original)
        QTest.mouseMove(self.canvas.viewport(), self.center + QPoint(-90, 40))
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                           pos=self.center + QPoint(-90, 40))
        self.assertGreater((self.canvas._scene_center() - center).x(), 20)
        self.assertAlmostEqual(self.canvas.transform().m11(), original)
        self.assertFalse(self.canvas.is_zoom_animating())

    def test_zoomed_double_click_release_still_animates_and_normal_hold_keeps_user_delay(self):
        self.canvas._zoom_at(2.0, QPointF(self.center))
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mouseDClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mouseMove(self.canvas.viewport(), self.center + QPoint(1, 0))
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton,
                           pos=self.center + QPoint(1, 0))
        self.assertTrue(self.canvas.is_zoom_animating())
        self.canvas._zoom_animation.setCurrentTime(100)
        self.assertGreater(self.canvas.zoom_factor(), 1)
        self.assertLess(self.canvas.zoom_factor(), 2)
        self.finish_zoom_animation()
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1)
        self.assertEqual(self.player.play_calls, 0)
        self.canvas._zoom_at(2.0, QPointF(self.center))
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.assertEqual(self.canvas._hold_timer.interval(), 100)
        self.wait_for_play_calls(1)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)

    def test_double_drag_candidate_photo_change_cancels_late_zoom_and_touch_hold_still_plays(self):
        self.canvas._zoom_at(2.0, QPointF(self.center))
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.canvas.set_photo(self.image, self.video_path)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.assertFalse(self.canvas.is_zoom_animating())
        self.assertFalse(self.canvas._click_timer.isActive())
        self.assertEqual(self.player.play_calls, 0)
        self.canvas._zoom_at(2.0, QPointF(self.center))
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 400, 300))
        self.wait_for_play_calls(1)
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 400, 300))
        self.assertFalse(self.canvas._play_requested)

    def test_double_click_from_other_zoom_returns_fit_and_rapid_retarget_is_continuous(self):
        self.canvas.zoom_in()
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.finish_zoom_animation()
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0, places=8)
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.canvas._zoom_animation.setCurrentTime(100)
        before = self.canvas.zoom_factor()
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.assertAlmostEqual(self.canvas.zoom_factor(), before)
        self.assertEqual(self.canvas._zoom_target_factor, 1.0)
        self.canvas._zoom_animation.setCurrentTime(80)
        before = self.canvas.zoom_factor()
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.assertAlmostEqual(self.canvas.zoom_factor(), before)
        self.assertEqual(self.canvas._zoom_target_factor, 2.0)
        self.finish_zoom_animation()
        self.assertAlmostEqual(self.canvas.zoom_factor(), 2.0, places=8)

    def test_touch_double_tap_uses_same_smooth_twice_fit_toggle(self):
        for target in (2.0, 1.0):
            for _ in range(2):
                self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 400, 300))
                self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 400, 300))
            self.assertTrue(self.canvas.is_zoom_animating())
            self.canvas._zoom_animation.setCurrentTime(100)
            self.assertGreater(self.canvas.zoom_factor(), 1.0)
            self.assertLess(self.canvas.zoom_factor(), 2.0)
            self.finish_zoom_animation()
            self.assertAlmostEqual(self.canvas.zoom_factor(), target, places=8)
        self.assertEqual(self.player.play_calls, 0)

    def test_upgrade_waits_for_zoom_and_preserves_twice_fit_subject(self):
        position = QPoint(500, 340)
        subject = self.canvas.mapToScene(position)
        self.canvas._toggle_double_zoom(QPointF(position))
        self.canvas._zoom_animation.setCurrentTime(100)
        full = QImage(4800, 3600, QImage.Format.Format_RGB32)
        full.fill("#334455")
        self.canvas.replace_photo(full)
        self.assertEqual(self.canvas._photo.pixmap().width(), 2400)
        self.assertTrue(self.canvas.is_zoom_animating())
        self.finish_zoom_animation()
        self.assertEqual(self.canvas._photo.pixmap().width(), 4800)
        self.assertIsNone(self.canvas._deferred_photo)
        self.assertAlmostEqual(self.canvas.zoom_factor(), 2.0, places=8)
        self.assertLess((self.canvas.mapFromScene(subject * 2) - position).manhattanLength(), 5)

    def test_new_photo_discards_old_animation_and_deferred_upgrade(self):
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.canvas._zoom_animation.setCurrentTime(100)
        full = QImage(4800, 3600, QImage.Format.Format_RGB32)
        full.fill("red")
        self.canvas.replace_photo(full)
        new_image = QImage(800, 600, QImage.Format.Format_RGB32)
        new_image.fill("blue")
        self.canvas.set_photo(new_image)
        self.assertFalse(self.canvas.is_zoom_animating())
        self.assertIsNone(self.canvas._deferred_photo)
        QTest.qWait(250)
        self.assertEqual(self.canvas._photo.pixmap().size(), new_image.size())
        self.assertEqual(self.canvas._photo.pixmap().toImage().pixelColor(400, 300).name(), "#0000ff")
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0, places=8)

    def test_wheel_interrupts_animation_without_late_snap(self):
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.canvas._zoom_animation.setCurrentTime(100)
        before = self.canvas.zoom_factor()
        wheel = QWheelEvent(
            QPointF(self.center), QPointF(self.canvas.viewport().mapToGlobal(self.center)),
            QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(self.canvas.viewport(), wheel)
        self.assertFalse(self.canvas.is_zoom_animating())
        self.assertGreater(self.canvas.zoom_factor(), before)
        after = self.canvas.transform()
        QTest.qWait(250)
        self.assertEqual(self.canvas.transform(), after)

    def test_horizontal_wheel_accumulates_high_resolution_angle_and_limits_burst(self):
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        with patch("livegallery.viewer.time.monotonic") as clock:
            clock.return_value = 10.0
            for _ in range(3):
                self.wheel(angle=(-30, 0))
            self.assertEqual(navigation, [])
            self.wheel(angle=(-30, 0))
            self.assertEqual(navigation, [1])
            for _ in range(30):
                self.wheel(angle=(-120, 0))
            self.assertEqual(navigation, [1])
            clock.return_value = 10.2
            self.wheel(angle=(-12000, 0))
            self.assertEqual(navigation, [1, 1])
            clock.return_value = 10.4
            self.wheel(angle=(120, 0))
            self.assertEqual(navigation, [1, 1, -1])
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0)

    def test_horizontal_wheel_pixel_preference_direction_and_os_inversion(self):
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        with patch("livegallery.viewer.time.monotonic") as clock:
            clock.return_value = 10.0
            for _ in range(3):
                self.wheel(angle=(120, 0), pixels=(-15, 0), inverted=True)
            self.assertEqual(navigation, [])
            self.wheel(angle=(120, 0), pixels=(-15, 0), inverted=True)
            self.assertEqual(navigation, [1])
            clock.return_value = 10.3
            self.wheel(pixels=(60, 0), inverted=False)
            self.assertEqual(navigation, [1, -1])
            clock.return_value = 10.6
            self.wheel(pixels=(-60, 0), inverted=False)
            self.assertEqual(navigation, [1, -1, 1])

    def test_horizontal_wheel_resets_partial_gestures_on_end_idle_axis_and_reversal(self):
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        with patch("livegallery.viewer.time.monotonic") as clock:
            clock.return_value = 10.0
            self.wheel(angle=(-90, 0))
            self.wheel(phase=Qt.ScrollPhase.ScrollEnd)
            self.wheel(angle=(-30, 0))
            self.assertEqual(navigation, [])
            self.wheel(phase=Qt.ScrollPhase.ScrollBegin)
            self.wheel(angle=(-90, 0))
            clock.return_value = 10.5
            self.wheel(angle=(-30, 0))
            self.assertEqual(navigation, [])
            self.wheel(angle=(90, 0))  # reversal discards the previous -30
            self.assertEqual(navigation, [])
            self.wheel(angle=(30, 0))
            self.assertEqual(navigation, [-1])
            clock.return_value = 10.8
            self.wheel(angle=(-90, 0))
            self.wheel(angle=(0, 30))  # changing to vertical clears X residue
            self.wheel(angle=(-30, 0))
            self.assertEqual(navigation, [-1])
            self.wheel(pixels=(-45, 0))  # unit change must also clear residue
            self.assertEqual(navigation, [-1])
            self.wheel(pixels=(-15, 0), phase=Qt.ScrollPhase.ScrollEnd)
            self.assertEqual(navigation, [-1, 1])
        self.assertEqual(self.canvas._horizontal_wheel_total, 0.0)

    def test_horizontal_navigation_rate_survives_loading_next_photo(self):
        navigation = []

        def navigate(step):
            navigation.append(step)
            self.canvas.set_photo(self.image, self.video_path)

        self.canvas.navigate_requested.connect(navigate)
        with patch("livegallery.viewer.time.monotonic") as clock:
            clock.return_value = 10.0
            self.wheel(angle=(-120, 0))
            clock.return_value = 10.01
            self.wheel(angle=(-120, 0))
            self.assertEqual(navigation, [1])
            clock.return_value = 10.2
            self.wheel(angle=(-120, 0))
            self.assertEqual(navigation, [1, 1])

    def test_horizontal_axis_dominance_does_not_zoom_and_vertical_still_zooms(self):
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        self.wheel(angle=(-120, 10))
        self.assertEqual(navigation, [1])
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0)
        self.wheel(angle=(10, 120))
        self.assertGreater(self.canvas.zoom_factor(), 1.0)
        self.assertEqual(navigation, [1])
        before = self.canvas.zoom_factor()
        self.wheel(pixels=(1, 20))
        self.assertGreater(self.canvas.zoom_factor(), before)

    def test_horizontal_pan_mode_moves_zoomed_image_and_consumes_at_fit(self):
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        self.canvas.set_options(horizontal_scroll_mode="pan")
        before = self.canvas._scene_center()
        self.assertTrue(self.wheel(angle=(-120, 0)).isAccepted())
        self.assertEqual(self.canvas._scene_center(), before)
        self.assertEqual(navigation, [])
        self.canvas.zoom_in()
        self.canvas.zoom_in()
        before = self.canvas._scene_center()
        factor = self.canvas.zoom_factor()
        self.wheel(angle=(-120, 5))
        self.assertGreater(self.canvas._scene_center().x(), before.x())
        self.assertAlmostEqual(self.canvas._scene_center().y(), before.y(), delta=3)
        self.assertAlmostEqual(self.canvas.zoom_factor(), factor)
        before = self.canvas._scene_center()
        self.wheel(pixels=(30, 0), inverted=True)
        self.assertLess(self.canvas._scene_center().x(), before.x())
        self.assertEqual(navigation, [])
        self.canvas.set_options(horizontal_scroll_mode="invalid")
        self.wheel(angle=(-120, 0))
        self.assertEqual(navigation, [1])

    def test_empty_canvas_does_not_navigate_from_horizontal_wheel(self):
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        self.canvas.clear()
        self.wheel(angle=(-120, 0))
        self.wheel(pixels=(100, 0))
        self.assertEqual(navigation, [])

    def test_pan_pinch_resize_hide_and_play_cancel_zoom_animation(self):
        def begin():
            self.canvas.fit_image()
            self.canvas._toggle_double_zoom(QPointF(self.center))
            self.canvas._zoom_animation.setCurrentTime(100)
            self.assertTrue(self.canvas.is_zoom_animating())

        begin()
        self.canvas._pan_by(QPointF(10, 0))
        self.assertFalse(self.canvas.is_zoom_animating())
        begin()
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 300, 300))
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Stationary, 300, 300),
                   (1, QEventPoint.State.Pressed, 500, 300))
        self.assertFalse(self.canvas.is_zoom_animating())
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, 250, 300),
                   (1, QEventPoint.State.Updated, 550, 300))
        self.assertFalse(self.canvas.is_zoom_animating())
        self.touch(QEvent.Type.TouchCancel)
        begin()
        self.canvas.resize(700, 500)
        self.app.processEvents()
        self.assertFalse(self.canvas.is_zoom_animating())
        begin()
        self.canvas.hide()
        self.assertFalse(self.canvas.is_zoom_animating())
        self.canvas.show()
        begin()
        before = self.canvas.transform()
        self.canvas.start_live()
        self.assertFalse(self.canvas.is_zoom_animating())
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        self.assertEqual(self.canvas.transform(), before)
        QTest.qWait(250)
        self.assertEqual(self.canvas.transform(), before)

    def test_initial_mouse_or_touch_drag_cancels_even_first_fit_animation_frame(self):
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.assertAlmostEqual(self.canvas.zoom_factor(), 1.0)
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mouseMove(self.canvas.viewport(), QPoint(450, 300))
        self.assertFalse(self.canvas.is_zoom_animating())
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(450, 300))
        self.canvas.fit_image()
        self.canvas._toggle_double_zoom(QPointF(self.center))
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 400, 300))
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, 450, 300))
        self.assertFalse(self.canvas.is_zoom_animating())
        self.touch(QEvent.Type.TouchCancel)

    def test_touch_swipe_navigates_at_fit_but_pans_when_zoomed(self):
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 600, 300))
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, 300, 310))
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 200, 310))
        self.assertEqual(navigation, [1])
        self.canvas.zoom_in()
        before = self.canvas._scene_center()
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 600, 300))
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, 300, 310))
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 200, 310))
        self.assertEqual(navigation, [1])
        self.assertNotEqual(self.canvas._scene_center(), before)

    def test_blank_area_swipes_both_directions_without_live_play_or_taps(self):
        self.portrait_with_letterboxing()
        navigation = []
        taps = QSignalSpy(self.canvas.controls_toggled)
        self.canvas.navigate_requested.connect(navigation.append)
        for start, end, step in ((780, 550, 1), (20, 250, -1)):
            self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, start, 300))
            self.assertTrue(self.canvas._touch_active)
            self.assertFalse(self.canvas._touch_started_on_image)
            self.assertFalse(self.canvas._hold_timer.isActive())
            self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, end, 305))
            self.assertFalse(self.canvas._blank_hold_timer.isActive())
            self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, end, 305))
            self.assertEqual(navigation[-1], step)
        self.assertEqual(navigation, [1, -1])
        self.assertEqual(self.player.play_calls, 0)
        self.assertFalse(self.canvas._click_timer.isActive())
        self.assertEqual(taps.count(), 0)

    def test_blank_touch_pans_when_zoomed_but_empty_canvas_does_not_navigate(self):
        self.portrait_with_letterboxing()
        self.canvas._zoom_at(2.0, QPointF(self.center))
        navigation = []
        self.canvas.navigate_requested.connect(navigation.append)
        before = self.canvas._scene_center()
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, 20, 400))
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 20, 400))
        self.assertLess(self.canvas._scene_center().y(), before.y())
        self.assertEqual(navigation, [])
        self.canvas.clear()
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 780, 300))
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, 300, 300))
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 300, 300))
        self.assertEqual(navigation, [])
        self.assertFalse(self.canvas._blank_hold_timer.isActive())

    def test_blank_long_press_context_fallback_is_deduplicated_and_never_plays(self):
        self.portrait_with_letterboxing()
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        menus = QSignalSpy(self.canvas.customContextMenuRequested)
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
        self.assertTrue(self.canvas._blank_hold_timer.isActive())
        self.canvas._blank_hold_timer.start(50)
        QTest.qWait(150)
        self.assertEqual(menus.count(), 1)
        self.assertEqual(self.player.play_calls, 0)
        # Simulate the real Windows promotion arriving after the fallback.
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(20, 300),
                                 self.canvas.viewport().mapToGlobal(QPoint(20, 300)))
        QApplication.sendEvent(self.canvas.viewport(), event)
        self.assertEqual(menus.count(), 1)
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 20, 300))
        self.assertFalse(self.canvas._click_timer.isActive())
        # The native path arriving first cancels the fallback instead.
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
        QApplication.sendEvent(self.canvas.viewport(), event)
        self.assertEqual(menus.count(), 2)
        self.assertFalse(self.canvas._blank_hold_timer.isActive())
        self.canvas._blank_hold_timeout()
        self.assertEqual(menus.count(), 2)

    def check_held_touch_menu(self, *, native_context=False, cancel=False, choose="mouse"):
        self.portrait_with_letterboxing()
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        device = QTest.createTouchDevice()
        window = self.canvas.windowHandle()
        sequence = QTest.touchEvent(window, device, False)
        point = self.canvas.viewport().mapTo(self.canvas, QPoint(20, 300))
        result = {}
        menus = []

        def popup(position):
            # Match MainWindow._item_menu, including its nested event loop and
            # cancellation of the canvas input state before QMenu takes over.
            self.canvas.stop_live()
            menu = QMenu(self.canvas)
            menus.append(menu)
            action = menu.addAction("Choose deliberately")
            triggered = QSignalSpy(action.triggered)

            def release_original_contact():
                # Small contact drift while still holding is enough for QMenu
                # to mistake its eventual promoted release for a menu choice.
                for x in (24, 28, 32, 36):
                    sequence.move(0, point + QPoint(x - 20, 0), window).commit()
                result["visible_while_held"] = menu.isVisible()
                if cancel:
                    QApplication.sendEvent(window, QTouchEvent(QEvent.Type.TouchCancel, device))
                else:
                    sequence.release(0, point + QPoint(16, 0), window).commit()
                # Windows may also promote the original press-and-hold to a
                # system mouse release, after the raw touch has already ended.
                local = QPointF(menu.actionGeometry(action).center())
                QApplication.sendEvent(menu, QMouseEvent(
                    QEvent.Type.MouseButtonRelease, local, local,
                    QPointF(menu.mapToGlobal(local.toPoint())),
                    Qt.MouseButton.RightButton, Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    Qt.MouseEventSource.MouseEventSynthesizedBySystem,
                ))
                result["visible_after_release"] = menu.isVisible()
                result["accidental_choices"] = triggered.count()
                if menu.isVisible():
                    if choose in ("touch", "other_touch"):
                        target = menu.windowHandle()
                        next_device = QTest.createTouchDevice() if choose == "other_touch" else device
                        tap = QTest.touchEvent(target, next_device, False)
                        tap.press(0, local.toPoint(), target).commit()
                        tap.release(0, local.toPoint(), target).commit()
                    elif choose == "keyboard":
                        QTest.keyClick(menu, Qt.Key.Key_Down)
                        QTest.keyClick(menu, Qt.Key.Key_Return)
                    else:
                        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=local.toPoint())
                result["deliberate_choices"] = triggered.count()

            # Deliberately longer than the old viewer's 650ms mouse suppression.
            QTimer.singleShot(750, release_original_contact)
            QTimer.singleShot(1800, menu.close)  # Bound a broken nested loop.
            menu.exec(self.canvas.mapToGlobal(position))

        self.canvas.customContextMenuRequested.connect(popup)
        # QMenu saves Qt's last global mouse position when it opens and ignores
        # a press at that exact origin. A previous touch-menu test can leave it
        # at this row's center. Move the real QWindow pointer to a neutral spot
        # before the opening touch, without changing any gesture assertions.
        QTest.mouseMove(window, self.canvas.viewport().mapTo(
            self.canvas, QPoint(self.canvas.viewport().width() - 15, 15)))
        sequence.press(0, point, window).commit()
        self.assertTrue(self.canvas._touch_active)
        if native_context:
            context_position = QPoint(20, 300)
            QTimer.singleShot(30, lambda: QApplication.sendEvent(
                self.canvas.viewport(), QContextMenuEvent(
                    QContextMenuEvent.Reason.Mouse, context_position,
                    self.canvas.viewport().mapToGlobal(context_position),
                ),
            ))
        else:
            self.canvas._blank_hold_timer.start(30)
        QTest.qWait(2100)
        self.assertEqual(len(menus), 1)
        self.assertTrue(result.get("visible_while_held"), result)
        self.assertTrue(result.get("visible_after_release"), result)
        self.assertEqual(result.get("accidental_choices"), 0, result)
        self.assertEqual(result.get("deliberate_choices"), 1, result)
        self.assertEqual(self.player.play_calls, 0)
        self.assertFalse(self.canvas._click_timer.isActive())
        for menu in menus:
            menu.deleteLater()

    def test_blank_long_press_menu_survives_held_touch_movement_and_late_release(self):
        self.check_held_touch_menu()

    def test_native_blank_context_menu_accepts_next_touch_after_original_release(self):
        self.check_held_touch_menu(native_context=True, choose="touch")

    def test_blank_context_menu_accepts_new_independent_touch_device(self):
        self.check_held_touch_menu(choose="other_touch")

    def test_blank_context_touch_cancel_leaves_menu_for_keyboard_choice(self):
        self.check_held_touch_menu(cancel=True, choose="keyboard")

    def test_blank_tap_and_hold_cancellation_leave_no_late_menu_or_play(self):
        self.portrait_with_letterboxing()
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        menus = QSignalSpy(self.canvas.customContextMenuRequested)
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 20, 300))
        self.assertFalse(self.canvas._click_timer.isActive())
        self.assertFalse(self.canvas._blank_hold_timer.isActive())
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Stationary, 20, 300),
                   (1, QEventPoint.State.Pressed, 200, 300))
        self.assertFalse(self.canvas._blank_hold_timer.isActive())
        self.touch(QEvent.Type.TouchCancel)
        self.canvas._blank_hold_timeout()
        self.assertEqual(menus.count(), 0)
        self.assertEqual(self.player.play_calls, 0)

    def test_blank_hold_timer_is_cancelled_by_photo_hide_focus_and_explicit_play(self):
        self.portrait_with_letterboxing()
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        menus = QSignalSpy(self.canvas.customContextMenuRequested)
        for cancel in (
            lambda: self.canvas.set_photo(self.image, self.video_path),
            self.canvas.hide,
            lambda: QApplication.sendEvent(self.canvas, QEvent(QEvent.Type.FocusOut)),
        ):
            self.portrait_with_letterboxing()
            self.canvas.show()
            self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
            self.assertTrue(self.canvas._blank_hold_timer.isActive())
            cancel()
            self.assertFalse(self.canvas._blank_hold_timer.isActive())
            self.canvas._blank_hold_timeout()
            self.assertEqual(menus.count(), 0)
            self.touch(QEvent.Type.TouchCancel)
        self.portrait_with_letterboxing()
        self.canvas.show()
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
        self.canvas.play_live_once()
        token = self.canvas._generation
        self.assertFalse(self.canvas._blank_hold_timer.isActive())
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 20, 300))
        self.touch(QEvent.Type.TouchCancel)
        self.canvas._blank_hold_timeout()
        self.assertTrue(self.canvas._play_requested)
        self.assertEqual(self.canvas._generation, token)
        self.assertEqual(menus.count(), 0)

    def test_second_finger_cancels_hold_and_pinches_around_touch_center(self):
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 300, 300))
        self.touch(
            QEvent.Type.TouchUpdate,
            (0, QEventPoint.State.Stationary, 300, 300),
            (1, QEventPoint.State.Pressed, 500, 300),
        )
        QTest.qWait(130)
        self.assertEqual(self.player.play_calls, 0)
        self.touch(
            QEvent.Type.TouchUpdate,
            (0, QEventPoint.State.Updated, 200, 300),
            (1, QEventPoint.State.Updated, 600, 300),
        )
        self.assertGreater(self.canvas.zoom_factor(), 1.9)
        self.touch(QEvent.Type.TouchCancel)
        self.assertFalse(self.canvas._touch_active)
        self.assertFalse(self.canvas._play_requested)

    def test_touch_cancel_discards_future_updates_and_window_deactivation_stops(self):
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 300, 300))
        self.touch(QEvent.Type.TouchCancel)
        self.touch(QEvent.Type.TouchUpdate, (0, QEventPoint.State.Updated, 300, 300))
        QTest.qWait(130)
        self.assertEqual(self.player.play_calls, 0)
        self.canvas.start_live()
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        QApplication.sendEvent(self.canvas, QEvent(QEvent.Type.WindowDeactivate))
        self.assertFalse(self.canvas._play_requested)
        self.assertTrue(self.canvas._photo.isVisible())

    def test_video_keeps_one_complete_owned_frame_and_centers_its_aspect(self):
        self.canvas.start_live()
        image = QImage(160, 90, QImage.Format.Format_RGB32)
        image.fill("red")
        self.canvas._video_frame(QVideoFrame(image), self.canvas._generation)
        shown = self.canvas._video.pixmap().toImage()
        self.assertEqual(shown.pixelColor(80, 45).name(), "#ff0000")
        image.fill("blue")
        self.assertEqual(shown.pixelColor(80, 45).name(), "#ff0000")
        self.assertAlmostEqual(self.canvas._video.scale(), 15.0)
        self.assertAlmostEqual(self.canvas._video.pos().y(), 225.0)
        self.canvas._video_frame(QVideoFrame(image), self.canvas._generation)
        replaced = self.canvas._video.pixmap().toImage()
        for y in range(replaced.height()):
            self.assertEqual(replaced.pixelColor(80, y).name(), "#0000ff")

    def test_frame_display_rotation_and_mirroring_applied_once(self):
        image = QImage(2, 3, QImage.Format.Format_RGB32)
        image.fill("black")
        image.setPixelColor(0, 0, Qt.GlobalColor.red)
        image.setPixelColor(1, 0, Qt.GlobalColor.green)
        image.setPixelColor(0, 2, Qt.GlobalColor.blue)
        image.setPixelColor(1, 2, Qt.GlobalColor.yellow)
        frame = QVideoFrame(image)
        frame.setRotation(QtVideo.Rotation.Clockwise90)
        converted = self.canvas._frame_image(frame)
        self.assertEqual((converted.width(), converted.height()), (3, 2))
        self.assertEqual(converted.pixelColor(0, 0).name(), "#0000ff")
        self.assertEqual(converted.pixelColor(2, 0).name(), "#ff0000")
        frame.setMirrored(True)
        converted = self.canvas._frame_image(frame)
        self.assertEqual(converted.pixelColor(0, 0).name(), "#ff0000")
        self.assertEqual(converted.pixelColor(2, 0).name(), "#0000ff")

    def test_decoder_handoff_only_presents_latest_and_navigation_rejects_stale_frames(self):
        self.canvas.start_live()
        token = self.canvas._generation
        for value in range(60):
            image = QImage(16, 12, QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.red if value < 59 else Qt.GlobalColor.blue)
            self.canvas._video_sink.setVideoFrame(QVideoFrame(image))
        self.assertFalse(self.canvas._video.isVisible())
        self.canvas._present_latest_video_frame()
        self.assertEqual(self.canvas._video.pixmap().toImage().pixelColor(8, 6).name(), "#0000ff")
        # Polling an unchanged shared frame must avoid a second image conversion.
        from unittest.mock import patch
        with patch.object(self.canvas, "_video_frame") as present:
            self.canvas._present_latest_video_frame()
            present.assert_not_called()
        self.canvas.set_photo(self.image)
        self.canvas._video_frame(self.frame(), token)
        self.canvas._present_latest_video_frame()
        self.assertFalse(self.canvas._video.isVisible())
        self.assertTrue(self.canvas._video.pixmap().isNull())
        self.assertFalse(self.canvas._frame_timer.isActive())

    def test_one_shot_ignores_loop_and_release_but_new_pointer_cancels(self):
        self.canvas.set_options(loop=True)
        self.canvas.play_live_once()
        self.assertEqual(self.player.loops, QMediaPlayer.Loops.Once)
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        self.canvas.release_live()
        QApplication.sendEvent(self.canvas, QEvent(QEvent.Type.Leave))
        self.assertTrue(self.canvas._play_requested)
        self.canvas._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        self.assertEqual(self.player.play_calls, 1)
        self.assertFalse(self.canvas._play_requested)
        self.assertTrue(self.canvas._photo.isVisible())
        self.canvas.play_live_once()
        self.canvas.start_live()
        self.assertFalse(self.canvas._play_once)
        self.assertEqual(self.player.loops, QMediaPlayer.Loops.Infinite)
        self.canvas.release_live()
        self.assertFalse(self.canvas._play_requested)
        self.canvas.play_live_once()
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.assertFalse(self.canvas._play_requested)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)

    def test_old_mouse_hold_release_cannot_stop_new_one_shot(self):
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.wait_for_play_calls(1)
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        # The app's once shortcut calls this while the old mouse is still down.
        self.canvas.play_live_once()
        token = self.canvas._generation
        self.canvas._video_frame(self.frame(), token)
        QApplication.sendEvent(self.canvas.viewport(), QEvent(QEvent.Type.UngrabMouse))
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        self.assertTrue(self.canvas._play_requested)
        self.assertTrue(self.canvas._play_once)
        self.assertEqual(self.canvas._generation, token)
        self.assertTrue(self.canvas._video.isVisible())
        self.canvas._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        self.assertFalse(self.canvas._play_requested)
        self.assertTrue(self.canvas._photo.isVisible())

    def test_old_touch_hold_release_or_cancel_cannot_stop_new_one_shot(self):
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 400, 300))
        self.wait_for_play_calls(1)
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        self.canvas.play_live_once()
        token = self.canvas._generation
        self.canvas._video_frame(self.frame(), token)
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 400, 300))
        self.touch(QEvent.Type.TouchCancel)
        self.assertTrue(self.canvas._play_requested)
        self.assertTrue(self.canvas._play_once)
        self.assertEqual(self.canvas._generation, token)
        self.assertTrue(self.canvas._video.isVisible())
        self.canvas._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        self.assertFalse(self.canvas._play_requested)
        self.assertTrue(self.canvas._photo.isVisible())

    def test_touch_image_context_is_suppressed_but_blank_and_mouse_are_preserved(self):
        portrait = QImage(600, 1200, QImage.Format.Format_RGB32)
        portrait.fill("red")
        self.canvas.set_photo(portrait, self.video_path)
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        menus = QSignalSpy(self.canvas.customContextMenuRequested)
        self.assertFalse(self.canvas.image_contains_viewport_point(QPointF(20, 300)))
        self.assertTrue(self.canvas.image_contains_viewport_point(QPointF(400, 300)))
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 400, 300))
        image_menu = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, self.center,
            self.canvas.viewport().mapToGlobal(self.center),
        )
        QApplication.sendEvent(self.canvas.viewport(), image_menu)
        self.assertEqual(menus.count(), 0)
        self.touch(QEvent.Type.TouchCancel)
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 20, 300))
        blank_menu = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(20, 300),
            self.canvas.viewport().mapToGlobal(QPoint(20, 300)),
        )
        QApplication.sendEvent(self.canvas.viewport(), blank_menu)
        self.assertEqual(menus.count(), 1)
        self.assertFalse(self.canvas._hold_timer.isActive())
        self.touch(QEvent.Type.TouchEnd, (0, QEventPoint.State.Released, 20, 300))
        self.touch(QEvent.Type.TouchBegin, (0, QEventPoint.State.Pressed, 400, 300))
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.RightButton, pos=self.center)
        QApplication.sendEvent(self.canvas.viewport(), image_menu)
        self.assertEqual(menus.count(), 2)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.RightButton, pos=self.center)


class NativeTouchTests(unittest.TestCase):
    def test_signed_coordinates_include_negative_monitors(self):
        packed = ((-50 & 0xFFFF) << 16) | (-200 & 0xFFFF)
        self.assertEqual(signed_screen_point(packed), (-200, -50))

    def test_native_pixels_use_window_scale_before_viewport_offset(self):
        class Window:
            def devicePixelRatioF(self):
                return 1.5

        class Viewport:
            def mapFrom(self, window, point):
                return point - QPoint(120, 80)

        class Canvas:
            def window(self):
                return Window()

            def viewport(self):
                return Viewport()

        self.assertEqual(native_client_to_viewport(Canvas(), 480, 270), QPointF(200, 100))

    def test_native_policy_changes_with_contact_and_bubbles_gesture_notify(self):
        class NativeFunction:
            def __init__(self, function):
                self.function = function

            def __call__(self, *args):
                return self.function(*args)

        class NativeApi:
            def __init__(self):
                self.policies = []
                self.GetAncestor = NativeFunction(lambda hwnd, flag: 123)
                self.ScreenToClient = NativeFunction(self.screen_to_client)
                self.SetGestureConfig = NativeFunction(self.configure)

            def screen_to_client(self, hwnd, pointer):
                point = ctypes.cast(pointer, ctypes.POINTER(_Point)).contents
                point.x -= 100
                point.y -= 100
                return 1

            def configure(self, hwnd, reserved, count, pointer, size):
                config = ctypes.cast(pointer, ctypes.POINTER(_GestureConfig)).contents
                self.policies.append((config.dwWant, config.dwBlock))
                return 1

        class Canvas:
            def isVisible(self):
                return True

            def window(self):
                return self

            def winId(self):
                return 123

            def devicePixelRatioF(self):
                return 1.0

            def viewport(self):
                return self

            def mapFrom(self, window, point):
                return point

            def image_contains_viewport_point(self, point):
                return 200 <= point.x() < 400 and 0 <= point.y() < 400

        canvas = Canvas()
        api = NativeApi()
        native_filter = ImageTouchGestureFilter(canvas, api)
        message = _Message()
        message.hwnd = 123
        message.message = WM_TABLET_QUERYSYSTEMGESTURESTATUS
        message.lParam = (200 << 16) | 350  # client point250,100 -> image
        result = native_filter.nativeEventFilter(QByteArray(b"windows_generic_MSG"), ctypes.addressof(message))
        self.assertEqual(result, (True, TABLET_DISABLE_PRESSANDHOLD))
        self.assertEqual(api.policies[-1], (0, GC_ALLGESTURES))
        message.lParam = (200 << 16) | 150  # client50,100 -> letterbox
        self.assertEqual(native_filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)), (False, 0))
        self.assertEqual(api.policies[-1], (GC_ALLGESTURES, 0))
        notice = _GestureNotify()
        notice.ptsLocation.x, notice.ptsLocation.y = 350, 200
        message.message = WM_GESTURENOTIFY
        message.lParam = ctypes.addressof(notice)
        self.assertEqual(native_filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)), (False, 0))
        self.assertEqual(api.policies[-1], (0, GC_ALLGESTURES))


if __name__ == "__main__":
    unittest.main()
