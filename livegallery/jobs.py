"""A small bounded-concurrency queue; current media outranks background work."""
import itertools
import queue
import threading
import time
from PySide6.QtCore import QObject, Signal


class JobQueue(QObject):
    finished = Signal(str, object, object)

    def __init__(self, parent=None, workers=2):
        super().__init__(parent)
        self._queue = queue.PriorityQueue()
        self._counter = itertools.count()
        self._closed = threading.Event()
        self._pending = set()
        self._lock = threading.Lock()
        self._threads = []
        for number in range(workers):
            worker = threading.Thread(target=self._work, name=f'media-{number}', daemon=True)
            self._threads.append(worker)
            worker.start()

    def submit(self, key, function, priority=5):
        with self._lock:
            if key in self._pending or self._closed.is_set():
                return False
            self._pending.add(key)
            # Enqueue under the same lock as close() so a submission cannot
            # appear after shutdown has already drained the queue.
            self._queue.put((priority, next(self._counter), key, function))
        return True

    def _work(self):
        while not self._closed.is_set():
            try:
                _, _, key, function = self._queue.get(timeout=.3)
            except queue.Empty:
                continue
            result, error = None, None
            try:
                if not self._closed.is_set():
                    result = function()
            except Exception as exception:
                error = str(exception)
            finally:
                with self._lock:
                    self._pending.discard(key)
                self._queue.task_done()
            if not self._closed.is_set():
                self.finished.emit(key, result, error)

    def close(self):
        """Stop accepting work and discard queued jobs without blocking the UI."""
        with self._lock:
            self._closed.set()
            while True:
                try:
                    _, _, key, _ = self._queue.get_nowait()
                    self._pending.discard(key)
                    self._queue.task_done()
                except queue.Empty:
                    break

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for running workers, using one timeout for the entire queue.

        Call close() first. A false result means a worker is still using its
        resources; its cache/database must remain open until a later wait ends.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        for worker in self._threads:
            if worker is threading.current_thread():
                return False
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            worker.join(remaining)
        return all(not worker.is_alive() for worker in self._threads)
