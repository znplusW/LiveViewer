"""Window integration and asynchronous-result ownership regressions."""

import ctypes
import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QImage, QKeyEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from livegallery.app import MainWindow, SettingsDialog
from livegallery.models import MediaItem, PairDecision
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
