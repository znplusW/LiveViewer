"""Geometry, bounded thumbnails and actual mouse/touch navigation."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QTouchEvent
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from livegallery.viewer import ViewerCanvas
from livegallery.cover_editor import FramePreview
from livegallery.theme import theme_tokens


class Player:
    def __init__(self):
        self.url = QUrl()
        self.play_calls = 0
    def source(self): return self.url
    def setSource(self, value): self.url = value
    def setPosition(self, _): pass
    def setLoops(self, _): pass
    def stop(self): pass
    def play(self): self.play_calls += 1


class ZoomNavigatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.video = Path(self.temporary.name) / 'sample.mov'
        self.video.touch()
        self.viewer = ViewerCanvas()
        self.viewer.resize(900, 640)
        self.viewer.show()
        self.viewer.activateWindow()
        self.app.processEvents()
        self.viewer._player = self.player = Player()
        self.image = self.photo(1600, 1000)
        self.viewer.set_photo(self.image, self.video)
        self.viewer.set_options(hold_ms=100)
        self.nav = self.viewer.zoom_navigator

    def tearDown(self):
        self.viewer.close()
        self.viewer.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    @staticmethod
    def photo(width, height):
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill('#467c91')
        painter = QPainter(image)
        painter.fillRect(0, 0, width // 2, height // 2, QColor('#cc664d'))
        painter.fillRect(width // 2, height // 2, width // 2, height // 2, QColor('#71ad72'))
        painter.end()
        return image

    def zoom(self):
        self.viewer._zoom_at(2.4, QPointF(self.viewer.viewport().rect().center()))
        self.app.processEvents()
        self.assertTrue(self.nav.isVisible())

    def assert_mapping(self):
        scene = self.viewer._image_rect
        shown = self.viewer.mapToScene(self.viewer.viewport().rect()).boundingRect().intersected(scene)
        image = self.nav.image_rect()
        expected = QRectF(image.left() + (shown.left() - scene.left()) / scene.width() * image.width(),
                          image.top() + (shown.top() - scene.top()) / scene.height() * image.height(),
                          shown.width() / scene.width() * image.width(),
                          shown.height() / scene.height() * image.height())
        actual = self.nav.visible_rect()
        for left, right in zip((actual.x(), actual.y(), actual.width(), actual.height()),
                               (expected.x(), expected.y(), expected.width(), expected.height())):
            self.assertAlmostEqual(left, right, places=6)
        self.assertTrue(image.adjusted(-.001, -.001, .001, .001).contains(actual))
        self.assertAlmostEqual(image.width() / image.height(), scene.width() / scene.height(), places=6)

    def test_mapping_portrait_landscape_pan_and_both_clamped_edges(self):
        for width, height in ((1600, 1000), (600, 1600)):
            self.viewer.set_photo(self.photo(width, height), self.video)
            self.assertFalse(self.nav.isVisible())
            self.zoom()
            self.assert_mapping()
            self.viewer._pan_by(QPointF(140, -80))
            self.assert_mapping()
            self.viewer.centerOn(-100000, -100000)
            self.assert_mapping()
            self.viewer.centerOn(100000, 100000)
            self.assert_mapping()

    def test_bounded_thumbnail_reused_for_pan_zoom_video_and_hidden_at_fit_clear(self):
        self.zoom()
        revision = self.nav.thumbnail_revision
        key = self.nav._thumbnail.cacheKey()
        self.assertLessEqual(max(self.nav._thumbnail.width(), self.nav._thumbnail.height()), 320)
        self.viewer._pan_by(QPointF(100, 40))
        self.viewer._zoom_at(1.2, QPointF(300, 300))
        self.viewer.start_live()
        self.viewer._video_frame(QVideoFrame(self.photo(320, 200)), self.viewer._generation)
        self.viewer._video_frame(QVideoFrame(self.photo(640, 400)), self.viewer._generation)
        self.viewer._update_zoom_navigator()
        self.assertTrue(self.viewer._video.isVisible())
        self.assertTrue(self.nav.isVisible())
        self.assertEqual(self.nav.thumbnail_revision, revision)
        self.assertEqual(self.nav._thumbnail.cacheKey(), key)
        self.viewer.fit_image()
        self.assertFalse(self.nav.isVisible())
        self.viewer.clear()
        self.assertFalse(self.nav.isVisible())
        self.assertTrue(self.nav._thumbnail.isNull())

    def test_resize_reanchors_overlay_and_small_viewport_shrinks_without_layout_space(self):
        self.zoom()
        for width, height in ((780, 380), (180, 130), (1000, 700)):
            self.viewer.resize(width, height)
            self.app.processEvents()
            viewport = self.viewer.viewport()
            self.assertEqual(self.nav.x() + self.nav.width(), viewport.width() - 12)
            self.assertEqual(self.nav.y() + self.nav.height(), viewport.height() - 12)
            self.assertLessEqual(self.nav.width(), min(160, viewport.width() - 24))
            self.assertLessEqual(self.nav.height(), min(160, viewport.height() - 24))
            self.assertEqual(viewport.size(), self.viewer.size())
            self.assert_mapping()

    def test_actual_mouse_click_drag_keeps_zoom_and_never_starts_live_or_navigation(self):
        self.zoom()
        scale = self.viewer.transform().m11()
        before = self.viewer._scene_center()
        navigation, toggles = QSignalSpy(self.viewer.navigate_requested), QSignalSpy(self.viewer.controls_toggled)
        rectangle = self.nav.image_rect()
        first = QPointF(rectangle.left() + rectangle.width() * .9,
                        rectangle.top() + rectangle.height() * .9).toPoint()
        last = QPointF(rectangle.left() + rectangle.width() * .1,
                       rectangle.top() + rectangle.height() * .1).toPoint()
        QTest.mousePress(self.nav, Qt.MouseButton.LeftButton, pos=first)
        QTest.qWait(180)
        self.assertNotEqual(self.viewer._scene_center(), before)
        QTest.mouseMove(self.nav, last)
        QTest.mouseRelease(self.nav, Qt.MouseButton.LeftButton, pos=last)
        self.assertLess(self.viewer._scene_center().x(), before.x())
        self.assertEqual(self.viewer.transform().m11(), scale)
        self.assertEqual(self.player.play_calls, 0)
        self.assertEqual(navigation.count(), 0)
        self.assertEqual(toggles.count(), 0)
        self.assertFalse(self.viewer._hold_timer.isActive())
        self.assert_mapping()

    def test_actual_window_touch_drag_cancel_late_updates_and_real_mouse_takeover(self):
        self.zoom()
        scale = self.viewer.transform().m11()
        before = self.viewer._scene_center()
        window = self.viewer.windowHandle()
        device = QTest.createTouchDevice()
        gesture = QTest.touchEvent(window, device, False)
        point = self.nav.image_rect().center().toPoint()
        def window_point(point): return self.nav.mapTo(self.viewer, point)
        gesture.press(0, window_point(point), window).commit()
        QTest.qWait(180)
        gesture.move(0, window_point(point + QPoint(25, 15)), window).commit()
        self.assertNotEqual(self.viewer._scene_center(), before)
        QApplication.sendEvent(self.nav, QTouchEvent(QEvent.Type.TouchCancel, device))
        cancelled = self.viewer._scene_center()
        gesture.move(0, window_point(point - QPoint(20, 15)), window).commit()
        gesture.release(0, window_point(point), window).commit()
        self.assertEqual(self.viewer._scene_center(), cancelled)
        event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(point), QPointF(point),
                            QPointF(self.nav.mapToGlobal(point)), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                            Qt.MouseEventSource.MouseEventSynthesizedByQt)
        QApplication.sendEvent(self.nav, event)
        self.assertEqual(self.viewer._scene_center(), cancelled)
        QTest.mouseClick(self.nav, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
        self.assertNotEqual(self.viewer._scene_center(), cancelled)
        self.assertEqual(self.viewer.transform().m11(), scale)
        self.assertEqual(self.player.play_calls, 0)
        self.assertFalse(self.viewer._touch_active)

    def test_multi_touch_never_becomes_a_late_single_finger_navigation(self):
        self.zoom()
        window = self.viewer.windowHandle()
        device = QTest.createTouchDevice()
        gesture = QTest.touchEvent(window, device, False)
        point = self.nav.image_rect().center().toPoint()
        first, second = self.nav.mapTo(self.viewer, point), self.nav.mapTo(self.viewer, point + QPoint(12, 0))
        gesture.press(0, first, window).commit()
        gesture.stationary(0).press(1, second, window).commit()
        cancelled = self.viewer._scene_center()
        gesture.stationary(0).release(1, second, window).commit()
        gesture.move(0, first + QPoint(20, 0), window).commit()
        gesture.release(0, first + QPoint(20, 0), window).commit()
        self.assertEqual(self.viewer._scene_center(), cancelled)
        self.assertEqual(self.player.play_calls, 0)

    def test_navigator_touch_separates_two_canvas_taps_and_cancels_pending_controls(self):
        self.zoom()
        window = self.viewer.windowHandle()
        device = QTest.createTouchDevice()
        gesture = QTest.touchEvent(window, device, False)
        point = QPoint(300, 220)
        gesture.press(0, point, window).commit()
        gesture.release(0, point, window).commit()
        self.assertTrue(self.viewer._click_timer.isActive())
        nav_point = self.nav.mapTo(self.viewer, self.nav.image_rect().center().toPoint())
        gesture.press(0, nav_point, window).commit()
        gesture.release(0, nav_point, window).commit()
        self.assertFalse(self.viewer._click_timer.isActive())
        gesture.press(0, point, window).commit()
        gesture.release(0, point, window).commit()
        self.assertFalse(self.viewer.is_zoom_animating(), 'Navigator input joined two unrelated photo taps')
        self.assertEqual(self.player.play_calls, 0)

    def test_frame_preview_same_size_new_frame_refreshes_thumbnail_without_resetting_view(self):
        preview = FramePreview()
        try:
            preview.resize(700, 520)
            preview.show()
            self.app.processEvents()
            preview.show_frame(self.image)
            preview._zoom_at(2.4, QPointF(350, 260))
            preview._pan_by(QPointF(70, -30))
            transform, center = preview.transform(), preview._scene_center()
            revision = preview.zoom_navigator.thumbnail_revision
            frame = self.photo(1600, 1000)
            frame.fill('#a87544')
            preview.show_frame(frame)
            self.assertEqual(preview.zoom_navigator.thumbnail_revision, revision + 1)
            self.assertEqual(preview.transform(), transform)
            self.assertEqual(preview._scene_center(), center)
            preview.show_frame(frame)
            self.assertEqual(preview.zoom_navigator.thumbnail_revision, revision + 1)
        finally:
            preview.close()
            preview.deleteLater()

    def test_dark_light_and_dpr_one_two_paint_visible_frame_at_same_logical_geometry(self):
        self.zoom()
        geometry = self.nav.visible_rect()
        for theme in ('dark', 'light'):
            colors = theme_tokens(theme)
            self.viewer.apply_theme(colors)
            for dpr in (1, 2):
                image = QImage(self.nav.width() * dpr, self.nav.height() * dpr, QImage.Format.Format_ARGB32)
                image.setDevicePixelRatio(dpr)
                image.fill(Qt.GlobalColor.transparent)
                self.nav.render(image)
                focus = QColor(colors['focus'])
                colored = sum(max(abs(image.pixelColor(x, y).red() - focus.red()),
                                  abs(image.pixelColor(x, y).green() - focus.green()),
                                  abs(image.pixelColor(x, y).blue() - focus.blue())) < 15
                              for x in range(image.width()) for y in range(image.height()))
                self.assertGreater(colored, 10)
                self.assertEqual(self.nav.visible_rect(), geometry)
