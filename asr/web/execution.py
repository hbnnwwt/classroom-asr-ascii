"""One bounded executor owns all task work, including the automatic AI handoff."""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

_local = threading.local()


def current_owner():
    return getattr(_local, "owner", None)


class QueueFull(Exception):
    pass


class TaskBusy(Exception):
    pass


class ExecutionManager:
    def __init__(self, workers=1, max_waiting=8):
        if workers < 1 or max_waiting < 0:
            raise ValueError("invalid execution capacity")
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="classroom-task")
        self._capacity = workers + max_waiting
        self._lock = threading.RLock()
        self._active = {}
        self._accepting = True

    @contextmanager
    def mutation_lock(self):
        with self._lock:
            yield

    def is_active(self, task_id):
        with self._lock:
            return task_id in self._active

    def submit(self, task_id, fn, *, admit=None):
        with self._lock:
            if task_id in self._active:
                raise TaskBusy(task_id)
            if not self._accepting or len(self._active) >= self._capacity:
                raise QueueFull()
            owner = uuid.uuid4().hex
            self._active[task_id] = owner
            try:
                if admit:
                    admit()
                return self._pool.submit(self._run, task_id, owner, fn)
            except BaseException:
                self._active.pop(task_id, None)
                raise

    def _run(self, task_id, owner, fn):
        _local.owner = owner
        try:
            return fn()
        finally:
            _local.owner = None
            with self._lock:
                if self._active.get(task_id) == owner:
                    self._active.pop(task_id)

    def shutdown(self):
        with self._lock:
            self._accepting = False
        # Native inference cannot be killed safely. The caller releases models only after this joins.
        self._pool.shutdown(wait=True)
