# -*- coding: utf-8 -*-
"""外部转写接口的 HTTP、任务状态和文件生命周期回归测试。"""
from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 隔离环境（临时 DB / 上传目录 / 输出目录）——不依赖 test_web.py 的局部 fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_web(web_runtime, monkeypatch):
    import asr.web as wm
    from asr.web.runtime import WebRuntime

    monkeypatch.chdir(web_runtime.root)
    runtime = WebRuntime(web_runtime.config_path, web_runtime.db_path,
                         web_runtime.upload_dir, web_runtime.output_root)
    monkeypatch.setattr(wm.app.state, "runtime", runtime, raising=False)
    return runtime


@pytest.fixture
def client(isolated_web):
    from asr.web import app as fastapi_app
    with TestClient(fastapi_app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1") as session:
        yield session


def _fake_probe(_path):
    """假的媒体探测：声称有音轨，避免依赖真实 ffprobe。"""
    return SimpleNamespace(audio_streams=[{"index": 0}], duration_s=16.0)


def _install(monkeypatch, *, write_formats=("srt", "txt"), encoding="utf-8-sig"):
    """替换慢速的媒体探测和推理，运行真实队列与 SQLite。"""
    import asr.web as wm

    monkeypatch.setattr(wm, "probe", _fake_probe)

    def _run(input_path, cfg, overrides, **kwargs):
        out_dir = Path(overrides["output_dir"])
        files = {}
        for fmt in write_formats:
            p = out_dir / f"x.{fmt}"
            p.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试字幕\n" if fmt == "srt" else "测试正文",
                         encoding=encoding if fmt == "srt" else "utf-8")
            files[fmt] = p
        return SimpleNamespace(duration_s=16.45, elapsed_s=1, rtf=0.1, segments_count=1, written=files)

    monkeypatch.setattr(wm, "pipeline_run", _run)


def _media_file(tmp_path: Path) -> Path:
    p = tmp_path / "lecture.mp4"
    p.write_bytes(b"FAKE-MEDIA")
    return p


def _wait_for_task(client, task_id, predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        row = client.get(f"/status/{task_id}").json()
        if predicate(row):
            return row
        time.sleep(0.01)
    pytest.fail(f"任务未进入预期状态: {row}")


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"db": True}


def test_health_reports_unreadable_database(client, isolated_web):
    isolated_web.db_path.write_bytes(b"not a database")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"] == {"db": False}


# ---------------------------------------------------------------------------
# /api/transcribe —— 异步模式
# ---------------------------------------------------------------------------
def test_transcribe_local_async_accepted(client, monkeypatch, tmp_path):
    _install(monkeypatch)
    src = _media_file(tmp_path)

    r = client.post("/api/transcribe", data={"input_path": str(src), "name": "异步"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["task_id"]

    st = _wait_for_task(client, body["task_id"], lambda row: row["status"] == "done")
    assert st["name"] == "异步"


def test_transcribe_local_accepts_relative_path(client, monkeypatch, tmp_path):
    """相对路径按服务进程当前目录解析（monkeypatch.chdir 已切到 tmp）。"""
    _install(monkeypatch)
    (tmp_path / "rel.mp4").write_bytes(b"FAKE")

    r = client.post("/api/transcribe", data={"input_path": "rel.mp4"})
    assert r.status_code == 202, r.text
    assert r.json()["task_id"]


def test_transcribe_local_requires_loopback_even_with_password(client, monkeypatch, isolated_web, tmp_path):
    from asr.web import app

    isolated_web.password = "test-password"
    _install(monkeypatch)
    src = _media_file(tmp_path)
    remote = TestClient(app, client=("192.0.2.2", 50000), base_url="http://asr.local")
    response = remote.post("/api/transcribe", data={"input_path": str(src)},
                           auth=("classroom", "test-password"))
    assert response.status_code == 403
    assert list(isolated_web.output_root.iterdir()) == []


def test_delete_local_task_preserves_source_in_upload_directory(client, monkeypatch, isolated_web):
    _install(monkeypatch)
    src = _media_file(isolated_web.upload_dir)
    task_id = client.post("/api/transcribe", data={"input_path": str(src)}).json()["task_id"]
    _wait_for_task(client, task_id, lambda row: row["can_delete"])
    assert client.delete(f"/api/projects/{task_id}").status_code == 200
    assert src.read_bytes() == b"FAKE-MEDIA"
    assert not (isolated_web.output_root / task_id).exists()


# ---------------------------------------------------------------------------
# /api/transcribe —— 同步模式
# ---------------------------------------------------------------------------
def test_transcribe_local_sync_returns_formats(client, monkeypatch, tmp_path):
    _install(monkeypatch)
    src = _media_file(tmp_path)

    r = client.post("/api/transcribe", data={
        "input_path": str(src), "sync": "true", "fmts": "srt,txt"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    assert body["segments"] == 1
    assert body["duration_s"] == 16.45
    assert "测试字幕" in body["formats"]["srt"]
    assert body["formats"]["txt"] == "测试正文"
    assert set(body["available_formats"]) >= {"srt", "txt"}


def test_transcribe_sync_only_returns_requested_formats(client, monkeypatch, tmp_path):
    _install(monkeypatch)
    src = _media_file(tmp_path)

    r = client.post("/api/transcribe", data={
        "input_path": str(src), "sync": "true", "fmts": "srt"})
    assert r.status_code == 200, r.text
    assert list(r.json()["formats"]) == ["srt"]


def test_transcribe_sync_reports_missing_format(client, monkeypatch, tmp_path):
    _install(monkeypatch, write_formats=("txt",))
    response = client.post("/api/transcribe", data={
        "input_path": str(_media_file(tmp_path)), "sync": "true", "fmts": "srt,txt"})
    assert response.status_code == 200
    assert response.json()["formats"] == {"txt": "测试正文"}
    assert "srt" in response.json()["format_errors"]


def test_transcribe_sync_reads_configured_srt_encoding(client, monkeypatch, isolated_web, tmp_path):
    template = isolated_web.config_path.read_text(encoding="utf-8")
    isolated_web.config_path.write_text(template.replace("srt_encoding: utf-8-sig", "srt_encoding: gb18030"),
                                        encoding="utf-8")
    _install(monkeypatch, encoding="gb18030")
    response = client.post("/api/transcribe", data={
        "input_path": str(_media_file(tmp_path)), "sync": "true", "fmts": "srt"})
    assert response.status_code == 200
    assert "测试字幕" in response.json()["formats"]["srt"]


def test_transcribe_sync_timeout_preserves_task_id(client, monkeypatch, tmp_path):
    import asr.web as wm

    _install(monkeypatch)
    run_pipeline = wm.pipeline_run
    release = Event()

    def run(*args, **kwargs):
        assert release.wait(4)
        return run_pipeline(*args, **kwargs)

    monkeypatch.setattr(wm, "pipeline_run", run)
    try:
        response = client.post("/api/transcribe", data={
            "input_path": str(_media_file(tmp_path)), "sync": "true", "timeout": "0.05"})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
    finally:
        release.set()
    _wait_for_task(client, task_id, lambda row: row["status"] == "done")
    assert client.get(f"/download/{task_id}/srt").status_code == 200


def test_transcribe_sync_timeout_also_bounds_database_poll(client, monkeypatch, tmp_path):
    import asr.web as wm

    _install(monkeypatch)
    read_task = wm._read_local_task
    release = Event()

    def slow_read(task_id):
        release.wait(1.5)
        return read_task(task_id)

    monkeypatch.setattr(wm, "_read_local_task", slow_read)
    try:
        response = client.post("/api/transcribe", data={
            "input_path": str(_media_file(tmp_path)), "sync": "true", "timeout": "0.05"})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
    finally:
        release.set()
    _wait_for_task(client, task_id, lambda row: row["status"] == "done")


def test_transcribe_sync_returns_subtitle_during_ai(client, monkeypatch, tmp_path):
    import asr.web as wm

    _install(monkeypatch)
    run_single_task = wm._run_single_task
    release = Event()

    def run(task_id, input_path, output_dir, config_path, diarize, quality, ai_feedback):
        run_single_task(task_id, input_path, output_dir, config_path, diarize, quality, False)
        wm._repository().queue_ai(task_id, ("feedback",), owner=wm.current_owner())
        assert release.wait(4)

    monkeypatch.setattr(wm, "_run_single_task", run)
    try:
        response = client.post("/api/transcribe", data={
            "input_path": str(_media_file(tmp_path)), "sync": "true", "timeout": "1.5", "ai_feedback": "true"})
        assert response.status_code == 200
        assert "测试字幕" in response.json()["formats"]["srt"]
    finally:
        release.set()


# ---------------------------------------------------------------------------
# /api/transcribe —— 错误路径
# ---------------------------------------------------------------------------
def test_transcribe_missing_file_400(client, monkeypatch, tmp_path):
    _install(monkeypatch)
    r = client.post("/api/transcribe", data={"input_path": str(tmp_path / "nope.mp4")})
    assert r.status_code == 400
    assert "文件不存在" in r.json()["detail"]


def test_transcribe_invalid_quality_400(client, monkeypatch, tmp_path):
    _install(monkeypatch)
    src = _media_file(tmp_path)
    r = client.post("/api/transcribe", data={"input_path": str(src), "quality": "bogus"})
    assert r.status_code == 400
    assert "质量档位" in r.json()["detail"]


@pytest.mark.parametrize("fields", [{"config": "other.yaml"}, {"fmts": "srt,mp4"}, {"timeout": "inf"},
                                    {"timeout": "0"}, {"timeout": "abc"}, {"timeout": "3601"},
                                    {"timeout": ""}, {"fmts": ""}])
def test_transcribe_validates_before_creating_task(client, monkeypatch, isolated_web, tmp_path, fields):
    _install(monkeypatch)
    data = {
        "input_path": str(_media_file(tmp_path)), "sync": "true" if "config" not in fields else "false",
        "timeout": "0.05", **fields}
    response = client.post("/api/transcribe", data=data)
    assert response.status_code == 400
    assert client.get("/api/projects").json()["total"] == 0
    assert list(isolated_web.output_root.iterdir()) == []


def test_transcribe_no_audio_stream_400(client, monkeypatch, tmp_path):
    import asr.web as wm
    _install(monkeypatch)
    monkeypatch.setattr(wm, "probe", lambda _p: SimpleNamespace(audio_streams=[]))
    src = _media_file(tmp_path)
    r = client.post("/api/transcribe", data={"input_path": str(src)})
    assert r.status_code == 400
    assert "没有音轨" in r.json()["detail"]


def test_transcribe_probe_failure_400(client, monkeypatch, tmp_path):
    import asr.web as wm
    _install(monkeypatch)

    def _boom(_p):
        raise RuntimeError("ffprobe exploded")

    monkeypatch.setattr(wm, "probe", _boom)
    src = _media_file(tmp_path)
    r = client.post("/api/transcribe", data={"input_path": str(src)})
    assert r.status_code == 400
    assert "无法探测" in r.json()["detail"]
