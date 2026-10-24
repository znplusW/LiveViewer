"""Window integration and asynchronous-result ownership regressions."""

import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QImage, QKeyEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from livegallery.app import MainWindow, SettingsDialog
from livegallery.models import MediaItem, PairDecision
from livegallery.settings import Preferences


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

    def test_fullscreen_is_immediate_stable_and_escape_only(self):
        self.show_live_item()
        self.window.grid_button.click()
        self.window.fullscreen_button.click()
        self.assertFalse(self.window._grid_mode)
        self.assertIs(self.window.content.currentWidget(), self.window.viewer)
        self.assert_fullscreen_has_no_controls()
        self.assertTrue(self.window.film.isVisible())
        QTest.mouseMove(self.window.viewer.viewport(), QPoint(100, 100))
        self.window.viewer.controls_toggled.emit()
        self.window._show_message("后台分析已完成")
        self.window._set_chrome(True)
        QTest.keyClick(self.window.viewer, Qt.Key.Key_F11)
        self.assert_fullscreen_has_no_controls()
        self.assertTrue(self.window.film.isVisible())
        QTest.keyClick(self.window.viewer, Qt.Key.Key_Escape)
        self.assertFalse(self.window.isFullScreen())
        self.assertTrue(self.window.header.isVisible())
        self.assertTrue(self.window.film.isVisible())

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
        QTest.keyClick(self.window.viewer, Qt.Key.Key_Escape)
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


if __name__ == "__main__":
    unittest.main()
