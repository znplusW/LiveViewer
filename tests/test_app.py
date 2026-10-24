"""Window integration and asynchronous-result ownership regressions."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from livegallery.app import MainWindow
from livegallery.models import MediaItem, PairDecision


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
        for _ in range(100):
            if not self.window.jobs._pending:
                break
            QTest.qWait(10)
        if not self.window.jobs._pending:
            self.window.cache.close()
        self.window.deleteLater()
        self.application.processEvents()
        self.environment.stop()
        self.temporary.cleanup()

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
        self.window.kind_filter.setCurrentIndex(3)
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


if __name__ == "__main__":
    unittest.main()
