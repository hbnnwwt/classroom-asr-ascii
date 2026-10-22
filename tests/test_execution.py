"""Bounded execution and ownership survive terminal-state callbacks."""
from threading import Event

import pytest


def test_bounded_execution_rejects_without_calling_admission():
    from asr.web.execution import ExecutionManager, QueueFull
    manager = ExecutionManager(workers=1, max_waiting=1)
    gate = Event()
    entered = Event()
    def work():
        entered.set()
        gate.wait(5)
    try:
        manager.submit("a", work)
        assert entered.wait(2)
        manager.submit("b", lambda: None)
        admitted = []
        with pytest.raises(QueueFull):
            manager.submit("c", lambda: None, admit=lambda: admitted.append(True))
        assert admitted == []
        assert manager.is_active("a") and manager.is_active("b")
    finally:
        gate.set()
        manager.shutdown()
    assert not manager.is_active("a")


def test_failed_admission_releases_slot_and_duplicate_is_busy():
    from asr.web.execution import ExecutionManager, TaskBusy
    manager = ExecutionManager(workers=1, max_waiting=0)
    gate = Event()
    def refuse():
        raise ValueError("rejected")
    try:
        with pytest.raises(ValueError):
            manager.submit("a", lambda: None, admit=refuse)
        manager.submit("a", lambda: gate.wait(5))
        with pytest.raises(TaskBusy):
            manager.submit("a", lambda: None)
    finally:
        gate.set()
        manager.shutdown()
