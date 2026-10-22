"""可重复的 HTTP→真实管线→产物→AI→恢复流程；只用合成音频与假模型。"""
import io
import json
import re
import time
import wave
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from asr.engines.base import Segment
from asr.config import write_llm_section
from asr.llm import LLMError


def wait_until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("background workflow did not reach expected state")


def synthetic_wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)
    return data.getvalue()


@pytest.mark.parametrize("formats", [["srt"], ["txt", "md", "srt", "vtt", "json"]])
def test_upload_pipeline_ai_preservation_restart_and_delete(monkeypatch, web_runtime, formats):
    import asr.web as web
    import asr.pipeline as pipeline
    from asr.web.runtime import WebRuntime
    template = web_runtime.config_path.read_text(encoding="utf-8")
    template, replacements = re.subn(r"^  formats:.*$", "  formats: " + json.dumps(formats), template, flags=re.M)
    assert replacements == 1
    template = write_llm_section(template, {"base_url": "http://fake.invalid/v1", "api_key": "fake-key",
                                            "model": "fake-model", "timeout": 1, "max_tokens": 100})
    web_runtime.config_path.write_text(template, encoding="utf-8")
    glossary = web_runtime.root / "glossary/default.txt"
    glossary.parent.mkdir()
    glossary.write_text("课堂结束 -> 课程结束\n", encoding="utf-8")
    runtime = WebRuntime(web_runtime.config_path, web_runtime.db_path, web_runtime.upload_dir,
                         web_runtime.output_root)
    monkeypatch.setattr(web.app.state, "runtime", runtime, raising=False)
    metadata = SimpleNamespace(audio_streams=[1], duration_s=1)
    monkeypatch.setattr(web, "probe", lambda p: metadata)
    monkeypatch.setattr(pipeline, "probe", lambda p: metadata)
    entered, release = Event(), Event()
    def transcribe(wav, hotwords=None, *, on_segments=None, on_progress=None, options=None):
        assert options.language == "zh"
        partial = [Segment(0, .4, "百分之九十九")]
        on_segments(partial)
        entered.set()
        assert release.wait(4)
        on_progress(1, "fake completed")
        return partial + [Segment(.5, .9, "课堂结束。")]
    monkeypatch.setattr(pipeline, "build_engine", lambda *a, **k: SimpleNamespace(transcribe=transcribe))
    try:
        with TestClient(web.app, client=("127.0.0.1", 45678), base_url="http://127.0.0.1") as client:
            ticket = client.post("/api/uploads").json()["upload_id"]
            response = client.post("/api/projects", headers={"X-Upload-Id": ticket},
                                   files={"files": ("lesson.1.wav", synthetic_wav(), "audio/wav")})
            assert response.status_code == 200
            task_id = response.json()["task_id"]
            assert entered.wait(3)
            interim = client.get(f"/status/{task_id}").json()
            assert "百分之九十九" in interim["transcript"]
            assert interim["execution_active"] and not interim["can_delete"]
            assert client.delete(f"/api/projects/{task_id}").status_code == 409
            release.set()
            def finished():
                state = client.get(f"/status/{task_id}").json()
                return state if state["status"] == "done" and not state["execution_active"] else None
            done = wait_until(finished)
            assert json.loads(done["transcript"])[0]["text"] == "99%"
            assert json.loads(done["transcript"])[1]["text"] == "课程结束。"
            assert set(done["available_formats"]) == set(formats) | {"replace.log"}
            assert done["meta"]["quality"] == "balanced"
            for fmt in formats:
                assert client.get(f"/download/{task_id}/{fmt}").status_code == 200
            assert client.get(f"/download/{task_id}/replace.log").status_code == 200
            ended_at = done["ended_at"]
            ai_entered, ai_release = Event(), Event()
            def good_chat(*args, **kwargs):
                ai_entered.set()
                assert ai_release.wait(4)
                return "# 完整点评\n\n保留最后成功版本。"
            monkeypatch.setattr(web, "chat", good_chat)
            assert client.post(f"/api/tasks/{task_id}/ai/feedback").status_code == 202
            assert ai_entered.wait(3)
            assert client.post(f"/api/tasks/{task_id}/ai/feedback").status_code == 409
            ai_state = client.get(f"/status/{task_id}").json()
            assert ai_state["transcription_status"] == "done"
            assert client.get(f"/download/{task_id}/srt").status_code == 200
            ai_release.set()
            success = wait_until(finished)
            old_name = success["meta"]["ai_files"]["feedback"]
            last_good = client.get(f"/download/{task_id}/feedback").content
            def failed_chat(*args, **kwargs):
                raise LLMError("finish_reason=length")
            monkeypatch.setattr(web, "chat", failed_chat)
            assert client.post(f"/api/tasks/{task_id}/ai/feedback").status_code == 202
            failed = wait_until(finished)
            assert failed["meta"]["ai_files"]["feedback"] == old_name
            assert failed["meta"]["ai_attempts"]["feedback"]["status"] == "failed"
            assert client.get(f"/download/{task_id}/feedback").content == last_good
            assert failed["ended_at"] == ended_at
            assert client.patch(f"/api/projects/{task_id}", json={"name": "重命名后"}).status_code == 200
            assert client.get(f"/download/{task_id}/srt").status_code == 200
            web._repository().queue_ai(task_id, ("feedback",))
            web._repository().accept_task({"id": "interrupted", "filename": "pending.wav",
                                          "input_path": str(runtime.upload_dir / "pending.wav"),
                                          "output_dir": str(runtime.output_root / "interrupted")})
        with TestClient(web.app, client=("127.0.0.1", 45678), base_url="http://127.0.0.1") as client:
            restored = client.get(f"/status/{task_id}").json()
            assert restored["status"] == "done"
            assert restored["meta"]["ai_attempts"]["feedback"]["status"] == "failed"
            assert restored["ended_at"] == ended_at
            assert client.get(f"/download/{task_id}/feedback").content == last_good
            assert client.get("/status/interrupted").json()["status"] == "failed"
            assert client.delete(f"/api/projects/{task_id}").status_code == 200
            assert client.get(f"/status/{task_id}").status_code == 404
            assert not (runtime.output_root / task_id).exists()
    finally:
        release.set()
        if "ai_release" in locals():
            ai_release.set()


def test_empty_final_srt_transcript_replaces_nonempty_progress(monkeypatch, web_runtime):
    import asr.web as web
    import asr.pipeline as pipeline
    from asr.web.runtime import WebRuntime
    path = web_runtime.config_path
    template = re.sub(r"^  formats:.*$", '  formats: ["srt"]', path.read_text(encoding="utf-8"), flags=re.M)
    path.write_text(template, encoding="utf-8")
    runtime = WebRuntime(path, web_runtime.db_path, web_runtime.upload_dir, web_runtime.output_root)
    monkeypatch.setattr(web.app.state, "runtime", runtime, raising=False)
    monkeypatch.setattr(web, "probe", lambda p: SimpleNamespace(audio_streams=[1], duration_s=1))
    monkeypatch.setattr(pipeline, "probe", lambda p: SimpleNamespace(duration_s=1))
    entered, release = Event(), Event()
    def transcribe(*args, on_segments=None, **kwargs):
        segments = [Segment(2, 3, "时间越界的渐进文字")]
        on_segments(segments)
        entered.set()
        assert release.wait(4)
        return segments
    monkeypatch.setattr(pipeline, "build_engine", lambda *a, **k: SimpleNamespace(transcribe=transcribe))
    try:
        with TestClient(web.app, client=("127.0.0.1", 12345), base_url="http://127.0.0.1") as client:
            response = client.post("/api/projects", files={"files": ("empty.wav", synthetic_wav(), "audio/wav")})
            task_id = response.json()["task_id"]
            assert entered.wait(3)
            assert "渐进文字" in client.get(f"/status/{task_id}").json()["transcript"]
            release.set()
            def finished():
                row = client.get(f"/status/{task_id}").json()
                return row if row["status"] == "done" and not row["execution_active"] else None
            final = wait_until(finished)
            assert final["meta"]["segments_count"] == 0
            assert final["transcript"] == "[]"
    finally:
        release.set()
