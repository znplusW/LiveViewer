"""Window integration and asynchronous-result ownership regressions."""

import ctypes
import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QImage, QKeyEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from livegallery.app import MainWindow, SettingsDialog
from livegallery.models import MediaItem, PairDecision
from livegallery.cache import CacheStore, file_version
from livegallery.cache_locations import open_cache
from livegallery.settings import Preferences
from livegallery.system_open import (E_ABORT, HRESULT_CANCELLED, OAIF_EXEC,
                                     OAIF_HIDE_REGISTRATION, OPENASINFO, open_with)


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"LIVEGALLERY_DATA_DIR": str(self.base / "data")})
        self.environment.start()
        self.window = MainWindow()
        self.window.show()
        self.application.processEvents()
        self.image = QImage(640, 480, QImage.Format.Format_RGB32)
        self.image.fill("#406078")

    def tearDown(self):
        self.window.close()
        self.assertTrue(self.window.wait_for_shutdown(5.0), self.window._shutdown_error)
        self.window.deleteLater()
        self.application.processEvents()
        self.environment.stop()
        self.temporary.cleanup()

    def test_close_is_nonblocking_and_cache_closes_after_running_worker_exits(self):
        started, release = threading.Event(), threading.Event()
        observations = []
        def active_job():
            started.set()
            release.wait(3)
            observations.append(self.window.cache._closed)
        self.window.jobs.submit("shutdown-held-worker", active_job, 0)
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        try:
            self.window.close()
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertFalse(self.window.wait_for_shutdown(0.02))
        finally:
            release.set()
        self.assertTrue(self.window.wait_for_shutdown(3))
        self.assertEqual(observations, [False])
        self.assertTrue(self.window.cache._closed)

    def picture(self, name):
        target = self.base / "photos" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.image.save(str(target)))
        return target

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail("Timed out waiting for asynchronous image load")

    def test_real_recursive_scan_navigation_search_and_grid(self):
        for name in ("1.png", "2.jpeg", "3.PNG", "sub/4.bmp"):
            self.picture(name)
        self.window.load_folder(self.base / "photos")
        self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer)
        self.assertEqual(len(self.window.model.items), 4)
        self.window._navigate(1)
        self.wait_until(lambda: self.window._displayed_item is not None and
                        self.window._displayed_item.image_path.name == "2.jpeg")
        self.window.grid_button.click()
        self.assertIs(self.window.content.currentWidget(), self.window.grid)
        self.window.grid_button.click()
        self.assertIs(self.window.content.currentWidget(), self.window.viewer)
        self.window.search.setText("4.bmp")
        self.assertEqual(len(self.window.model.items), 1)
        self.window.search.setText("no-such-photo")
        self.assertEqual(len(self.window.model.items), 0)
        self.assertIsNone(self.window._current_id)
        self.assertIs(self.window.content.currentWidget(), self.window.empty_page)

    def test_prefetch_result_for_removed_extra_can_load_current_live_source(self):
        path = self.picture("chosen.jpeg")
        item = MediaItem("live-id", path, self.base / "clip.mov")
        self.window._primary_items = [item]
        self.window._rebuild_items()
        self.window._current_id = item.id
        self.window._request_context["prefetch"] = ("image", self.window._epoch, "removed-extra-id", path)
        self.window._job_finished("prefetch", (self.image, (640, 480)), None)
        self.assertIs(self.window.content.currentWidget(), self.window.viewer)
        self.assertEqual(self.window._displayed_item.id, "live-id")

    def test_filmstrip_top_arrow_hides_and_existing_controls_restore_in_fullscreen(self):
        self.show_live_item()
        if not self.window.strip_button.isChecked():
            self.window.strip_button.click()
        self.assertTrue(self.window.film.isVisible())
        self.assertEqual(self.window.collapse_strip_button.accessibleName(), "隐藏胶片条")
        self.window.collapse_strip_button.click()
        self.assertFalse(self.window.film.isVisible())
        self.assertFalse(self.window.strip_button.isChecked())
        self.assertFalse(self.window.preferences.filmstrip_visible)
        self.assertFalse(self.window._remembered_strip)
        self.window.strip_button.click()
        self.assertTrue(self.window.film.isVisible())
        self.window._toggle_fullscreen()
        self.assertTrue(self.window.collapse_strip_button.isVisible())
        self.window.collapse_strip_button.click()
        self.assertFalse(self.window.film.isVisible())
        self.assertTrue(self.window._fullscreen_active)
        self.window._toggle_strip_shortcut()
        self.assertTrue(self.window.film.isVisible())
        self.assertTrue(self.window.strip_button.isChecked())

    def test_reveal_selects_current_image_on_worker_and_coalesces_duplicate_clicks(self):
        item = self.show_live_item()
        started, release = threading.Event(), threading.Event()
        calls = []
        gui_thread = threading.get_ident()
        def reveal(path):
            calls.append((path, threading.get_ident()))
            started.set()
            release.wait(2)
            return True
        with patch('livegallery.app.reveal_in_folder', side_effect=reveal):
            self.window._open_image_folder(item)
            self.assertTrue(started.wait(1))
            self.window._open_image_folder(item)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], item.image_path)
            self.assertNotEqual(calls[0][1], gui_thread)
            self.assertTrue(self.window._reveal_pending)
            release.set()
            self.wait_until(lambda: not self.window._reveal_pending)
            self.window._reveal_thread.join(1)

    def test_reveal_failure_is_reported_on_gui_thread(self):
        item = self.show_live_item()
        with patch('livegallery.app.reveal_in_folder', side_effect=OSError('无法定位测试图片')), \
                patch.object(self.window, '_open_error') as error:
            self.window._open_image_folder(item)
            self.wait_until(lambda: not self.window._reveal_pending)
            error.assert_called_once_with('无法定位测试图片')
            self.window._reveal_thread.join(1)

    def test_reveal_result_after_window_close_does_not_touch_deleted_widgets(self):
        item = self.show_live_item()
        entered, release = threading.Event(), threading.Event()
        def reveal(_path):
            entered.set()
            release.wait(2)
            raise OSError('late failure')
        with patch('livegallery.app.reveal_in_folder', side_effect=reveal), \
                patch.object(self.window, '_open_error') as error:
            self.window._open_image_folder(item)
            self.assertTrue(entered.wait(1))
            self.window.close()
            self.assertTrue(self.window.wait_for_shutdown(5))
            release.set()
            self.window._reveal_thread.join(1)
            self.application.processEvents()
            error.assert_not_called()

    def test_pair_revision_rejects_old_automatic_result_after_manual_selection(self):
        first = self.picture("same.jpg")
        second = self.picture("same.jpeg")
        item = MediaItem("pair", first, self.base / "same.mov", (first, second), True)
        self.window._primary_items = [item]
        self.window._rebuild_items()
        self.window._current_id = item.id
        self.window._pair_revisions[item.id] = 1
        self.window._request_context["old"] = ("pair", self.window._epoch, item, 0)
        self.window._job_finished("old", PairDecision(first, (), False, "old"), None)
        self.assertTrue(self.window._item_by_id(item.id).pending)
        self.window._request_context["new"] = ("pair", self.window._epoch, item, 1)
        self.window._job_finished("new", PairDecision(second, (first,), False, "manual"), None)
        self.assertEqual(self.window._item_by_id(item.id).image_path, second)
        self.assertEqual(len(self.window._extras[item.id]), 1)

    def test_resolved_item_leaving_pending_filter_selects_remaining_item(self):
        first, second = self.picture("a.jpg"), self.picture("b.jpg")
        a = MediaItem("a", first, self.base / "a.mov", (first,), True)
        b = MediaItem("b", second, uncertain=True)
        self.window._primary_items = [a, b]
        self.window._rebuild_items()
        self.window._current_id = a.id
        self.window._request_pair = lambda *_args: None
        self.window.kind_filter.pending.setChecked(True)
        self.window._request_context["complete"] = ("pair", self.window._epoch, a, 0)
        self.window._job_finished("complete", PairDecision(first, (), False, "done"), None)
        self.assertEqual(self.window._current_id, b.id)
        self.assertEqual(self.window.counter_label.text(), "1 / 1")

    def test_background_analysis_is_one_at_a_time_and_pauses_during_playback(self):
        first, second = self.picture("a.jpg"), self.picture("b.jpg")
        self.window._primary_items = [
            MediaItem("a", first, self.base / "a.mov", (first,), True),
            MediaItem("b", second, self.base / "b.mov", (second,), True),
        ]
        requests = []
        self.window._request_pair = lambda item, priority: requests.append((item.id, priority))
        self.window._schedule_background_pair()
        self.window._schedule_background_pair()
        self.assertEqual(requests, [("a", 12)])
        self.window._background_pair_id = None
        self.window.viewer._play_requested = True
        self.window._schedule_background_pair()
        self.assertEqual(len(requests), 1)

    def show_live_item(self):
        path = self.picture("still.jpg")
        item = MediaItem("live", path, self.base / "clip.mov")
        self.window._displayed_item = item
        self.window._current_id = item.id
        self.window._primary_items = [item]
        self.window._rebuild_items()
        self.window.model.set_items([item])
        self.window.viewer.set_photo(self.image, item.video_path)
        self.window.content.setCurrentWidget(self.window.viewer)
        self.window.viewer.setFocus()
        self.application.processEvents()
        return item

    def assert_fullscreen_has_no_controls(self):
        self.assertTrue(self.window.isFullScreen())
        for widget in (self.window.header, self.window.filterbar, self.window.infobar,
                       self.window.viewer_tools, self.window.sidebar, self.window.statusBar(),
                       self.window.toast, self.window.library_label, self.window.analysis_label):
            self.assertFalse(widget.isVisible(), widget.objectName() or type(widget).__name__)

    def test_fullscreen_is_immediate_stable_and_f11_or_escape_exit(self):
        self.show_live_item()
        self.window.fullscreen_button.click()
        self.assertFalse(self.window._grid_mode)
        self.assertIs(self.window.content.currentWidget(), self.window.viewer)
        self.assert_fullscreen_has_no_controls()
        self.assertTrue(self.window.film.isVisible())
        QTest.mouseMove(self.window.viewer.viewport(), QPoint(100, 100))
        self.window.viewer.controls_toggled.emit()
        self.window._show_message("后台分析已完成")
        self.window._set_chrome(True)
        repeated = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F11,
                             Qt.KeyboardModifier.NoModifier, "", True)
        QApplication.sendEvent(self.window.viewer, repeated)
        self.assert_fullscreen_has_no_controls()
        self.assertTrue(self.window.film.isVisible())
        QTest.keyClick(self.window.viewer, Qt.Key.Key_F11)
        self.assertFalse(self.window.isFullScreen())
        self.assertTrue(self.window.header.isVisible())
        self.assertTrue(self.window.film.isVisible())
        QTest.keyClick(self.window.viewer, Qt.Key.Key_F11)
        self.assert_fullscreen_has_no_controls()
        QTest.keyClick(self.window.viewer, Qt.Key.Key_Escape)
        self.assertFalse(self.window.isFullScreen())

    def test_fullscreen_restores_maximized_state_and_hidden_filmstrip_preference(self):
        self.show_live_item()
        self.window.strip_button.click()
        self.window.showMaximized()
        self.application.processEvents()
        self.assertTrue(self.window.isMaximized())
        self.window._toggle_fullscreen()
        self.assert_fullscreen_has_no_controls()
        self.assertFalse(self.window.film.isVisible())
        self.window.viewer.controls_toggled.emit()
        self.assertFalse(self.window.film.isVisible())
        QTest.keyClick(self.window.viewer, Qt.Key.Key_F11)
        self.assertTrue(self.window.isMaximized())
        self.assertFalse(self.window.film.isVisible())

    def test_media_type_filter_supports_both_each_and_neither(self):
        live = MediaItem("live", self.picture("live.jpg"), self.base / "live.mov")
        static = MediaItem("static", self.picture("static.png"), uncertain=True)
        self.window._primary_items = [live, static]
        self.window._rebuild_items()
        self.window._apply_filters(select_first=False)
        self.assertEqual({item.id for item in self.window.model.items}, {"live", "static"})
        self.window.kind_filter.static.setChecked(False)
        self.assertEqual([item.id for item in self.window.model.items], ["live"])
        self.window.kind_filter.live.setChecked(False)
        self.assertEqual(self.window.model.items, [])
        self.window.kind_filter.static.setChecked(True)
        self.assertEqual([item.id for item in self.window.model.items], ["static"])
        self.window.kind_filter.live.setChecked(True)
        self.assertEqual(len(self.window.model.items), 2)
        self.window.kind_filter.pending.setChecked(True)
        self.assertEqual([item.id for item in self.window.model.items], ["static"])

    def test_once_shortcut_is_independent_of_key_release_and_repeat(self):
        self.show_live_item()
        with patch.object(self.window.viewer, "play_live_once") as once, \
                patch.object(self.window.viewer, "stop_live") as stop:
            QTest.keyPress(self.window.viewer, Qt.Key.Key_P)
            QTest.keyRelease(self.window.viewer, Qt.Key.Key_P)
            repeated = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_P,
                                 Qt.KeyboardModifier.NoModifier, "p", True)
            QApplication.sendEvent(self.window.viewer, repeated)
            self.assertEqual(once.call_count, 1)
            stop.assert_not_called()

    def test_custom_once_combo_overrides_builtin_and_ignores_text_and_modal_dialog(self):
        self.show_live_item()
        self.window._once_sequence = QKeySequence("Ctrl+O")
        with patch.object(self.window.viewer, "play_live_once") as once, \
                patch.object(self.window, "choose_folder") as choose:
            QTest.keyClick(self.window.viewer, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(once.call_count, 1)
            choose.assert_not_called()
            self.window.search.setFocus()
            self.application.processEvents()
            QTest.keyClick(self.window.search, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(once.call_count, 1)
            dialog = SettingsDialog(self.window.preferences, self.window)
            dialog.show()
            self.application.processEvents()
            QTest.keyClick(dialog.live_key, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(once.call_count, 1)
            dialog.reject()

    def test_f11_always_exits_fullscreen_even_when_assigned_to_playback(self):
        self.show_live_item()
        self.window._once_sequence = QKeySequence("F11")
        with patch.object(self.window.viewer, "play_live_once") as once:
            QTest.keyClick(self.window.viewer, Qt.Key.Key_F11)
            once.assert_called_once()
            self.assertFalse(self.window.isFullScreen())
            self.window._toggle_fullscreen()
            self.assert_fullscreen_has_no_controls()
            QTest.keyClick(self.window.viewer, Qt.Key.Key_F11)
            self.assertFalse(self.window.isFullScreen())
            once.assert_called_once()

    def test_shortcut_settings_validate_conflicts_and_persist_once_key(self):
        dialog = SettingsDialog(self.window.preferences, self.window)
        dialog.live_once_key.setKeySequence(QKeySequence("Space"))
        with patch("livegallery.app.QMessageBox.information") as message:
            dialog._accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertIn("相同", message.call_args.args[2])
        dialog.live_once_key.setKeySequence(QKeySequence("Esc"))
        with patch("livegallery.app.QMessageBox.information") as message:
            dialog._accept()
            self.assertIn("Esc", message.call_args.args[2])
        dialog.live_once_key.setKeySequence(QKeySequence("Right"))
        with patch("livegallery.app.QMessageBox.information") as message:
            dialog._accept()
            self.assertIn("下一张", message.call_args.args[2])
        dialog.override_keys.setChecked(True)
        dialog._accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        preferences = Preferences(live_once_key="Ctrl+Shift+L")
        preferences.save()
        self.assertEqual(Preferences.load().live_once_key, "Ctrl+Shift+L")
        # Settings saved by older versions gain the new default automatically.
        target = self.base / "data" / "settings.json"
        target.write_text('{"live_key": "Space"}', encoding="utf-8")
        self.assertEqual(Preferences.load().live_once_key, "P")

    def test_all_internal_shortcut_conflicts_are_shown_while_editing(self):
        dialog = SettingsDialog(self.window.preferences, self.window)
        dialog.show()
        self.application.processEvents()
        try:
            for editor in (dialog.live_key, dialog.live_once_key, dialog.filmstrip_key):
                dialog.live_key.setKeySequence(QKeySequence("Space"))
                dialog.live_once_key.setKeySequence(QKeySequence("P"))
                dialog.filmstrip_key.setKeySequence(QKeySequence("F"))
                for key, description in (("Left", "上一张"), ("Right", "下一张"),
                                         ("F11", "全屏"), ("Ctrl+O", "打开文件夹")):
                    with self.subTest(editor=editor, key=key):
                        editor.setKeySequence(QKeySequence(key))
                        self.assertTrue(dialog.shortcut_note.isVisible())
                        self.assertIn(description, dialog.shortcut_note.text())
                        self.assertTrue(dialog.override_keys.isVisible())
                        self.assertFalse(dialog.override_keys.isChecked())
            dialog.filmstrip_key.setKeySequence(QKeySequence("F"))
            dialog.live_once_key.setKeySequence(QKeySequence("F11"))
            self.assertIn("全屏时仍用于退出", dialog.shortcut_note.text())
            dialog.live_once_key.setKeySequence(QKeySequence("Space"))
            self.assertTrue(dialog.shortcut_note.isVisible())
            self.assertIn("相同", dialog.shortcut_note.text())
            self.assertFalse(dialog.override_keys.isVisible())
            dialog.live_once_key.setKeySequence(QKeySequence("Esc"))
            self.assertTrue(dialog.shortcut_note.isVisible())
            self.assertIn("Esc 保留", dialog.shortcut_note.text())
            dialog.live_once_key.setKeySequence(QKeySequence("Ctrl+Left"))
            self.assertFalse(dialog.shortcut_note.isVisible())
        finally:
            dialog.reject()

    def test_modified_arrows_do_not_silently_use_plain_arrow_navigation(self):
        self.show_live_item()
        with patch.object(self.window, "_navigate") as navigate:
            QTest.keyClick(self.window.viewer, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(self.window.viewer, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
            navigate.assert_not_called()
            QTest.keyClick(self.window.viewer, Qt.Key.Key_Left)
            navigate.assert_called_once_with(-1)

    def test_old_hold_p_binding_survives_once_shortcut_migration(self):
        target = self.base / "data" / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        for saved in ('{"live_key": "p"}', '{"live_key": "P", "live_once_key": "p"}',
                      '{"live_key": "P", "live_once_key": "Esc"}',
                      '{"live_key": "P", "live_once_key": "not a valid key"}'):
            target.write_text(saved, encoding="utf-8")
            loaded = Preferences.load()
            self.assertEqual(loaded.live_key, "P")
            self.assertEqual(loaded.live_once_key, "F9")
        # Reopen from the migrated file as an actual application launch would.
        self.window.close()
        self.assertTrue(self.window.wait_for_shutdown(5))
        self.window.deleteLater()
        self.application.processEvents()
        target.write_text('{"live_key": "p"}', encoding="utf-8")
        self.window = MainWindow()
        self.window.show()
        self.show_live_item()
        with patch.object(self.window.viewer, "start_live") as hold, \
                patch.object(self.window.viewer, "release_live") as release, \
                patch.object(self.window.viewer, "play_live_once") as once:
            QTest.keyPress(self.window.viewer, Qt.Key.Key_P)
            hold.assert_called_once()
            once.assert_not_called()
            QTest.keyRelease(self.window.viewer, Qt.Key.Key_P)
            release.assert_called_once()
            QTest.keyClick(self.window.viewer, Qt.Key.Key_F9)
            once.assert_called_once()

    def test_fullscreen_overview_preserves_mode_and_opened_photo_stays_fullscreen(self):
        self.show_live_item()
        self.window.grid_button.click()
        self.window.fullscreen_button.click()
        self.assert_fullscreen_has_no_controls()
        self.assertTrue(self.window._grid_mode)
        self.assertIs(self.window.content.currentWidget(), self.window.grid)
        self.assertFalse(self.window.film.isVisible())
        self.assertFalse(self.window.fullscreen_live_badge.isVisible())
        QTest.mouseMove(self.window.grid.viewport(), QPoint(200, 100))
        QTest.keyClick(self.window.grid, Qt.Key.Key_Escape)
        self.assertFalse(self.window.isFullScreen())
        self.assertTrue(self.window._grid_mode)
        QTest.keyClick(self.window.grid, Qt.Key.Key_Escape)
        self.assertTrue(self.window._grid_mode)
        QTest.keyClick(self.window.grid, Qt.Key.Key_F11)
        self.window._grid_selected(self.window.model.index(0))
        self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer)
        self.assertTrue(self.window.isFullScreen())
        self.assertFalse(self.window._grid_mode)
        self.assertTrue(self.window.fullscreen_live_badge.isVisible())
        QTest.keyClick(self.window.viewer, Qt.Key.Key_Escape)
        self.assertFalse(self.window.isFullScreen())
        self.assertFalse(self.window._grid_mode)
        repeated = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                             Qt.KeyboardModifier.NoModifier, "", True)
        QApplication.sendEvent(self.window.viewer, repeated)
        self.assertFalse(self.window._grid_mode)
        QTest.keyClick(self.window.viewer, Qt.Key.Key_Escape)
        self.assertTrue(self.window._grid_mode)
        self.assertIs(self.window.content.currentWidget(), self.window.grid)

    def test_fullscreen_live_badge_tracks_displayed_media_and_fixed_client_corner(self):
        live = self.show_live_item()
        self.assertFalse(self.window.fullscreen_live_badge.isVisible())
        self.window._toggle_fullscreen()
        badge = self.window.fullscreen_live_badge
        self.assertTrue(badge.isVisible())
        self.assertIs(badge.parentWidget(), self.window.centralWidget())
        self.assertTrue(badge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        position = badge.mapTo(self.window.centralWidget(), QPoint())
        self.assertEqual(position, QPoint(16, 16))
        self.window._current_full = True
        self.window.viewer.zoom_in()
        self.window._toggle_strip_shortcut()
        self.assertEqual(badge.mapTo(self.window.centralWidget(), QPoint()), position)
        self.window._toggle_grid(True)
        self.assertFalse(badge.isVisible())
        self.window._toggle_grid(False)
        self.assertTrue(badge.isVisible())
        static = MediaItem("static", self.picture("ordinary.png"))
        self.window._primary_items = [live, static]
        self.window._rebuild_items()
        self.window._current_id = static.id
        self.window._display_image(static, self.image, (640, 480))
        self.assertFalse(badge.isVisible())
        self.window._current_id = live.id
        self.window._display_image(live, self.image, (640, 480))
        self.assertTrue(badge.isVisible())
        self.window.kind_filter.live.setChecked(False)
        self.wait_until(lambda: self.window._displayed_item is not None and
                        self.window._displayed_item.id == static.id)
        self.assertFalse(badge.isVisible())
        self.window.kind_filter.static.setChecked(False)
        self.assertFalse(badge.isVisible())

    def test_live_badge_waits_for_pending_pair_and_hides_during_loading(self):
        live = self.show_live_item()
        pending = MediaItem("pending", self.picture("pending.jpg"), self.base / "pending.mov",
                            (), True)
        self.window._primary_items.append(pending)
        self.window._rebuild_items()
        self.window.model.set_items([live, pending])
        self.window._toggle_fullscreen()
        with patch.object(self.window, "_request_pair"):
            self.window._select_row(1)
        self.assertFalse(self.window.fullscreen_live_badge.isVisible())
        self.window._request_context["pair-ready"] = ("pair", self.window._epoch, pending, 0)
        self.window._job_finished("pair-ready", PairDecision(pending.image_path, (), False, "ready"), None)
        self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer)
        self.assertTrue(self.window.fullscreen_live_badge.isVisible())

    def test_filmstrip_shortcut_fullscreen_grid_input_safety_and_persistence(self):
        self.show_live_item()
        QTest.keyClick(self.window.viewer, Qt.Key.Key_F)
        self.assertFalse(self.window.film.isVisible())
        self.assertFalse(Preferences.load().filmstrip_visible)
        self.window._toggle_fullscreen()
        QTest.keyPress(self.window.viewer, Qt.Key.Key_F)
        QTest.keyRelease(self.window.viewer, Qt.Key.Key_F)
        self.assertTrue(self.window.film.isVisible())
        repeated = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F, Qt.KeyboardModifier.NoModifier, "f", True)
        QApplication.sendEvent(self.window.viewer, repeated)
        self.assertTrue(self.window.film.isVisible())
        self.window._toggle_grid(True)
        QTest.keyClick(self.window.grid, Qt.Key.Key_F)
        self.assertFalse(self.window._remembered_strip)
        self.assertFalse(self.window.film.isVisible())
        self.window._toggle_grid(False)
        self.assertFalse(self.window.film.isVisible())
        self.window._exit_fullscreen()
        self.window.search.setFocus()
        QTest.keyClick(self.window.search, Qt.Key.Key_F)
        self.assertFalse(self.window._remembered_strip)
        self.window.search.clear()
        dialog = SettingsDialog(self.window.preferences, self.window)
        dialog.show()
        self.application.processEvents()
        QTest.keyClick(dialog.filmstrip_key, Qt.Key.Key_F)
        self.assertFalse(self.window._remembered_strip)
        dialog.reject()
        saved = Preferences(live_key="F", live_once_key="F10", filmstrip_key="F", filmstrip_visible=False)
        saved.save()
        loaded = Preferences.load()
        self.assertEqual(loaded.live_key, "F")
        self.assertEqual(loaded.live_once_key, "F10")
        self.assertEqual(loaded.filmstrip_key, "F8")
        self.assertFalse(loaded.filmstrip_visible)
        saved.filmstrip_key = "Ctrl+Shift+G"
        saved.save()
        self.assertEqual(Preferences.load().filmstrip_key, "Ctrl+Shift+G")

    def test_filmstrip_conflicts_with_both_playback_keys_and_reserves_esc(self):
        dialog = SettingsDialog(self.window.preferences, self.window)
        for key, label in (("Space", "按住播放"), ("P", "单击播放"), ("Esc", "Esc 保留")):
            dialog.filmstrip_key.setKeySequence(QKeySequence(key))
            self.assertIn(label, dialog.shortcut_note.text())
            with patch("livegallery.app.QMessageBox.information"):
                dialog._accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_saving_custom_filmstrip_key_rebinds_the_actual_action(self):
        self.show_live_item()
        dialog = SettingsDialog(self.window.preferences, self.window)
        dialog.filmstrip_key.setKeySequence(QKeySequence("Ctrl+G"))
        with patch("livegallery.app.SettingsDialog", return_value=dialog), \
                patch.object(dialog, "exec", return_value=QDialog.DialogCode.Accepted):
            self.window.open_settings()
        self.assertEqual(self.window.preferences.filmstrip_key, "Ctrl+G")
        self.assertEqual(Preferences.load().filmstrip_key, "Ctrl+G")
        QTest.keyClick(self.window.viewer, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(self.window.film.isVisible())
        QTest.keyClick(self.window.viewer, Qt.Key.Key_F)
        self.assertFalse(self.window.film.isVisible())

    def test_open_with_menu_uses_selected_static_source_and_cancel_is_quiet(self):
        self.show_live_item()
        heic = self.base / "照片 空格 'literal'" / "配对静态.HEIC"
        heic.parent.mkdir()
        heic.touch()
        item = MediaItem("chosen-heic", heic, self.base / "never-open.MOV")
        self.window._primary_items = [item]
        self.window._rebuild_items()
        self.window._current_id = item.id
        application = MagicMock()
        application.display_name = "照片 & 查看器"
        menu = QMenu(self.window)
        def execute(_position):
            submenu = next(action.menu() for action in menu.actions() if action.text() == "以其他软件打开")
            self.assertIsNotNone(submenu)  # A real submenu supplies the hover arrow.
            submenu.aboutToShow.emit()
            submenu.aboutToShow.emit()
            self.assertEqual(submenu.actions()[0].text(), "照片 && 查看器")
            # Qt may hide menus before delivering QAction.triggered. Native
            # references must remain valid until the outer exec returns.
            submenu.aboutToHide.emit()
            menu.aboutToHide.emit()
            application.close.assert_not_called()
            submenu.actions()[0].trigger()
            application.invoke.assert_called_once_with(heic)
            application.close.assert_not_called()
            submenu.actions()[-1].trigger()
        with patch("livegallery.app.QMenu", return_value=menu), \
                patch.object(menu, "exec", side_effect=execute), \
                patch("livegallery.app.enumerate_apps", return_value=[application]) as enumerate_native, \
                patch("livegallery.app.open_with", return_value=False) as chooser, \
                patch.object(self.window, "_show_message") as message:
            self.window._item_menu(QPoint())
            enumerate_native.assert_called_once_with(heic)
            application.close.assert_called_once()
            chooser.assert_called_once_with(heic, int(self.window.winId()))
            message.assert_not_called()
        with patch("livegallery.app.open_with", side_effect=OSError("native failure")), \
                patch.object(self.window, "_show_message") as message:
            self.window._open_with_other_app(item)
            message.assert_called_once_with("native failure")
        self.window._toggle_fullscreen()
        with patch("livegallery.app.open_with", side_effect=OSError("native failure")), \
                patch("livegallery.app.QMessageBox.warning") as warning:
            self.window._open_with_other_app(item)
            warning.assert_called_once_with(self.window, "无法打开", "native failure")

    def test_open_with_submenu_always_has_chooser_and_handles_list_or_invoke_failure(self):
        item = self.show_live_item()
        for response in ([], OSError("enumeration failed")):
            menu = QMenu(self.window)
            applications = []
            with patch("livegallery.app.enumerate_apps", side_effect=response if isinstance(response, OSError) else None,
                       return_value=response if isinstance(response, list) else None):
                submenu = self.window._add_open_with_menu(menu, item, applications)
                submenu.aboutToShow.emit()
                self.assertFalse(submenu.actions()[0].isEnabled())
                self.assertEqual(submenu.actions()[-1].text(), "选择其他应用…")
            menu.deleteLater()
        application = MagicMock()
        application.invoke.side_effect = OSError("invoke failed")
        with patch.object(self.window, "_show_message") as message:
            self.window._invoke_other_app(application, item)
            message.assert_called_once_with("invoke failed")
        application.invoke.side_effect = None
        application.invoke.return_value = False
        with patch.object(self.window, "_show_message") as message:
            self.window._invoke_other_app(application, item)
            message.assert_not_called()

    def test_horizontal_scroll_setting_roundtrip_rebind_and_mute_preserve_mode(self):
        self.show_live_item()
        dialog = SettingsDialog(self.window.preferences, self.window)
        dialog.horizontal_scroll_mode.setCurrentIndex(1)
        with patch("livegallery.app.SettingsDialog", return_value=dialog), \
                patch.object(dialog, "exec", return_value=QDialog.DialogCode.Accepted), \
                patch.object(self.window.viewer, "set_options") as options:
            self.window.open_settings()
            self.assertEqual(options.call_args.args[-1], "pan")
            self.window._toggle_sound(False)
            self.assertEqual(options.call_args.args[-1], "pan")
        self.assertEqual(Preferences.load().horizontal_scroll_mode, "pan")
        for mode, expected in (("navigate", "navigate"), ("pan", "pan"), ("invalid", "navigate")):
            Preferences(horizontal_scroll_mode=mode).save()
            self.assertEqual(Preferences.load().horizontal_scroll_mode, expected)
        self.assertEqual(SettingsDialog(Preferences(horizontal_scroll_mode="pan"), self.window)
                         .horizontal_scroll_mode.currentIndex(), 1)

    def test_filename_mouse_drag_copies_selected_text_and_ctrl_a_copies_long_full_name(self):
        self.show_live_item()
        field = self.window.filename_label
        filename = "Photo_2026_10_25_IMG_12345_中文_" + "long_filename_" * 18 + ".HEIC"
        field.setText(filename)
        self.assertTrue(field.isReadOnly())
        self.assertEqual(field.cursorPosition(), 0)
        metrics = field.fontMetrics()
        start = QPoint(metrics.horizontalAdvance(filename[:2]) + 2, field.height() // 2)
        end = QPoint(metrics.horizontalAdvance(filename[:15]) + 2, field.height() // 2)
        QTest.mousePress(field, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(field, end)
        QTest.mouseRelease(field, Qt.MouseButton.LeftButton, pos=end)
        self.assertTrue(field.hasSelectedText())
        selected = field.selectedText()
        self.assertNotEqual(selected, filename)
        self.assertIn(selected, filename)
        QTest.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.application.clipboard().text(), selected)
        QTest.keyClick(field, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(field.selectedText(), filename)
        QTest.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.application.clipboard().text(), filename)
        field.setSelection(len(filename) - 20, 12)
        selected, cursor = field.selectedText(), field.cursorPosition()
        cursor_rect = field.cursorRect()
        field.setText(filename)  # A background refresh of this same photo.
        self.assertEqual(field.selectedText(), selected)
        self.assertEqual(field.cursorPosition(), cursor)
        self.assertEqual(field.cursorRect(), cursor_rect)
        field.setText("replacement.jpg")
        self.assertEqual(field.cursorPosition(), 0)
        self.assertFalse(field.hasSelectedText())

    def test_filename_native_context_copy_and_shortcuts_do_not_change_media_or_text(self):
        item = self.show_live_item()
        field = self.window.filename_label
        filename = "Example_中文_filename.JPG"
        field.setText(filename)
        field.setFocus()
        field.setSelection(2, 7)
        selected = field.selectedText()
        standard_menu = []
        def copy_and_close():
            popup = QApplication.activePopupWidget()
            if isinstance(popup, QMenu):
                standard_menu.extend(action.text() for action in popup.actions())
                copy = next((action for action in popup.actions()
                             if "copy" in action.text().casefold() or "复制" in action.text()), None)
                if copy is not None:
                    copy.trigger()
                popup.close()
        QTimer.singleShot(50, copy_and_close)
        with patch.object(self.window, "_item_menu") as file_menu:
            QApplication.sendEvent(field, QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                   field.rect().center(), field.mapToGlobal(field.rect().center())))
            QTest.qWait(100)
            file_menu.assert_not_called()
        self.assertTrue(standard_menu)
        self.assertEqual(self.application.clipboard().text(), selected)
        self.assertFalse(any("以其他软件打开" in title for title in standard_menu))
        with patch.object(self.window.viewer, "start_live") as hold, \
                patch.object(self.window.viewer, "play_live_once") as once, \
                patch.object(self.window, "_navigate") as navigate, \
                patch.object(self.window, "_toggle_strip_shortcut") as strip:
            self.window.activateWindow()
            field.setFocus()
            self.application.processEvents()
            self.assertIs(QApplication.focusWidget(), field)
            QTest.keyClick(field, Qt.Key.Key_P)
            QTest.keyClick(field, Qt.Key.Key_F)
            QTest.keyClick(field, Qt.Key.Key_Space)
            QTest.keyClick(field, Qt.Key.Key_Left)
            QTest.keyClick(field, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(field.text(), filename)
            self.assertEqual(self.window._current_id, item.id)
            for callback in (hold, once, navigate, strip):
                callback.assert_not_called()
        QTest.keyClick(field, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        repeated = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                             Qt.KeyboardModifier.NoModifier, "", True)
        QApplication.sendEvent(field, repeated)
        self.assertFalse(self.window._grid_mode)
        QTest.keyClick(field, Qt.Key.Key_Escape)
        self.assertTrue(self.window._grid_mode)
        QTest.keyClick(field, Qt.Key.Key_Escape)
        self.assertTrue(self.window._grid_mode)

    def catalog(self, items, *, cached=False):
        self.window._primary_items = items
        self.window._rebuild_items()
        self.window.model.set_items(items)
        if cached:
            for item in items:
                self.window._cache_preview(item.image_path, self.image, (640, 480))

    def test_autoplay_once_per_selection_and_not_on_refresh_or_manual_loop_setting(self):
        first = MediaItem("first", self.picture("first.jpg"), self.base / "first.mov")
        second = MediaItem("second", self.picture("second.jpg"), self.base / "second.mov")
        static = MediaItem("static", self.picture("static.png"))
        self.catalog([first, second, static], cached=True)
        self.window.preferences.autoplay = True
        self.window.preferences.loop = True
        with patch.object(self.window.viewer, "play_live_once") as once, \
                patch.object(self.window.viewer, "start_live") as held:
            self.window._select_row(0)
            self.application.processEvents()
            once.assert_called_once()
            self.window._display_image(first, self.image, (640, 480))
            self.window._display_image(first, self.image, (640, 480))
            self.application.processEvents()
            once.assert_called_once()
            self.window._select_row(2)
            self.application.processEvents()
            once.assert_called_once()
            # The old zero-delay request must not play after rapid navigation.
            self.window._select_row(0)
            self.window._select_row(1)
            observed_paths = []
            once.side_effect = lambda: observed_paths.append(self.window.viewer._video_path)
            self.application.processEvents()
            self.assertEqual(observed_paths, [second.video_path])
            self.assertEqual(once.call_count, 2)
            held.assert_not_called()

    def test_autoplay_suppressed_in_grid_dialog_hidden_and_enable_applies_next_selection(self):
        first = MediaItem("first", self.picture("first.jpg"), self.base / "first.mov")
        second = MediaItem("second", self.picture("second.jpg"), self.base / "second.mov")
        self.catalog([first, second], cached=True)
        with patch.object(self.window.viewer, "play_live_once") as once:
            self.window._select_row(0)
            self.application.processEvents()
            once.assert_not_called()
            dialog = SettingsDialog(self.window.preferences, self.window)
            dialog.autoplay.setChecked(True)
            with patch("livegallery.app.SettingsDialog", return_value=dialog), \
                    patch.object(dialog, "exec", return_value=QDialog.DialogCode.Accepted):
                self.window.open_settings()
            self.window._display_image(first, self.image, (640, 480))
            self.application.processEvents()
            once.assert_not_called()
            self.assertTrue(Preferences.load().autoplay)
            self.window._toggle_grid(True)
            self.window._select_row(1)
            self.application.processEvents()
            once.assert_not_called()
            self.window._toggle_grid(False)
            dialog = SettingsDialog(self.window.preferences, self.window)
            dialog.show()
            self.application.processEvents()
            self.window._select_row(0)
            self.application.processEvents()
            once.assert_not_called()
            dialog.reject()
            self.window.hide()
            self.window._select_row(1)
            self.application.processEvents()
            once.assert_not_called()
            self.window.show()
            self.window._display_image(second, self.image, (640, 480))
            self.application.processEvents()
            once.assert_not_called()
            self.window._select_row(0)
            self.application.processEvents()
            once.assert_called_once()

    def test_rapid_hundred_selections_cancel_obsolete_decodes_and_bypass_blocked_probe(self):
        items = [MediaItem(str(index), self.picture(f"rapid-{index}.jpg")) for index in range(100)]
        self.catalog(items)
        self.window.model.thumbnail_requested.disconnect()
        background_started, background_release = threading.Event(), threading.Event()
        first_started, first_release = threading.Event(), threading.Event()
        decoded = []
        def blocked_probe():
            background_started.set()
            background_release.wait(5)
        def decode(path, edge):
            decoded.append((path.name, edge))
            if path == items[0].image_path:
                first_started.set()
                first_release.wait(5)
            return self.image, (4000, 3000)
        self.window.jobs.submit("test-probe", blocked_probe, 12)
        self.assertTrue(background_started.wait(1))
        try:
            with patch("livegallery.app._decode", side_effect=decode):
                self.window._select_row(0)
                self.assertTrue(first_started.wait(1))
                self.window._submit("full", str(items[0].image_path),
                                    lambda: decode(items[0].image_path, None), 1,
                                    (self.window._epoch, items[0].id, items[0].image_path))
                for row in range(1, 100):
                    self.window._select_row(row)
                    self.assertLessEqual(self.window.jobs.queued_count, 3)
                first_release.set()
                self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer
                                and self.window._displayed_item.id == "99")
                self.assertFalse(background_release.is_set())
                self.assertEqual(decoded, [("rapid-0.jpg", 2000), ("rapid-99.jpg", 2000)])
                self.assertLessEqual(len(self.window._request_context), 3)
        finally:
            first_release.set()
            background_release.set()

    def test_current_photo_promotes_queued_neighbor_while_background_is_blocked(self):
        items = [MediaItem(str(index), self.picture(f"neighbor-{index}.jpg")) for index in range(2)]
        self.catalog(items)
        self.window.model.thumbnail_requested.disconnect()
        started, release = threading.Event(), threading.Event()
        decoded = []
        def background():
            started.set()
            release.wait(5)
        self.window.jobs.submit("blocked-background", background, 12)
        self.assertTrue(started.wait(1))
        try:
            with patch("livegallery.app._decode", side_effect=lambda path, _edge:
                       (decoded.append(path.name) or self.image, (640, 480))):
                self.window._select_row(0)
                self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer)
                self.window._select_row(1)
                self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer
                                and self.window._displayed_item.id == "1")
                self.assertEqual(decoded, ["neighbor-0.jpg", "neighbor-1.jpg"])
                self.assertFalse(release.is_set())
        finally:
            release.set()

    def test_pending_pair_shows_provisional_still_before_analysis_and_refines_without_replay(self):
        first, better = self.picture("pending.jpg"), self.picture("pending.jpeg")
        pending = MediaItem("pending", first, self.base / "pending.mov", (first, better), True)
        self.catalog([pending], cached=True)
        self.window._cache_preview(better, self.image, (640, 480))
        self.window.preferences.autoplay = True
        with patch.object(self.window, "_request_pair"), \
                patch.object(self.window.viewer, "play_live_once") as once:
            self.window._select_row(0)
            self.application.processEvents()
            self.assertIs(self.window.content.currentWidget(), self.window.viewer)
            self.assertEqual(self.window._shown_image_path, first)
            once.assert_called_once()
            self.window.viewer.zoom_in()
            zoom = self.window.viewer.zoom_factor()
            self.window._is_playing = True
            self.window.viewer._play_requested = True
            self.window._request_context["refined"] = ("pair", self.window._epoch, pending, 0)
            self.window._job_finished("refined", PairDecision(better, (first,), False, "better"), None)
            self.assertEqual(self.window._shown_image_path, first)
            self.assertIsNotNone(self.window._deferred_candidate)
            self.window.viewer._play_requested = False
            self.window._playback_changed(False)
            self.assertEqual(self.window._shown_image_path, better)
            self.assertAlmostEqual(self.window.viewer.zoom_factor(), zoom, places=2)
            self.application.processEvents()
            once.assert_called_once()

    def test_arrow_autorepeat_coalesces_to_latest_target_and_flushes_release(self):
        items = [MediaItem(str(index), self.picture(f"key-{index}.jpg")) for index in range(100)]
        self.catalog(items, cached=False)
        self.window._current_id = "0"
        self.window.content.setCurrentWidget(self.window.viewer)
        self.window.viewer.setFocus()
        self.application.processEvents()
        with patch.object(self.window, "_select_row") as select:
            for _index in range(99):
                repeat = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier, "", True)
                QApplication.sendEvent(self.window.viewer, repeat)
            select.assert_not_called()
            QTest.keyRelease(self.window.viewer, Qt.Key.Key_Right)
            select.assert_called_once_with(99)

    def test_editor_result_updates_current_or_adds_copy_without_rescan_or_autoplay(self):
        first, other = self.picture("cover.jpg"), self.picture("cover.jpeg")
        original = MediaItem("cover", first, self.base / "cover.mov", (first, other))
        self.catalog([original], cached=True)
        self.window.preferences.autoplay = True
        versions = (file_version(first),)
        thumbnail = self.window.cache.get_thumbnail(first)
        overwrite = SimpleNamespace(image_path=first, video_path=original.video_path, created_copy=False)
        with patch.object(self.window, "load_folder") as rescan, \
                patch.object(self.window.viewer, "play_live_once") as once:
            self.window._apply_cover_result(original, overwrite, versions)
            self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer)
            self.assertFalse(thumbnail.exists())
            self.assertEqual(self.window.cache.get_override(original), first)
            self.assertEqual(len(self.window._primary_items), 1)
            self.assertEqual([item.image_path for item in self.window._extras[original.id]], [other])
            new_image = self.picture("cover_v2.jpg")
            new_video = self.base / "cover_v2.mov"
            new_video.touch()
            copy = SimpleNamespace(image_path=new_image, video_path=new_video, created_copy=True)
            self.window._apply_cover_result(original, copy, versions)
            self.wait_until(lambda: self.window.content.currentWidget() is self.window.viewer
                            and self.window._displayed_item.image_path == new_image)
            self.assertEqual(len(self.window._primary_items), 2)
            self.assertEqual(self.window.viewer._video_path, new_video)
            once.assert_not_called()
            rescan.assert_not_called()

    def test_cover_editor_entry_is_live_only_and_cancel_does_not_refresh_files(self):
        live = self.show_live_item()
        static = MediaItem("plain", self.picture("plain.png"))
        self.catalog([live, static])
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Rejected
        dialog.saved_result = None
        with patch("livegallery.cover_editor.EditorDialog", return_value=dialog) as editor, \
                patch.object(self.window, "_apply_cover_result") as refresh:
            self.window._open_cover_editor(static)
            editor.assert_not_called()
            self.window._queue_navigation(1)
            self.assertEqual(self.window._navigation_target_id, static.id)
            self.window._open_cover_editor(live)
            self.assertFalse(self.window._navigation_timer.isActive())
            self.assertIsNone(self.window._navigation_target_id)
            QTest.qWait(60)
            self.assertEqual(self.window._current_id, live.id)
            editor.assert_called_once_with(live, self.window)
            dialog.deleteLater.assert_called_once()
            refresh.assert_not_called()
            self.assertFalse(self.window._autoplay_armed)
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.saved_result = SimpleNamespace(image_path=live.image_path, video_path=live.video_path)
            self.window._open_cover_editor(live)
            self.assertEqual(refresh.call_count, 1)
            self.assertIs(refresh.call_args.args[1], dialog.saved_result)

    def test_edit_invalidation_rejects_inflight_old_pair_cache_write(self):
        item = MediaItem("guarded", self.picture("guarded.jpg"), self.base / "guarded.mov")
        decision = PairDecision(item.image_path, (), False, "obsolete")
        for manual in (False, True):
            if manual:
                self.window.cache.set_override(item, item.image_path)
            started, release, complete = threading.Event(), threading.Event(), threading.Event()
            def old_analysis():
                self.window.cache.get_override(item)
                if not manual:
                    self.window.cache.get_decision(item, "test")
                started.set()
                release.wait(3)
                self.window.cache.put_decision(item, "test", decision)
                complete.set()
            worker = threading.Thread(target=old_analysis)
            worker.start()
            self.assertTrue(started.wait(1))
            self.window.cache.invalidate_item(item)
            release.set()
            self.assertTrue(complete.wait(1))
            worker.join(1)
            self.assertIsNone(self.window.cache.get_decision(item, "test"))

    def choose_cache(self, mode):
        application_directory = self.base / 'application'
        application_directory.mkdir(exist_ok=True)
        with patch('livegallery.app.app_directory', return_value=application_directory):
            dialog = SettingsDialog(self.window.preferences, self.window)
            dialog.cache_location.setCurrentIndex(dialog.cache_location.findData(mode))
            with patch('livegallery.app.SettingsDialog', return_value=dialog), \
                    patch.object(dialog, 'exec', return_value=QDialog.DialogCode.Accepted):
                self.window.open_settings()
            dialog.deleteLater()

    def test_three_cache_locations_switch_independently_and_keep_current_photo(self):
        first, selected = self.picture('first.jpg'), self.picture('selected.jpg')
        self.window.load_folder(first.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        self.assertFalse((first.parent / 'LiveViewer Cache').exists())
        self.window._select_row(1)
        self.wait_until(lambda: self.window._displayed_item.image_path == selected)
        item = self.window._displayed_item
        self.window.cache.set_override(item, selected)
        self.window.cache.get_thumbnail(selected)
        self.window._toggle_grid(True)
        settings_path = self.base / 'data' / 'settings.json'
        flag = self.base / 'application' / 'portable.flag'
        for mode, target in (
                ('program', self.base / 'data' / 'program-data' / 'cache'),
                ('library', first.parent / 'LiveViewer Cache'),
                ('user', self.base / 'data' / 'cache')):
            with self.subTest(mode=mode):
                previous = self.window.cache
                self.choose_cache(mode)
                self.wait_until(lambda: self.window.cache is not previous and
                                self.window._displayed_item is not None and
                                self.window._displayed_item.image_path == selected)
                self.assertEqual(self.window.cache.base, target)
                self.assertTrue(self.window._grid_mode)
                self.assertEqual(self.window.cache.get_override(self.window._displayed_item), selected)
                self.assertEqual(Preferences.load().cache_location, mode)
                self.assertTrue(settings_path.is_file())
                self.assertFalse(flag.exists())
        self.window._clear_cache()
        self.wait_until(lambda: not any(context[0] == 'clear' for context in
                                       self.window._request_context.values()))
        self.assertEqual(self.window.cache.get_override(self.window._displayed_item), selected)

    def test_library_mode_without_folder_uses_local_cache_until_opened(self):
        self.choose_cache('library')
        self.wait_until(lambda: self.window._cache_key == ('library', None))
        self.assertEqual(self.window.preferences.cache_location, 'library')
        self.assertEqual(self.window.cache.base, self.base / 'data' / 'cache')
        path = self.picture('one.jpg')
        self.window.load_folder(path.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        cache = self.window.cache
        self.assertEqual(cache.base, path.parent / 'LiveViewer Cache')
        self.window.load_folder(path.parent, preserve_view=True)
        self.wait_until(lambda: self.window._displayed_item is not None)
        self.assertIs(self.window.cache, cache)
        self.assertFalse(self.window._cache_warning)

    def test_cache_location_change_does_not_autoplay_the_same_photo_again(self):
        path = self.picture('live.jpg')
        path.with_suffix('.mov').touch()
        self.window.preferences.autoplay = True
        decision = PairDecision(path, (), False, 'test')
        with patch('livegallery.app.resolve_pair', return_value=decision), \
                patch.object(self.window.viewer, 'play_live_once') as once:
            self.window.load_folder(path.parent)
            self.wait_until(lambda: once.call_count == 1 and
                            self.window._displayed_item is not None and
                            not self.window._displayed_item.pending)
            once.reset_mock()
            old = self.window.cache
            self.choose_cache('program')
            self.wait_until(lambda: self.window.cache is not old and
                            self.window._displayed_item is not None and
                            not self.window._displayed_item.pending)
            QTest.qWait(30)
            once.assert_not_called()
            self.assertFalse(self.window._autoplay_armed)
            self.assertEqual(self.window._displayed_item.image_path, path)

    def test_recovered_gallery_cache_is_reused_on_same_folder_rescan(self):
        path = self.picture('one.jpg')
        self.choose_cache('library')
        self.wait_until(lambda: self.window._cache_key == ('library', None))
        self.window.load_folder(path.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        cache = self.window.cache
        warning = '缓存索引已自动恢复。'
        self.window._cache_warning = warning
        with patch('livegallery.app.open_cache', wraps=open_cache) as opened:
            self.window.load_folder(path.parent, preserve_view=True)
            self.wait_until(lambda: self.window._displayed_item is not None)
            opened.assert_not_called()
        self.assertIs(self.window.cache, cache)
        self.assertEqual(self.window._cache_location, 'library')
        self.assertEqual(self.window._cache_warning, warning)

    def test_gallery_cache_never_imports_same_named_different_gallery(self):
        first, second = self.picture('A/same.bmp'), self.picture('B/same.bmp')
        image = QImage(640, 480, QImage.Format.Format_RGB32)
        image.fill('#dd2222')
        self.assertTrue(image.save(str(second)))
        timestamp = first.stat().st_mtime_ns
        os.utime(second, ns=(timestamp, timestamp))
        self.assertEqual(first.stat().st_size, second.stat().st_size)
        self.choose_cache('library')
        self.wait_until(lambda: self.window._cache_key == ('library', None))
        self.window.load_folder(first.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        old = self.window.cache
        old.get_thumbnail(first)
        old.set_override(self.window._displayed_item, first)
        self.window.load_folder(second.parent)
        self.wait_until(lambda: self.window._displayed_item is not None and
                        self.window._displayed_item.image_path == second)
        thumbnail = QImage(str(self.window.cache.get_thumbnail(second)))
        color = thumbnail.pixelColor(20, 20)
        self.assertGreater(color.red(), 200)
        self.assertLess(color.blue(), 80)
        self.assertIsNone(self.window.cache.get_override(self.window._displayed_item))
        self.wait_until(lambda: old._closed)

    def test_global_cache_rebind_releases_previous_gallery_readers(self):
        first, second = self.picture('A/first.jpg'), self.picture('B/second.jpg')
        self.choose_cache('library')
        self.wait_until(lambda: self.window._cache_key == ('library', None))
        self.window.load_folder(first.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        self.window.cache.set_override(self.window._displayed_item, first)
        self.choose_cache('user')
        self.wait_until(lambda: self.window._cache_location == 'user' and
                        self.window._displayed_item is not None)
        global_cache = self.window.cache
        self.assertEqual(global_cache.get_override(self.window._displayed_item), first)
        readers = [reader for reader in global_cache._owned_readers if reader.library_root == first.parent]
        self.assertTrue(readers)
        self.window.load_folder(second.parent)
        self.wait_until(lambda: self.window._displayed_item is not None and
                        self.window._displayed_item.image_path == second and
                        id(global_cache) not in self.window._cache_stores)
        self.assertIsNot(global_cache, self.window.cache)
        self.assertTrue(all(reader._closed for reader in readers))

    def test_folder_switch_keeps_old_pair_writer_owned_until_it_finishes(self):
        first, second = self.picture('A/first.jpg'), self.picture('B/second.jpg')
        self.choose_cache('library')
        self.wait_until(lambda: self.window._cache_key == ('library', None))
        self.window.load_folder(first.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        old = self.window.cache
        item = self.window._displayed_item
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        observed = []
        def held_pair(candidate, cache):
            observed.append(cache)
            started.set()
            release.wait(4)
            observed.append(cache._closed)
            cache.set_override(candidate, candidate.image_path)
            completed.set()
            return PairDecision(candidate.image_path, (), False, 'test')
        try:
            with patch('livegallery.app.resolve_pair', side_effect=held_pair):
                self.window._request_pair(item, 3)
                self.assertTrue(started.wait(1))
                self.window.load_folder(second.parent)
                self.wait_until(lambda: self.window._displayed_item is not None and
                                self.window._displayed_item.image_path == second)
                self.assertFalse(old._closed)
                self.assertIsNone(self.window.cache.get_override(self.window._displayed_item))
                release.set()
                self.assertTrue(completed.wait(1))
                self.wait_until(lambda: id(old) not in self.window._cache_stores)
        finally:
            release.set()
        self.assertEqual(observed, [old, False])
        rebound = open_cache('library', first.parent, user_base=self.base / 'data',
                             program_base=self.base / 'data' / 'program-data')
        try:
            self.assertFalse(rebound.fallback)
            self.assertEqual(rebound.cache.get_override(item), first)
        finally:
            rebound.close()

    def test_slow_stale_cache_open_is_off_gui_and_closed_after_new_folder_wins(self):
        first, second = self.picture('A/first.jpg'), self.picture('B/second.jpg')
        entered, release = threading.Event(), threading.Event()
        stale, threads = [], []
        def delayed_open(location, folder=None, **kwargs):
            threads.append(threading.current_thread())
            if folder == first.parent:
                entered.set()
                release.wait(4)
            binding = open_cache(location, folder, **kwargs)
            if folder == first.parent:
                stale.append(binding.cache)
            return binding
        try:
            with patch('livegallery.app.open_cache', side_effect=delayed_open):
                before = time.monotonic()
                self.window.load_folder(first.parent)
                self.assertLess(time.monotonic() - before, 0.3)
                self.assertTrue(entered.wait(1))
                self.window.load_folder(second.parent)
                self.wait_until(lambda: self.window._displayed_item is not None and
                                self.window._displayed_item.image_path == second)
                release.set()
                self.wait_until(lambda: stale and id(stale[0]) not in self.window._cache_stores)
                self.assertEqual(self.window._displayed_item.image_path, second)
        finally:
            release.set()
        self.assertTrue(all(worker is not threading.main_thread() for worker in threads))

    def test_deep_verification_cancels_on_folder_change_without_closing_its_cache(self):
        first, second = self.picture('A/first.jpg'), self.picture('B/second.jpg')
        self.choose_cache('library')
        self.wait_until(lambda: self.window._cache_key == ('library', None))
        self.window.load_folder(first.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        old = self.window.cache
        entered, release = threading.Event(), threading.Event()
        observed = []
        def verify(paths, cancel=None, progress=None):
            entered.set()
            release.wait(4)
            observed.append((cancel(), old._closed, threading.current_thread()))
            return {'checked': 0, 'changed': [], 'cancelled': True}
        try:
            with patch.object(old, 'verify_files', side_effect=verify):
                self.window._verify_cache()
                self.assertTrue(entered.wait(1))
                self.window.load_folder(second.parent)
                self.wait_until(lambda: self.window._displayed_item is not None and
                                self.window._displayed_item.image_path == second)
                self.assertFalse(old._closed)
                release.set()
                self.wait_until(lambda: id(old) not in self.window._cache_stores)
                self.assertFalse(self.window._verify_running)
                self.assertEqual(self.window._displayed_item.image_path, second)
        finally:
            release.set()
        self.assertEqual(observed[0][:2], (True, False))
        self.assertIsNot(observed[0][2], threading.main_thread())

    def test_deep_verification_refreshes_current_same_stat_image_and_preserves_choice(self):
        path = self.picture('same.bmp')
        self.window.load_folder(path.parent)
        self.wait_until(lambda: self.window._displayed_item is not None)
        item, cache = self.window._displayed_item, self.window.cache
        cache.get_thumbnail(path)
        cache.set_override(item, path)
        timestamp, size = path.stat().st_mtime_ns, path.stat().st_size
        image = QImage(640, 480, QImage.Format.Format_RGB32)
        image.fill('#dd2222')
        self.assertTrue(image.save(str(path)))
        os.utime(path, ns=(timestamp, timestamp))
        self.assertEqual(path.stat().st_size, size)
        self.window._verify_cache()
        self.wait_until(lambda: not self.window._verify_running and self.window._displayed_item is not None and
                        not self.window.viewer._photo.pixmap().isNull() and
                        self.window.viewer._photo.pixmap().toImage().pixelColor(20, 20).red() > 200)
        self.assertEqual(self.window._displayed_item.image_path, path)
        self.assertEqual(cache.get_override(self.window._displayed_item), path)
        self.assertIs(cache, self.window.cache)

    def test_unwritable_program_bootstrap_uses_user_cache(self):
        self.window.close()
        self.assertTrue(self.window.wait_for_shutdown(5))
        self.window.deleteLater()
        self.application.processEvents()
        Preferences(cache_location='program').save()
        user, program = self.base / 'data' / 'cache', self.base / 'data' / 'program-data' / 'cache'
        seen = []
        def attempt(base, *args, **kwargs):
            seen.append(base)
            if base == program:
                raise PermissionError('test readonly directory')
            return CacheStore(base, *args, **kwargs)
        with patch('livegallery.app.CacheStore', side_effect=attempt):
            self.window = MainWindow()
        self.assertEqual(seen, [program, user])
        self.assertEqual(self.window.cache.base, user)
        self.assertEqual(self.window.preferences.cache_location, 'program')
        self.assertIn('本机缓存', self.window._cache_warning)


class OpenWithTests(unittest.TestCase):
    def test_native_unicode_path_flags_owner_cancel_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "中文 空格 'and&symbols'.HEIC"
            target.touch()
            calls = []
            shell = MagicMock()
            def choose(owner, pointer):
                info = ctypes.cast(pointer, ctypes.POINTER(OPENASINFO)).contents
                calls.append((owner, info.pcszFile, info.pcszClass, info.oaifInFlags))
                return 0
            shell.SHOpenWithDialog.side_effect = choose
            with patch("livegallery.system_open._load_shell32", return_value=shell):
                self.assertTrue(open_with(target, 12345))
                self.assertEqual(calls, [(12345, str(target.resolve()), None,
                                          OAIF_EXEC | OAIF_HIDE_REGISTRATION)])
                self.assertEqual(shell.SHOpenWithDialog.restype, ctypes.c_int32)
                for cancelled in (HRESULT_CANCELLED, ctypes.c_int32(HRESULT_CANCELLED).value, E_ABORT):
                    shell.SHOpenWithDialog.side_effect = None
                    shell.SHOpenWithDialog.return_value = cancelled
                    self.assertFalse(open_with(target))
                shell.SHOpenWithDialog.return_value = 0x80004005
                with self.assertRaisesRegex(OSError, "80004005"):
                    open_with(target)
            with patch("livegallery.system_open._load_shell32") as load:
                with self.assertRaises(FileNotFoundError):
                    open_with(target.with_name("missing.heic"))
                load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
