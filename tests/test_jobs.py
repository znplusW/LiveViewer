"""Queue responsiveness uses gates and execution order, never timing claims."""
import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from livegallery.jobs import JobQueue


class JobQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_reserved_current_lane_bypasses_blocked_video_analysis(self):
        queue = JobQueue(workers=2)
        started, release, current = threading.Event(), threading.Event(), threading.Event()
        def blocked_background():
            started.set()
            release.wait(3)
        try:
            queue.submit("pair-old", blocked_background, 12)
            self.assertTrue(started.wait(1))
            queue.submit("current-photo", current.set, 0)
            self.assertTrue(current.wait(1), "Current photo waited behind a blocked background analysis")
            self.assertFalse(release.is_set())
        finally:
            queue.close()
            release.set()
            self.assertTrue(queue.wait(3))

    def test_hundred_rapid_current_requests_execute_only_latest_after_active_read(self):
        queue = JobQueue(workers=2, max_pending=16)
        started, release, newest = threading.Event(), threading.Event(), threading.Event()
        executed = []
        def active_native_read():
            started.set()
            release.wait(3)
            executed.append("already-running")
        try:
            queue.submit("photo-0", active_native_read, 0)
            self.assertTrue(started.wait(1))
            for index in range(1, 101):
                queue.cancel_where(lambda key: key.startswith("photo-"))
                def decode(index=index):
                    executed.append(index)
                    if index == 100:
                        newest.set()
                queue.submit(f"photo-{index}", decode, 0)
                self.assertLessEqual(queue.queued_count, 1)
            release.set()
            self.assertTrue(newest.wait(1))
            self.assertEqual(executed, ["already-running", 100])
        finally:
            queue.close()
            release.set()
            self.assertTrue(queue.wait(3))

    def test_queued_neighbor_promotes_into_current_lane_without_duplicate_decode(self):
        queue = JobQueue(workers=2)
        started, release, decoded = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def background():
            started.set()
            release.wait(3)
        try:
            queue.submit("background-probe", background, 12)
            self.assertTrue(started.wait(1))
            queue.submit("neighbor", lambda: (calls.append("decoded"), decoded.set()), 5)
            self.assertTrue(queue.promote("neighbor", 0))
            self.assertTrue(decoded.wait(1))
            self.assertEqual(calls, ["decoded"])
            self.assertFalse(release.is_set())
        finally:
            queue.close()
            release.set()
            self.assertTrue(queue.wait(3))

    def test_bounded_queue_and_close_drop_work_and_suppress_cancelled_result(self):
        queue = JobQueue(workers=1, max_pending=12)
        started, release = threading.Event(), threading.Event()
        decoded, finished = [], []
        queue.finished.connect(lambda key, _result, _error: finished.append(key))
        def blocked():
            started.set()
            release.wait(3)
        try:
            queue.submit("running", blocked, 0)
            self.assertTrue(started.wait(1))
            for index in range(300):
                queue.submit(f"obsolete-full-{index}", lambda index=index: decoded.append(index), 1)
            self.assertEqual(queue.queued_count, 12)
            before = time.monotonic()
            queue.close()
            self.assertLess(time.monotonic() - before, .5)
            self.assertEqual(queue.queued_count, 0)
            self.assertFalse(queue.submit("after-close", lambda: None, 0))
            self.assertFalse(queue.wait(.01))
            release.set()
            self.assertTrue(queue.wait(1))
            self.application.processEvents()
            self.assertEqual(decoded, [])
            self.assertEqual(finished, [])
        finally:
            queue.close()
            release.set()
            self.assertTrue(queue.wait(3))

    def test_cancelled_running_owner_is_not_idle_until_function_finishes(self):
        queue = JobQueue(workers=2)
        first, second = object(), object()
        started, release, other = threading.Event(), threading.Event(), threading.Event()
        def work():
            started.set()
            release.wait(3)
        try:
            queue.submit('old-cache', work, 0, owner=first)
            self.assertTrue(started.wait(1))
            queue.cancel_where(lambda key: key == 'old-cache')
            self.assertFalse(queue.wait_owner(first, .02))
            queue.submit('new-cache', other.set, 4, owner=second)
            self.assertTrue(other.wait(1))
            self.assertTrue(queue.wait_owner(second, 1))
            release.set()
            self.assertTrue(queue.wait_owner(first, 1))
        finally:
            release.set()
            queue.close()
            self.assertTrue(queue.wait(3))


if __name__ == "__main__":
    unittest.main()
