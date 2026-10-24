"""A small bounded-concurrency queue; current media outranks background work."""
import itertools
import queue
import threading
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
        for number in range(workers):
            threading.Thread(target=self._work, name=f'media-{number}', daemon=True).start()

    def submit(self, key, function, priority=5):
        with self._lock:
            if key in self._pending or self._closed.is_set():
                return False
            self._pending.add(key)
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
        self._closed.set()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
