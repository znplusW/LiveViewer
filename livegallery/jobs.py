"""Bounded, cancellable media work with a reserved interactive worker."""
import itertools
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal


@dataclass
class _Job:
    key: str
    function: object
    priority: int
    order: int
    cancelled: bool = False
    owner: object = None


class JobQueue(QObject):
    finished = Signal(str, object, object)
    discarded = Signal(str)

    def __init__(self, parent=None, workers=2, max_pending=96):
        super().__init__(parent)
        self._counter = itertools.count()
        self._closed = threading.Event()
        self._pending = {}
        self._running = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._max_pending = max(1, int(max_pending))
        self._threads = []
        count = max(1, int(workers))
        for number in range(count):
            # Slow video probing/thumbnail I/O cannot occupy the current-photo
            # worker. A one-worker test queue still handles every priority.
            lane = 'any' if count == 1 else ('interactive' if number == 0 else 'background')
            worker = threading.Thread(target=self._work, args=(lane,), name=f'media-{number}', daemon=True)
            self._threads.append(worker)
            worker.start()

    @property
    def queued_count(self):
        with self._lock:
            return len(self._pending)

    def submit(self, key, function, priority=5, *, owner=None):
        evicted = None
        with self._condition:
            if self._closed.is_set():
                return False
            queued = self._pending.get(key)
            if queued is not None:
                queued.priority = min(queued.priority, priority)
                queued.function = function
                queued.owner = owner
                self._condition.notify_all()
                return True
            if any(job.key == key and not job.cancelled for job in self._running.values()):
                return False
            if len(self._pending) >= self._max_pending:
                worst = max(self._pending.values(), key=lambda job: (job.priority, -job.order))
                if priority > worst.priority:
                    return False
                evicted = self._pending.pop(worst.key)
                evicted.cancelled = True
            self._pending[key] = _Job(key, function, priority, next(self._counter), owner=owner)
            self._condition.notify_all()
        if evicted is not None:
            self.discarded.emit(evicted.key)
        return True

    def promote(self, key, priority=0):
        with self._condition:
            job = self._pending.get(key)
            if job is None:
                return False
            job.priority = min(priority, job.priority)
            self._condition.notify_all()
            return True

    def cancel_where(self, predicate, *, include_running=True):
        """Remove obsolete queued work; suppress results of active native reads.

        A running decoder cannot safely be interrupted inside a native library.
        Its result is discarded, and no other obsolete queued read is started.
        """
        discarded = set()
        with self._condition:
            for key, job in list(self._pending.items()):
                if predicate(key):
                    self._pending.pop(key)
                    job.cancelled = True
                    discarded.add(key)
            if include_running:
                for job in self._running.values():
                    if not job.cancelled and predicate(job.key):
                        job.cancelled = True
                        discarded.add(job.key)
            self._condition.notify_all()
        for key in discarded:
            self.discarded.emit(key)
        return discarded

    def _next(self, lane):
        running_keys = {job.key for job in self._running.values()}
        available = [job for job in self._pending.values() if job.key not in running_keys
                     and (lane == 'any' or (job.priority <= 1) == (lane == 'interactive'))]
        if not available:
            return None
        job = min(available, key=lambda candidate: (candidate.priority, candidate.order))
        self._pending.pop(job.key)
        self._running[job.order] = job
        return job

    def _work(self, lane):
        while True:
            with self._condition:
                job = self._next(lane)
                while job is None and not self._closed.is_set():
                    self._condition.wait()
                    job = self._next(lane)
                if self._closed.is_set():
                    return
            result, error = None, None
            try:
                if not job.cancelled:
                    result = job.function()
            except Exception as exception:
                error = str(exception)
            finally:
                with self._condition:
                    self._running.pop(job.order, None)
                    publish = not job.cancelled and not self._closed.is_set()
                    self._condition.notify_all()
            if publish:
                self.finished.emit(job.key, result, error)

    def close(self):
        """Discard pending work without waiting on active native operations."""
        with self._condition:
            self._closed.set()
            self._pending.clear()
            for job in self._running.values():
                job.cancelled = True
            self._condition.notify_all()

    def wait_owner(self, owner, timeout: float | None = None) -> bool:
        """Wait off the GUI thread until every operation using an owner settles.

        Cancelled native reads remain running until their function returns, so
        a cache/database cannot be closed merely because its result was dropped.
        Unrelated owners and the reserved current-image lane keep progressing.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while any(job.owner is owner for job in (*self._pending.values(), *self._running.values())):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        for worker in self._threads:
            if worker is threading.current_thread():
                return False
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            worker.join(remaining)
        return all(not worker.is_alive() for worker in self._threads)
