"""Upload acceptance, cancellation and cleanup are observable HTTP contracts."""
from threading import Event
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def upload_client(web_runtime, monkeypatch):
    import asr.web as web
    from asr.web.runtime import WebRuntime
    runtime = WebRuntime(web_runtime.config_path, web_runtime.db_path,
                         web_runtime.upload_dir, web_runtime.output_root)
    monkeypatch.setattr(web.app.state, "runtime", runtime, raising=False)
    monkeypatch.setattr(web, "probe", lambda p: SimpleNamespace(audio_streams=[1]))
    with TestClient(web.app, client=("127.0.0.1", 12345), base_url="http://127.0.0.1") as client:
        yield client, web, runtime


def test_cancelled_upload_cannot_create_task_or_leave_files(upload_client):
    client, web, runtime = upload_client
    session = client.post("/api/uploads").json()
    uid = session["upload_id"]
    assert client.post(f"/api/uploads/{uid}/cancel").json()["status"] == "cancelled"
    response = client.post("/api/projects", headers={"X-Upload-Id": uid},
                           files={"files": ("lesson.wav", b"partial")})
    assert response.status_code == 409
    assert client.get("/api/projects").json()["items"] == []
    assert list(runtime.upload_dir.iterdir()) == []
    assert list(runtime.output_root.iterdir()) == []


def test_accepted_upload_exposes_task_id_and_rejects_second_task(upload_client, monkeypatch):
    client, web, runtime = upload_client
    monkeypatch.setattr(web, "_run_single_task", lambda *a: web._update_task(a[0], status="done"))
    uid = client.post("/api/uploads").json()["upload_id"]
    response = client.post("/api/projects", headers={"X-Upload-Id": uid},
                           files={"files": ("lesson.wav", b"audio")})
    assert response.status_code == 200
    tid = response.json()["task_id"]
    assert client.get(f"/api/uploads/{uid}").json() == {"upload_id": uid, "status": "accepted", "task_id": tid}
    cancelled = client.post(f"/api/uploads/{uid}/cancel")
    assert cancelled.status_code == 409 and cancelled.json()["detail"]["task_id"] == tid
    second = client.post("/api/projects", headers={"X-Upload-Id": uid}, files={"files": ("another.wav", b"a")})
    assert second.status_code == 409
    assert len(client.get("/api/projects").json()["items"]) == 1
    assert len(list(runtime.upload_dir.iterdir())) == 1


def test_probe_failure_cleans_current_unregistered_file(upload_client, monkeypatch):
    client, web, runtime = upload_client
    def bad_probe(path):
        raise RuntimeError("bad media")
    monkeypatch.setattr(web, "probe", bad_probe)
    response = client.post("/api/projects", files={"files": ("broken.wav", b"partial")})
    assert response.status_code == 400
    assert list(runtime.upload_dir.iterdir()) == []
    assert list(runtime.output_root.iterdir()) == []


def test_queue_full_leaves_upload_open_and_no_residue(upload_client, monkeypatch):
    client, web, runtime = upload_client
    from asr.web.execution import ExecutionManager
    web._executor().shutdown()
    manager = ExecutionManager(workers=1, max_waiting=0)
    web.app.state.execution = manager
    release, entered = Event(), Event()
    def blocked(*args):
        entered.set()
        release.wait(5)
        web._update_task(args[0], status="done")
    monkeypatch.setattr(web, "_run_single_task", blocked)
    try:
        first = client.post("/api/projects", files={"files": ("a.wav", b"a")})
        assert first.status_code == 200 and entered.wait(2)
        tid = first.json()["task_id"]
        assert client.delete(f"/api/projects/{tid}").status_code == 409
        uid = client.post("/api/uploads").json()["upload_id"]
        refused = client.post("/api/projects", headers={"X-Upload-Id": uid}, files={"files": ("b.wav", b"b")})
        assert refused.status_code == 503 and "retry-after" in refused.headers
        assert client.get(f"/api/uploads/{uid}").json()["status"] == "open"
        assert len(client.get("/api/projects").json()["items"]) == 1
        assert len(list(runtime.upload_dir.iterdir())) == 1
        assert len(list(runtime.output_root.iterdir())) == 1
    finally:
        release.set()


def test_cancel_during_probe_wins_and_cleans_media(upload_client, monkeypatch):
    client, web, runtime = upload_client
    entered, release = Event(), Event()
    def probe(path):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(audio_streams=[1])
    monkeypatch.setattr(web, "probe", probe)
    uid = client.post("/api/uploads").json()["upload_id"]
    with ThreadPoolExecutor(1) as pool:
        response = pool.submit(lambda: client.post("/api/projects", headers={"X-Upload-Id": uid},
                                                   files={"files": ("a.wav", b"audio")}))
        try:
            assert entered.wait(2)
            assert client.post(f"/api/uploads/{uid}/cancel").json()["status"] == "cancelled"
        finally:
            release.set()
        assert response.result().status_code == 409
    assert list(runtime.upload_dir.iterdir()) == []
    assert list(runtime.output_root.iterdir()) == []


def _done_ai_task(web, runtime):
    out = runtime.output_root / "ai"
    out.mkdir()
    web._repository().accept_task({"id": "ai", "name": "lesson", "filename": "a.wav",
                                  "input_path": str(runtime.upload_dir / "a.wav"), "output_dir": str(out)})
    old = out / "old.deep.md"
    old.write_text("完整旧稿", encoding="utf-8")
    meta = {"ai_files": {"deep": old.name}, "ai_errors": {"deep": "old error"}}
    web._repository().update("ai", status="done", ended_at="before",
                             transcript='[{"start":0,"end":1,"text":"课堂"}]',
                             meta_json=json.dumps(meta))
    return old


def test_concurrent_ai_has_one_admission_and_failed_publish_keeps_old(upload_client, monkeypatch):
    client, web, runtime = upload_client
    old = _done_ai_task(web, runtime)
    entered, release = Event(), Event()
    monkeypatch.setattr(web, "llm_enabled", lambda cfg: True)
    def chat(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return "new draft"
    def fail_publish(path, text):
        raise OSError("disk full")
    monkeypatch.setattr(web, "chat", chat)
    monkeypatch.setattr(web, "atomic_write_text", fail_publish)
    try:
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(lambda: client.post("/api/tasks/ai/ai/deep"))
            b = pool.submit(lambda: client.post("/api/tasks/ai/ai/deep"))
            assert sorted([a.result().status_code, b.result().status_code]) == [202, 409]
        assert entered.wait(2)
        assert client.get("/download/ai/deep").text == "完整旧稿"
        assert client.delete("/api/projects/ai").status_code == 409
    finally:
        release.set()
        web._executor().shutdown()
    item = client.get("/status/ai").json()
    assert item["meta"]["ai_attempts"]["deep"]["status"] == "failed"
    assert item["meta"]["ai_files"]["deep"] == old.name
    assert old.read_text(encoding="utf-8") == "完整旧稿"
    assert item["ended_at"] == "before" and item["transcription_status"] == "done"


def test_watchdog_failure_does_not_make_active_task_deletable_or_revive(upload_client, monkeypatch):
    client, web, runtime = upload_client
    entered, release = Event(), Event()
    monkeypatch.setattr(web, "ASR_STALL_TIMEOUT_S", 0.04)
    def pipeline(path, cfg, overrides, *, on_progress, on_segments):
        entered.set()
        assert release.wait(3)
        on_progress("asr", 0.9, "late")
        on_segments([SimpleNamespace(start=0, end=1, text="late")])
        return SimpleNamespace(duration_s=1, elapsed_s=1, rtf=1, segments_count=0, written={}, segments=[])
    monkeypatch.setattr(web, "pipeline_run", pipeline)
    response = client.post("/api/projects", files={"files": ("a.wav", b"a")})
    tid = response.json()["task_id"]
    try:
        assert entered.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            item = client.get(f"/status/{tid}").json()
            if item["status"] == "failed":
                break
            time.sleep(0.01)
        assert item["status"] == "failed" and item["execution_active"] and not item["can_delete"]
        assert client.delete(f"/api/projects/{tid}").status_code == 409
    finally:
        release.set()
        web._executor().shutdown()
    final = client.get(f"/status/{tid}").json()
    assert final["status"] == "failed" and final["transcript"] == ""
    assert final["can_delete"]


def test_registered_paths_never_fall_back_to_unregistered_files(upload_client):
    client, web, runtime = upload_client
    _done_ai_task(web, runtime)
    out = runtime.output_root / "ai"
    outside = runtime.output_root / "private.txt"
    outside.write_text("private", encoding="utf-8")
    (out / "stale.txt").write_text("stale", encoding="utf-8")
    web._repository().update("ai", meta_json=json.dumps({"output_files": {"txt": "../private.txt"},
                                                       "ai_files": {"deep": "../private.txt"},
                                                       "part_files": ["../private.txt"]}))
    for suffix in ("txt", "deep", "part_srt/0"):
        assert client.get(f"/download/ai/{suffix}").status_code == 404
    assert client.get("/api/tasks/ai/ai/deep").status_code == 404
    assert client.get("/status/ai").json()["available_formats"] == []


def test_ai_queue_full_preserves_status_errors_and_previous_files(upload_client, monkeypatch):
    client, web, runtime = upload_client
    from asr.web.execution import ExecutionManager
    _done_ai_task(web, runtime)
    before = web._repository().get("ai")
    web._executor().shutdown()
    web.app.state.execution = ExecutionManager(workers=1, max_waiting=0)
    release = Event()
    web._executor().submit("other", lambda: release.wait(3))
    monkeypatch.setattr(web, "llm_enabled", lambda cfg: True)
    try:
        response = client.post("/api/tasks/ai/ai/deep")
        assert response.status_code == 503
        after = web._repository().get("ai")
        assert after["status"] == before["status"] == "done"
        assert after["meta"] == before["meta"]
    finally:
        release.set()


def test_second_media_probe_failure_cleans_all_files(upload_client, monkeypatch):
    client, web, runtime = upload_client
    def probe(path):
        if path.name.startswith("01_"):
            raise RuntimeError("second media corrupt")
        return SimpleNamespace(audio_streams=[1])
    monkeypatch.setattr(web, "probe", probe)
    response = client.post("/api/projects", files=[("files", ("first.wav", b"one")),
                                                   ("files", ("second.wav", b"two"))])
    assert response.status_code == 400
    assert list(runtime.upload_dir.iterdir()) == []
    assert list(runtime.output_root.iterdir()) == []
    assert client.get("/api/projects").json()["items"] == []


def test_upload_read_failure_cleans_current_partial_file(upload_client):
    import asyncio
    client, web, runtime = upload_client
    class BrokenUpload:
        filename = "broken.wav"
        reads = 0
        async def read(self, size):
            self.reads += 1
            if self.reads == 1:
                return b"partial media"
            raise OSError("connection interrupted")
    with pytest.raises(OSError):
        asyncio.run(web._accept_upload([BrokenUpload()], "", "default", False, "balanced", False, None))
    assert list(runtime.upload_dir.iterdir()) == []
    assert list(runtime.output_root.iterdir()) == []


def test_config_validation_errors_never_echo_key_values(upload_client, monkeypatch):
    client, web, runtime = upload_client
    from asr.config import ConfigLoadError
    def broken_config(*args, **kwargs):
        raise ConfigLoadError("invalid llm.api_key=private-key")
    monkeypatch.setattr(web, "load_config", broken_config)
    response = client.post("/api/projects", files={"files": ("a.wav", b"a")})
    tid = response.json()["task_id"]
    web._executor().shutdown()
    status = client.get(f"/status/{tid}")
    assert status.json()["status"] == "failed"
    assert "private-key" not in status.text
