"""SQLite admission is the authority for task and upload state."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest


def test_upload_cancel_and_accept_have_exactly_one_winner(tmp_path):
    from asr.web.repository import TaskRepository, AdmissionError
    repo = TaskRepository(tmp_path / "state.db")
    repo.initialize()
    session = repo.create_upload()
    gate = Barrier(2)

    def compete(action):
        gate.wait()
        try:
            return action()
        except AdmissionError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(compete, lambda: repo.accept_task(
            {"id": "lesson", "filename": "a.wav", "input_path": "a.wav", "output_dir": "out"},
            session["upload_id"]))
        b = pool.submit(compete, lambda: repo.cancel_upload(session["upload_id"]))
        a.result(), b.result()
    state = repo.get_upload(session["upload_id"])
    assert (state["status"], repo.get("lesson") is not None) in {
        ("accepted", True), ("cancelled", False)}
    if state["status"] == "accepted":
        assert state["task_id"] == "lesson"


def test_ai_admission_is_compare_and_swap_and_keeps_old_artifact(tmp_path):
    from asr.web.repository import TaskRepository, AdmissionError
    repo = TaskRepository(tmp_path / "state.db")
    repo.initialize()
    repo.accept_task({"id": "a", "filename": "a", "input_path": "a", "output_dir": "out"})
    repo.update("a", status="done", ended_at="yesterday", meta_json='{"ai_files":{"deep":"old.md"}}')
    repo.queue_ai("a", ("deep",))
    with pytest.raises(AdmissionError, match="task_busy"):
        repo.queue_ai("a", ("deep",))
    row = repo.get("a")
    assert row["meta"]["ai_files"] == {"deep": "old.md"}
    assert row["meta"]["ai_attempts"]["deep"]["status"] == "queued"
    assert row["ended_at"] == "yesterday"


def test_restart_marks_asr_interrupted_and_preserves_completed_transcription(tmp_path):
    from asr.web.repository import TaskRepository
    repo = TaskRepository(tmp_path / "state.db")
    repo.initialize()
    for tid in ("asr", "ai"):
        repo.accept_task({"id": tid, "filename": "a", "input_path": "a", "output_dir": "out"})
    repo.update("asr", status="running", stage="asr")
    repo.update("ai", status="done", transcript='[{"text":"完整稿"}]', ended_at="before")
    repo.queue_ai("ai", ("deep",))
    repo.recover_interrupted()
    assert repo.get("asr")["status"] == "failed"
    ai = repo.get("ai")
    assert ai["status"] == "done" and ai["transcript"] == '[{"text":"完整稿"}]'
    assert ai["ended_at"] == "before"
    assert ai["meta"]["ai_attempts"]["deep"]["status"] == "failed"
