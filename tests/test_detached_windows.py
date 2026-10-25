"""Native auxiliary-window lifecycle and shared-viewer application integration."""
import os
import sys
import traceback
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from shiboken6 import isValid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel

from livegallery.app import MainWindow, SettingsDialog
from livegallery.detached_windows import FloatingMapWindow, PreviewWindow, accessible_geometry
from livegallery.selftest import _check_detached_preview_windows, _wait
from livegallery.settings import Preferences


class DetachedWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.qt_errors = []
        self.error_hook = patch('sys.excepthook', side_effect=lambda *args: self.qt_errors.append(args))
        self.error_hook.start()
        self.addCleanup(self.error_hook.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {'LIVEGALLERY_DATA_DIR': str(self.base / 'data')})
        self.environment.start()
        self.photos = self.base / 'photos'
        self.photos.mkdir()
        for index, color in enumerate(('#407894', '#b69b42', '#884876')):
            image = QImage(960, 640, QImage.Format.Format_RGB32)
            image.fill(color)
            image.save(str(self.photos / f'{index}.jpg'))
        self.window = MainWindow()
        self.window.resize(1280, 850)
        self.window.show()
        self.window.load_folder(self.photos)
        _wait(lambda: not self.window._scan_pending and self.window._shown_image_path is not None, 25)
        self.settle()

    def tearDown(self):
        self.window.close()
        self.assertTrue(self.window.wait_for_shutdown(25), self.window._shutdown_error)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.temporary.cleanup()
        self.assertEqual(self.qt_errors, [], [''.join(traceback.format_exception(*error)) for error in self.qt_errors])

    def settle(self):
        for _ in range(5):
            self.app.processEvents()
            QTest.qWait(1)

    def detach(self):
        QTest.mouseClick(self.window.detach_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertTrue(self.window._detached)
        return self.window.preview_window

    def test_actual_button_moves_same_viewer_player_and_grid_selection_updates_preview(self):
        window = self.window
        viewer, player, model = window.viewer, window.viewer._player, window.model
        preview = self.detach()
        self.assertIs(window.viewer, viewer)
        self.assertIs(viewer._player, player)
        self.assertIs(window.model, model)
        self.assertIs(preview.content.currentWidget(), viewer)
        self.assertIs(window.content.currentWidget(), window.grid)
        self.assertTrue(window._grid_mode)
        self.assertFalse(window.grid_button.isEnabled())
        index = model.index(1)
        window.grid.scrollTo(index)
        self.settle()
        QTest.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=window.grid.visualRect(index).center())
        expected = model.items[1].image_path
        _wait(lambda: window._shown_image_path == expected, 25)
        self.assertIs(window.content.currentWidget(), window.grid)
        self.assertIs(preview.content.currentWidget(), viewer)
        self.assertIn(expected.name, preview.windowTitle())
        self.assertFalse(window.film.isVisible())

    def test_reparent_preserves_real_transform_and_center_without_autoplay(self):
        window = self.window
        window.viewer._zoom_at(2.5, window.viewer._viewport_center())
        before = window.viewer.transform(), window.viewer._scene_center()
        with patch.object(window.viewer, 'play_live_once') as play:
            for _ in range(2):
                self.detach()
                self.assertEqual(window.viewer.transform(), before[0])
                self.assertLess((window.viewer._scene_center() - before[1]).manhattanLength(), 5)
                window._redock_preview()
                self.settle()
                self.assertEqual(window.viewer.transform(), before[0])
                self.assertLess((window.viewer._scene_center() - before[1]).manhattanLength(), 5)
                self.assertIs(window.content.currentWidget(), window.viewer)
            play.assert_not_called()

    def test_detach_mid_zoom_applies_deferred_high_resolution_before_capturing_view(self):
        viewer = self.window.viewer
        viewer._toggle_double_zoom(viewer._viewport_center())
        viewer._zoom_animation.setCurrentTime(viewer._zoom_animation.duration() // 2)
        before_width = viewer._image_rect.width() * viewer.transform().m11()
        before_center = viewer._scene_center() / viewer._image_rect.width()
        higher = QImage(1920, 1280, QImage.Format.Format_RGB32)
        higher.fill('#407894')
        viewer.replace_photo(higher)
        self.assertIsNotNone(viewer._deferred_photo)
        self.detach()
        self.assertFalse(viewer.is_zoom_animating())
        self.assertIsNone(viewer._deferred_photo)
        self.assertEqual(viewer._image_rect.width(), 1920)
        self.assertAlmostEqual(viewer._image_rect.width() * viewer.transform().m11(), before_width, delta=4)
        self.assertLess((viewer._scene_center() / viewer._image_rect.width() - before_center).manhattanLength(), .02)

    def test_queued_reparent_restore_cannot_apply_old_photo_view_to_new_cached_selection(self):
        window = self.window
        target = window.model.items[1]
        image = QImage(str(target.image_path))
        window._cache_preview(target.image_path, image, image.size().toTuple())
        window.viewer._zoom_at(2.5, window.viewer._viewport_center())
        window._detach_preview()
        window._select_row(1, allow_autoplay=False)
        self.assertAlmostEqual(window.viewer.zoom_factor(), 1, delta=.01)
        self.settle()
        self.assertEqual(window._current_id, target.id)
        self.assertAlmostEqual(window.viewer.zoom_factor(), 1, delta=.01)
        self.assertTrue(window.viewer._fit_mode)

    def test_font_and_screen_callbacks_are_safe_after_auxiliary_qobjects_are_deleted(self):
        errors = []
        with patch('sys.excepthook', side_effect=lambda *args: errors.append(args)):
            for cls in (PreviewWindow, FloatingMapWindow):
                auxiliary = cls()
                QApplication.sendEvent(auxiliary, QEvent(QEvent.Type.FontChange))
                QApplication.sendEvent(auxiliary, QEvent(QEvent.Type.ScreenChangeInternal))
                auxiliary.shutdown()
                auxiliary.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
        self.assertEqual(errors, [], [str(error[1]) for error in errors])

    def test_empty_filter_and_loading_state_stay_in_both_intended_hosts(self):
        preview = self.detach()
        self.window.search.setText('no such photo')
        self.settle()
        self.assertIs(preview.content.currentWidget(), preview.empty)
        self.assertIn('没有符合条件', preview.empty.text())
        self.assertIs(self.window.content.currentWidget(), self.window.grid)
        self.window.search.clear()
        _wait(lambda: self.window._shown_image_path is not None)
        self.assertIs(preview.content.currentWidget(), self.window.viewer)
        empty = self.base / 'empty'
        empty.mkdir()
        self.window.load_folder(empty)
        _wait(lambda: not self.window._scan_pending)
        self.assertTrue(self.window._grid_mode)
        self.assertIs(preview.content.currentWidget(), preview.empty)
        self.assertIs(self.window.content.currentWidget(), self.window.grid)
        preview.close()
        self.settle()
        self.assertFalse(self.window._detached)
        self.assertIs(self.window.content.currentWidget(), self.window.empty_page)

    def test_failed_image_redocks_to_the_real_error_page_and_later_valid_photo_recovers(self):
        broken = self.photos / 'broken.jpg'
        broken.write_bytes(b'not an image')
        self.window._rescan()
        _wait(lambda: not self.window._scan_pending)
        self.detach()
        row = next(i for i, item in enumerate(self.window.model.items) if item.image_path == broken)
        self.window._select_row(row, allow_autoplay=False)
        _wait(lambda: self.window.empty_title.text() == '无法读取这张图片')
        self.assertIs(self.window.preview_window.content.currentWidget(), self.window.preview_window.empty)
        self.window.preview_window.close()
        self.settle()
        self.assertIs(self.window.content.currentWidget(), self.window.empty_page)
        self.window._select_row(0, allow_autoplay=False)
        _wait(lambda: self.window._shown_image_path == self.photos / '0.jpg')
        self.assertIs(self.window.content.currentWidget(), self.window.viewer)

    def test_preview_close_redocks_and_startup_restores_geometry_without_opening_auxiliary_windows(self):
        preview = self.detach()
        preview.resize(740, 530)
        self.settle()
        preview._remember_geometry()
        saved = Preferences.load().preview_geometry
        self.assertTrue(saved)
        preview.close()
        self.settle()
        self.assertFalse(self.window._detached)
        self.assertIsNone(self.window.preview_window)
        self.assertIs(self.window.content.currentWidget(), self.window.viewer)
        restored = MainWindow()
        try:
            self.assertEqual(restored.preferences.preview_geometry, saved)
            self.assertFalse(restored._detached)
            self.assertIsNone(restored.preview_window)
            self.assertIsNone(restored.touch_map_window)
        finally:
            restored.close()
            self.assertTrue(restored.wait_for_shutdown(25))
            restored.deleteLater()

    def test_keyboard_shortcuts_route_to_shared_photo_but_grid_zoom_and_text_input_remain_local(self):
        preview = self.detach()
        preview.activateWindow()
        self.window.viewer.setFocus()
        self.settle()
        with patch.object(self.window.viewer, 'play_live_once') as play:
            QTest.keyClick(self.window.viewer, Qt.Key.Key_P)
            play.assert_called_once()
        before = self.window.model.items[0].id
        QTest.keyClick(self.window.viewer, Qt.Key.Key_Right)
        self.assertNotEqual(self.window._current_id, before)
        old_width = self.window.grid.thumbnail_width
        self.window.activateWindow()
        self.window.grid.setFocus()
        self.settle()
        QTest.keyClick(self.window.grid, Qt.Key.Key_Equal, Qt.KeyboardModifier.ControlModifier)
        self.assertGreater(self.window.grid.thumbnail_width, old_width)
        self.window.search.setFocus()
        QTest.keyClicks(self.window.search, 'P')
        self.assertEqual(self.window.search.text(), 'P')

    def test_display_recovery_uses_real_available_screen_and_preserves_live_window(self):
        preview = self.detach()
        preview.move(-30000, -20000)
        self.window._recover_auxiliary_windows()
        self.settle()
        self.assertTrue(any(screen.availableGeometry().intersects(preview.geometry()) for screen in self.app.screens()))
        self.assertTrue(self.window._detached and preview.isVisible())
        self.assertEqual(accessible_geometry(QRect(2000, 0, 900, 700), [QRect(0, 0, 800, 600)]), QRect(0, 0, 800, 600))
        gaps = (QRect(0, 0, 800, 600), QRect(1600, 0, 800, 600))
        recovered = accessible_geometry(QRect(1000, 100, 250, 250), gaps)
        self.assertTrue(any(area.contains(recovered) for area in gaps))

    def test_sidebar_toggle_follows_folder_label_and_fullscreen_retains_action_state(self):
        window = self.window
        self.assertIs(window.sidebar_button.parentWidget(), window.path_group)
        self.assertFalse(window.header.isAncestorOf(window.sidebar_button))
        window.path_label.setText('已打开 3 个文件夹')
        self.settle()
        self.assertLessEqual(window.sidebar_button.x() - (window.path_label.x() + window.path_label.width()), 8)
        self.assertEqual(window.sidebar_button.text(), '隐藏列表')
        QTest.mouseClick(window.sidebar_button, Qt.MouseButton.LeftButton)
        self.assertFalse(window.sidebar.isVisible())
        self.assertEqual(window.sidebar_button.text(), '显示列表')
        window._toggle_fullscreen()
        self.settle()
        self.assertFalse(window.filterbar.isVisible())
        window._toggle_fullscreen()
        self.settle()
        self.assertFalse(window.sidebar.isVisible() or window.sidebar_button.isChecked())
        self.assertEqual(window.sidebar_button.text(), '显示列表')
        window.path_label.setText('/very long path/' * 40)
        window.resize(940, 650)
        self.settle()
        self.assertIn('…', QLabel.text(window.path_label))
        self.assertTrue(window.sidebar_button.isVisible())
        self.assertLess(window.path_group.width(), 431)
        self.assertEqual(window.minimumWidth(), 940)

    def test_real_location_dropdown_floats_and_map_close_hides_only_then_redocks_last_edge(self):
        preview = self.detach()
        self.assertIsNone(self.window.touch_map_panel)
        QTest.mouseClick(self.window.map_button, Qt.MouseButton.LeftButton)
        self.settle()
        panel = self.window.touch_map_panel
        self.assertIs(panel.surface.viewer, self.window.viewer)
        self.assertEqual(panel.edge, 'right')
        self.assertTrue(panel.isVisible())
        self.window._dock_touch_map('left')
        panel.location.showPopup()
        QTest.keyClick(panel.location, Qt.Key.Key_End)
        QTest.keyClick(panel.location, Qt.Key.Key_Return)
        self.settle()
        floating = self.window.touch_map_window
        self.assertTrue(floating.isVisible() and self.window.preferences.map_floating)
        floating.close()
        self.settle()
        self.assertTrue(preview.isVisible() and self.window._detached)
        self.assertFalse(panel.isVisible())
        self.window._toggle_touch_map(True)
        self.settle()
        QTest.mouseClick(panel.redock_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertEqual(panel.edge, 'left')
        self.assertTrue(panel.isVisible())
        self.assertFalse(floating.isVisible() or self.window.preferences.map_floating)
        self.window._float_touch_map()
        self.settle()
        QTest.mouseClick(panel.return_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertFalse(self.window._detached or panel.isVisible())
        self.assertFalse(isValid(preview) and preview.isVisible())
        self.assertIsNone(self.window.touch_map_window)
        # The former floating child is reusable after the old native window dies.
        self.detach()
        self.window._toggle_touch_map(True)
        self.settle()
        self.assertTrue(self.window.touch_map_window.isVisible())

    def test_dock_mouse_and_qwindow_touch_separators_resize_overview_and_keep_separate_preferences(self):
        self.detach()
        self.window._toggle_touch_map(True)
        self.settle()
        panel = self.window.touch_map_panel
        device = QTest.createTouchDevice()
        model = self.window.model
        selected = self.window._current_id
        resets = QSignalSpy(model.modelReset)
        for edge in ('right', 'left', 'bottom'):
            self.window._dock_touch_map(edge)
            self.settle()
            handle = panel.resize_handle
            start = handle.rect().center()
            delta = QPoint(0, -35) if edge == 'bottom' else QPoint(35 if edge == 'left' else -35, 0)
            before = panel.height() if edge == 'bottom' else panel.width()
            overview = self.window.content.height() if edge == 'bottom' else self.window.content.width()
            end = handle.mapToGlobal(start) + delta
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(handle, handle.mapFromGlobal(end))
            self.settle()
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(end))
            self.settle()
            self.assertEqual(panel.height() if edge == 'bottom' else panel.width(), before + 35)
            self.assertLess(self.window.content.height() if edge == 'bottom' else self.window.content.width(), overview)
            target = self.window.windowHandle()
            point = handle.mapTo(self.window, handle.rect().center())
            sequence = QTest.touchEvent(target, device, False)
            sequence.press(0, point, target).commit()
            sequence.move(0, point - delta, target).commit()
            self.settle()
            sequence.release(0, point - delta, target).commit()
            self.settle()
            self.assertEqual(panel.height() if edge == 'bottom' else panel.width(), before)
        self.assertEqual(self.window.preferences.map_dock_width, 320)
        self.assertEqual(self.window.preferences.map_bottom_height, 260)
        self.assertEqual(resets.count(), 0)
        self.assertEqual(self.window._current_id, selected)
        self.assertIs(self.window.model, model)

    def test_floating_preview_fullscreen_is_pure_and_main_close_stops_all_auxiliary_windows(self):
        preview = self.detach()
        self.window._toggle_touch_map(True)
        self.window._float_touch_map()
        self.settle()
        floating = self.window.touch_map_window
        self.window._toggle_fullscreen()
        self.settle()
        self.assertTrue(preview.isFullScreen())
        self.assertFalse(preview.isAncestorOf(self.window.film))
        self.assertFalse(preview.isAncestorOf(self.window.touch_map_panel))
        self.assertIs(preview.content.currentWidget(), self.window.viewer)
        self.window.close()
        self.settle()
        self.assertFalse(isValid(preview) and preview.isVisible())
        self.assertFalse(isValid(floating) and floating.isVisible())
        self.assertTrue(self.window.wait_for_shutdown(25))

    def test_small_bottom_map_keeps_header_and_surface_inside_and_does_not_overwrite_preference(self):
        self.detach()
        self.window._toggle_touch_map(True)
        self.window._dock_touch_map('bottom')
        self.window._set_map_thickness(180)
        self.window.resize(940, 650)
        self.settle()
        panel = self.window.touch_map_panel
        self.assertEqual(self.window.preferences.map_bottom_height, 220)
        self.assertGreaterEqual(panel.height(), panel.minimumSizeHint().height())
        for child in (panel.surface, panel.location, panel.hide_button, panel.return_button):
            rect = QRect(child.mapTo(panel, QPoint()), child.size())
            self.assertTrue(panel.rect().contains(rect), (type(child).__name__, rect, panel.rect()))
        self.assertGreaterEqual(self.window.content.height(), 160)
        self.window.preferences.map_bottom_height = 640
        self.window._clamp_touch_map()
        self.settle()
        self.assertLess(panel.height(), 640)
        self.assertEqual(self.window.preferences.map_bottom_height, 640)

    def test_source_window_helper_proves_clamp_and_restore_when_native_size_is_capped(self):
        fixtures = Path(__file__).parent / 'fixtures'
        self.window.setMaximumSize(1028, 749)
        self.window.load_folder(fixtures)
        _wait(lambda: not self.window._scan_pending and self.window._shown_image_path is not None, 25)
        result = _check_detached_preview_windows(self.window, fixtures, self.base / 'reports')
        preferred = result['map_restore_preferred_height']
        self.assertLess(result['clamped_map_height'], preferred)
        self.assertEqual(result['map_large_restored_height'], preferred)
        self.assertLess(preferred, 640)
        for state in result['map_resize_actual_geometries'].values():
            self.assertLessEqual(state['window'][0], 1028)
            self.assertLessEqual(state['window'][1], 749)
            self.assertEqual(state['preferred_height'], preferred)
            self.assertGreaterEqual(state['overview'][1], 160)

    def test_qwindow_top_edge_swipe_exits_only_corresponding_fullscreen_and_keeps_overview_or_preview(self):
        self.window.fullscreen_edge_gesture.set_enabled_edges({'top'})
        device = QTest.createTouchDevice()
        def swipe(window):
            target = window.windowHandle()
            point = QPoint(window.width() // 2, 5)
            sequence = QTest.touchEvent(target, device, False)
            sequence.press(0, point, target).commit()
            sequence.move(0, point + QPoint(0, 110), target).commit()
            self.settle()
            sequence.release(0, point + QPoint(0, 110), target).commit()
            self.settle()
        selected = self.window._current_id
        self.window._toggle_grid(True)
        self.window._toggle_fullscreen()
        self.settle()
        swipe(self.window)
        self.assertFalse(self.window._fullscreen_active)
        self.assertTrue(self.window._grid_mode)
        self.assertIs(self.window.content.currentWidget(), self.window.grid)
        self.detach()
        self.window._toggle_touch_map(True)
        self.window._float_touch_map()
        self.settle()
        preview = self.window.preview_window
        self.window._toggle_fullscreen()
        self.settle()
        swipe(preview)
        self.assertFalse(preview.isFullScreen())
        self.assertTrue(self.window._detached and self.window.touch_map_panel.isVisible())
        self.assertTrue(self.window.touch_map_window.isVisible())
        self.assertEqual(self.window._current_id, selected)
        self.assertFalse(self.window.viewer._play_requested)

    def test_real_settings_save_applies_empty_exit_gestures_immediately_and_cancel_keeps_it(self):
        self.assertEqual(self.window.preferences.fullscreen_exit_gestures, ['left'])
        errors = []
        def choose(accept):
            dialog = QApplication.activeModalWidget()
            try:
                self.assertIsInstance(dialog, SettingsDialog)
                dialog.fullscreen_exit_gestures.set_selected_gestures([] if accept else ['top', 'right'])
                buttons = dialog.findChild(QDialogButtonBox)
                button = buttons.button(QDialogButtonBox.StandardButton.Save if accept else QDialogButtonBox.StandardButton.Cancel)
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            except Exception as error:
                errors.append(error)
                if dialog is not None:
                    dialog.reject()
        for accept in (True, False):
            QTimer.singleShot(0, lambda accept=accept: choose(accept))
            self.window.open_settings()
            self.assertFalse(errors, errors)
            self.assertEqual(self.window.preferences.fullscreen_exit_gestures, [])
            self.assertEqual(self.window.fullscreen_edge_gesture.enabled_edges, frozenset())
            self.assertEqual(Preferences.load().fullscreen_exit_gestures, [])


if __name__ == '__main__':
    unittest.main()
