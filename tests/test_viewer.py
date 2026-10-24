"""Canvas input tests; these do not replace testing on a physical touchscreen."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QEventPoint, QImage, QTouchEvent
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from livegallery.viewer import ViewerCanvas


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

    def test_quick_release_never_starts_a_late_video(self):
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.qWait(25)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.qWait(120)
        self.assertEqual(self.player.play_calls, 0)
        self.assertTrue(self.canvas._photo.isVisible())

    def test_hold_waits_for_frame_then_release_immediately_restores_photo(self):
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.qWait(130)
        self.assertEqual(self.player.play_calls, 1)
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
        QTest.qWait(130)
        self.canvas._video_frame(self.frame(), self.canvas._generation)
        self.canvas._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        QTest.qWait(140)
        self.assertEqual(self.player.play_calls, 1)
        self.assertTrue(self.canvas._photo.isVisible())
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.center)
        QTest.qWait(130)
        self.assertEqual(self.player.play_calls, 2)
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


if __name__ == "__main__":
    unittest.main()
