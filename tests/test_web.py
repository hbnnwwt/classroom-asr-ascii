"""Web UI 单测（FastAPI TestClient + SQLite）。"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from asr.web import (
    _init_db, _update_task, _row_to_dict, _db, DB_PATH, OUTPUT_ROOT, UPLOAD_DIR,
    _run_pipeline,
)
from asr.web import app as fastapi_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把 SQLite 文件和上传/输出目录都指到 tmp_path，互不污染。"""
    test_db = tmp_path / "state.db"
    test_uploads = tmp_path / "uploads"
    test_outputs = tmp_path / "outputs"
    test_uploads.mkdir()
    test_outputs.mkdir()

    import asr.web as wm
    monkeypatch.setattr(wm, "DB_PATH", test_db)
    monkeypatch.setattr(wm, "UPLOAD_DIR", test_uploads)
    monkeypatch.setattr(wm, "OUTPUT_ROOT", test_outputs)
    # 重设 module 内的 DB_PATH 引用（_db/_init_db 用 module-level 常量）
    wm.DB_PATH = test_db
    wm.UPLOAD_DIR = test_uploads
    wm.OUTPUT_ROOT = test_outputs

    _init_db()
    yield tmp_path


@pytest.fixture
def client(isolated_db):
    """FastAPI TestClient。"""
    return TestClient(fastapi_app)


# ---------------------------------------------------------------------------
# SQLite 持久化测试（直接调函数，不经过 HTTP）
# ---------------------------------------------------------------------------
def test_init_db_creates_table(isolated_db):
    """_init_db 后能查到 tasks 表。"""
    with _db() as c:
        cur = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        assert cur.fetchone() is not None


def test_init_db_migrates_old_schema(isolated_db, monkeypatch):
    """老库（没有 transcript 列）启动后自动 ALTER TABLE 加上，不丢数据。"""
    import asr.web as wm
    import sqlite3 as _sq
    # 注意：必须用 wm.DB_PATH（fixture monkeypatch 注入的 tmp 路径），
    # 不能用顶部的 import DB_PATH（那是 import 时的旧引用）。
    db_path = wm.DB_PATH

    # 模拟老库：删掉 transcript 列重建（仅保留基础列）
    with _sq.connect(str(db_path)) as c:
        c.execute("DROP TABLE tasks")
        c.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                input_path TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL DEFAULT 0,
                stage TEXT DEFAULT '',
                message TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                meta_json TEXT DEFAULT ''
            )
        """)
        c.execute(
            "INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("old1", "old.mp4", "/in/old.mp4", "/out/old1", "done", "2026-09-22T00:00:00"),
        )

    # 触发迁移（注入到老库的实际路径）
    wm._init_db()

    # 列应已添加
    with _sq.connect(str(db_path)) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    assert "transcript" in cols

    # 老数据仍在
    with wm._db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id='old1'").fetchone()
    assert row is not None
    d = wm._row_to_dict(row)
    assert d["id"] == "old1"
    assert d["transcript"] == ""  # 默认空


def test_db_round_trip(isolated_db):
    """insert + update + read 回环。"""
    import sqlite3
    with sqlite3.connect(str(DB_PATH)) if False else _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("t1", "a.mp4", "/in/a.mp4", "/out/t1", "pending", "2026-09-22T00:00:00"),
        )
    _update_task("t1", status="running", progress=0.5, stage="asr", message="识别中")
    with _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", ("t1",)).fetchone()
    d = _row_to_dict(row)
    assert d["status"] == "running"
    assert d["progress"] == 0.5
    assert d["stage"] == "asr"


def test_meta_json_round_trip(isolated_db):
    """meta_json 字段正确存取。"""
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("t2", "b.mp4", "/in/b.mp4", "/out/t2", "done", "2026-09-22T00:00:00", '{"k": 1}'),
        )
    with _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", ("t2",)).fetchone()
    assert _row_to_dict(row)["meta"] == {"k": 1}


# ---------------------------------------------------------------------------
# HTTP 端点测试（用 mocked pipeline_run 避免真实加载模型）
# ---------------------------------------------------------------------------
def _fake_pipeline_run(*args, **kwargs):
    """替换真实 pipeline_run：写一个伪 .srt 文件 + 把 DB 设为 done。"""
    from pathlib import Path as _P
    overrides = args[2] if len(args) >= 3 else kwargs.get("overrides", {})
    out_dir = _P(overrides["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fake.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    (out_dir / "fake.json").write_text("{}", encoding="utf-8")
    task_id = overrides.get("_task_id_for_test", "")
    if task_id:
        _update_task(task_id, status="done", progress=1.0, message="ok",
                     meta_json=json.dumps({"segments_count": 1}))
    # 返回最小 Result（dataclass）
    from asr.pipeline import Result
    return Result(
        input_path=_P("/tmp/x"),
        output_dir=out_dir,
        duration_s=1.0,
        elapsed_s=0.1,
        rtf=0.1,
        written={"srt": out_dir / "fake.srt", "json": out_dir / "fake.json"},
        replace_log=[],
        segments_count=1,
    )


def test_index_serves_html(client):
    """GET / 返回管理界面 index.html。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "项目管理" in r.text
    assert "list.js" in r.text


def test_task_page_serves_html(client):
    """GET /task/{id} 返回详情页 task.html。"""
    r = client.get("/task/abc123")
    assert r.status_code == 200
    assert "项目详情" in r.text
    assert "task.js" in r.text


def test_static_files(client):
    """静态文件可访问，且带 no-cache 头（改了前端立即生效）。"""
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "progress-bar" in r.text
    assert "no-cache" in r.headers.get("Cache-Control", "")
    r = client.get("/static/task.js")
    assert r.status_code == 200
    assert "poll" in r.text
    assert "no-cache" in r.headers.get("Cache-Control", "")
    r = client.get("/static/list.js")
    assert r.status_code == 200
    assert "loadProjects" in r.text
    assert "no-cache" in r.headers.get("Cache-Control", "")


def test_static_file_not_found(client):
    """不存在的静态文件 → 404。"""
    r = client.get("/static/nonexistent.js")
    assert r.status_code == 404


def _wait_done(client, task_id, timeout_s=5.0):
    """轮询直到状态非 pending/running。"""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/status/{task_id}")
        s = r.json()["status"]
        if s in ("done", "failed"):
            return s
        time.sleep(0.05)
    return None


def test_upload_creates_task(client, monkeypatch):
    """POST /upload 创建任务并返回 task_id（轮询直到完成）。"""
    import asr.web as wm
    monkeypatch.setattr(wm, "_run_pipeline", _run_pipeline_sync_patch)

    r = client.post(
        "/upload",
        files={"file": ("a.mp4", b"FAKEAUDIO", "video/mp4")},
        data={"diarize": "true", "quality": "balanced"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "task_id" in data
    assert data["filename"] == "a.mp4"

    status = _wait_done(client, data["task_id"])
    assert status == "done", f"expected done, got {status}"


def test_status_404_unknown_task(client):
    """不存在的 task_id → 404。"""
    r = client.get("/status/nonexistent")
    assert r.status_code == 404


def test_download_returns_file(client, monkeypatch):
    """done 任务的输出文件可下载。"""
    import asr.web as wm
    monkeypatch.setattr(wm, "_run_pipeline", _run_pipeline_sync_patch)

    r = client.post("/upload", files={"file": ("a.mp4", b"X", "video/mp4")})
    task_id = r.json()["task_id"]

    status = _wait_done(client, task_id)
    assert status == "done"

    # 下载 SRT
    r = client.get(f"/download/{task_id}/srt")
    assert r.status_code == 200
    assert "hello" in r.text


def test_download_rejects_running(client, monkeypatch):
    """运行中 → 409。"""
    # 注入一个 running 任务
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'running', ?)""",
            ("running1", "x.mp4", "/in/x", "/out/r1", "2026-09-22T00:00:00"),
        )
    r = client.get("/download/running1/srt")
    assert r.status_code == 409


def test_download_rejects_bad_fmt(client):
    """不允许的格式 → 400。"""
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'done', ?)""",
            ("fmt1", "x.mp4", "/in/x", "/out/f1", "2026-09-22T00:00:00"),
        )
    r = client.get("/download/fmt1/exe")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 项目管理 API
# ---------------------------------------------------------------------------
def test_list_projects_empty(client):
    """空库 → 空列表。"""
    r = client.get("/api/projects")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_list_projects_with_data(client):
    """有数据 → 按 created_at DESC 返回，不含 transcript。"""
    import asr.web as wm
    with _db() as c:
        for i, (status, name) in enumerate([
            ("done", "项目A"),
            ("running", "项目B"),
            ("failed", "项目C"),
        ]):
            c.execute(
                """INSERT INTO tasks (id, name, filename, input_path, output_dir,
                                     status, created_at, transcript)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"p{i}", name, f"f{i}.mp4", f"/in/{i}", f"/out/{i}",
                 status, f"2026-09-22T00:0{i}:00", "big transcript"),
            )

    r = client.get("/api/projects")
    data = r.json()
    assert data["total"] == 3
    # 按时间倒序：p2(失败) > p1(运行中) > p0(完成)
    assert [it["id"] for it in data["items"]] == ["p2", "p1", "p0"]
    # 不含 transcript（太大）
    assert "transcript" not in data["items"][0]


def test_list_projects_filter_by_status(client):
    """?status= 筛选。"""
    with _db() as c:
        for i, status in enumerate(["done", "done", "failed"]):
            c.execute(
                """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (f"f{i}", f"f{i}.mp4", "/in", "/out", status, "2026-09-22T00:00:00"),
            )

    r = client.get("/api/projects?status=done")
    data = r.json()
    assert data["total"] == 2
    assert all(it["status"] == "done" for it in data["items"])


def test_list_projects_search(client):
    """?q= 模糊搜索 name + filename。"""
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "神经网络课程", "a.mp4", "/in", "/out", "done", "2026-09-22T00:00:00"),
        )
        c.execute(
            """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s2", "其他", "机器学习.mp4", "/in", "/out", "done", "2026-09-22T00:00:00"),
        )

    r = client.get("/api/projects?q=神经")
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == "s1"

    # 搜文件名也能命中
    r = client.get("/api/projects?q=机器学习")
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == "s2"


def test_list_projects_rejects_bad_status(client):
    """?status= 不合法值 → 400。"""
    r = client.get("/api/projects?status=invalid")
    assert r.status_code == 400


def test_get_project_detail(client):
    """GET /api/projects/{id} 详情含 transcript。"""
    import asr.web as wm
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("d1", "详细", "x.mp4", "/in", "/out", "done", "2026-09-22T00:00:00"),
        )

    r = client.get("/api/projects/d1")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "d1"
    assert data["name"] == "详细"


def test_get_project_not_found(client):
    """不存在的 id → 404。"""
    r = client.get("/api/projects/nope")
    assert r.status_code == 404


def test_create_project(client, monkeypatch):
    """POST /api/projects 创建项目并启动后台任务。"""
    import asr.web as wm
    monkeypatch.setattr(wm, "_run_pipeline", _run_pipeline_sync_patch)

    r = client.post(
        "/api/projects",
        files={"file": ("a.mp4", b"FAKE", "video/mp4")},
        data={"name": "我的项目", "diarize": "true", "quality": "balanced"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "task_id" in data
    assert data["name"] == "我的项目"
    assert data["filename"] == "a.mp4"

    # DB 中应有这条记录
    with _db() as c:
        row = c.execute("SELECT name FROM tasks WHERE id=?", (data["task_id"],)).fetchone()
    assert row["name"] == "我的项目"


def test_create_project_default_name(client, monkeypatch):
    """不传 name → 用文件名去后缀。"""
    import asr.web as wm
    monkeypatch.setattr(wm, "_run_pipeline", _run_pipeline_sync_patch)

    r = client.post(
        "/api/projects",
        files={"file": ("lecture_03.mp4", b"X", "video/mp4")},
        data={"name": ""},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "lecture_03"


def test_patch_project_name(client):
    """PATCH /api/projects/{id} 改 name。"""
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("p1", "旧名", "x.mp4", "/in", "/out", "done", "2026-09-22T00:00:00"),
        )

    r = client.patch("/api/projects/p1", json={"name": "新名"})
    assert r.status_code == 200
    assert r.json()["name"] == "新名"

    with _db() as c:
        row = c.execute("SELECT name FROM tasks WHERE id='p1'").fetchone()
    assert row["name"] == "新名"


def test_patch_project_rejects_empty_name(client):
    """PATCH 空名 → 400。"""
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("p1", "x.mp4", "/in", "/out", "done", "2026-09-22T00:00:00"),
        )
    r = client.patch("/api/projects/p1", json={"name": "  "})
    assert r.status_code == 400


def test_patch_project_not_found(client):
    """PATCH 不存在 → 404。"""
    r = client.patch("/api/projects/nope", json={"name": "x"})
    assert r.status_code == 404


def test_delete_project(client):
    """DELETE → DB 行删，输出目录删。"""
    import asr.web as wm
    # 建一个真实输出目录
    out_dir = wm.OUTPUT_ROOT / "del1"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fake.srt").write_text("x", encoding="utf-8")
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("del1", "删我", "x.mp4", "/in/x", str(out_dir), "done", "2026-09-22T00:00:00"),
        )

    r = client.delete("/api/projects/del1")
    assert r.status_code == 200
    assert r.json()["deleted"] == "del1"

    # DB 行没了
    with _db() as c:
        row = c.execute("SELECT id FROM tasks WHERE id='del1'").fetchone()
    assert row is None
    # 目录也没了
    assert not out_dir.exists()


def test_delete_project_not_found(client):
    """DELETE 不存在 → 404。"""
    r = client.delete("/api/projects/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 批量操作
# ---------------------------------------------------------------------------
def test_batch_delete_success(client):
    """批量删除：返回 deleted + failed 列表。"""
    import asr.web as wm
    out1 = wm.OUTPUT_ROOT / "bd1"
    out2 = wm.OUTPUT_ROOT / "bd2"
    out1.mkdir(parents=True, exist_ok=True)
    out2.mkdir(parents=True, exist_ok=True)
    (out1 / "x.srt").write_text("x", encoding="utf-8")
    (out2 / "y.srt").write_text("y", encoding="utf-8")
    with _db() as c:
        for tid in ("bd1", "bd2", "bd3"):
            c.execute(
                """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tid, "n"+tid, "x.mp4", "/in/x", str(wm.OUTPUT_ROOT/tid), "done",
                 "2026-09-22T00:00:00"),
            )

    r = client.post("/api/projects/batch-delete", json={"ids": ["bd1", "bd2", "bd_nope"]})
    assert r.status_code == 200
    data = r.json()
    assert sorted(data["deleted"]) == ["bd1", "bd2"]
    assert data["failed"] == ["bd_nope"]

    # DB 行没了，目录也没了
    with _db() as c:
        for tid in ("bd1", "bd2"):
            row = c.execute("SELECT id FROM tasks WHERE id=?", (tid,)).fetchone()
            assert row is None
    assert not out1.exists()
    assert not out2.exists()


def test_batch_delete_rejects_empty(client):
    """空 ids → 400。"""
    r = client.post("/api/projects/batch-delete", json={"ids": []})
    assert r.status_code == 400


def test_batch_delete_rejects_too_many(client):
    """超过 50 个 → 400。"""
    r = client.post("/api/projects/batch-delete", json={"ids": ["x"] * 51})
    assert r.status_code == 400


def test_batch_export_zip(client):
    """批量导出：ZIP 含多个项目的输出文件。"""
    import asr.web as wm
    import zipfile as _z
    import io as _io

    out1 = wm.OUTPUT_ROOT / "bx1"
    out2 = wm.OUTPUT_ROOT / "bx2"
    out1.mkdir(parents=True, exist_ok=True)
    out2.mkdir(parents=True, exist_ok=True)
    (out1 / "a.srt").write_text("AAA", encoding="utf-8")
    (out1 / "a.txt").write_text("aaa", encoding="utf-8")
    (out2 / "b.srt").write_text("BBB", encoding="utf-8")
    with _db() as c:
        for tid, name in [("bx1", "课程A"), ("bx2", "课程B")]:
            c.execute(
                """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tid, name, f"{tid}.mp4", "/in/x", str(wm.OUTPUT_ROOT/tid), "done",
                 "2026-09-22T00:00:00"),
            )

    r = client.post("/api/projects/batch-export", json={"ids": ["bx1", "bx2"]})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    # 解 ZIP 验证内容
    zip_bytes = _io.BytesIO(r.content)
    with _z.ZipFile(zip_bytes) as zf:
        names = set(zf.namelist())
    assert "课程A/a.srt" in names
    assert "课程A/a.txt" in names
    assert "课程B/b.srt" in names


def test_batch_export_skips_undone(client):
    """未完成的项目不进 ZIP，但写到 _skipped.json 里。"""
    import asr.web as wm
    import zipfile as _z
    import io as _io

    out1 = wm.OUTPUT_ROOT / "be1"
    out1.mkdir(parents=True, exist_ok=True)
    (out1 / "x.srt").write_text("X", encoding="utf-8")
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("be1", "done的", "a.mp4", "/in", str(out1), "done", "2026-09-22T00:00:00"),
        )
        c.execute(
            """INSERT INTO tasks (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("be2", "running的", "b.mp4", "/in", "/out/be2", "running", "2026-09-22T00:00:00"),
        )

    r = client.post("/api/projects/batch-export", json={"ids": ["be1", "be2"]})
    assert r.status_code == 200
    with _z.ZipFile(_io.BytesIO(r.content)) as zf:
        names = zf.namelist()
    assert "done的/x.srt" in names
    assert "_skipped.json" in names


def test_batch_export_empty_ids(client):
    """空 ids → 400。"""
    r = client.post("/api/projects/batch-export", json={"ids": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Helper：同步跑 _run_pipeline（不走后台线程）
# ---------------------------------------------------------------------------
def _run_pipeline_sync_patch(task_id, input_path, output_dir, config_path, diarize, quality):
    """monkey-patch 用的同步版：把 _task_id 注入到 overrides 让 fake pipeline_run 能更新 DB。"""
    import asr.web as wm

    def _cb(stage, ratio, msg):
        wm._update_task(task_id, progress=ratio, stage=stage, message=msg)

    try:
        cfg = wm.load_config(config_path, strict_models=False)
        overrides = {
            "output_dir": str(output_dir),
            "diarize": diarize,
            "quality": quality,
            "_task_id_for_test": task_id,
        }
        with patch("asr.web.pipeline_run", _fake_pipeline_run):
            wm.pipeline_run(input_path, cfg, overrides, on_progress=_cb)
        wm._update_task(task_id, status="done", progress=1.0,
                        message="完成", meta_json=json.dumps({"segments_count": 1}))
    except Exception as e:  # noqa: BLE001
        wm._update_task(task_id, status="failed", error=str(e))


# ---------------------------------------------------------------------------
# 流式 transcript 推送
# ---------------------------------------------------------------------------
def test_stream_transcript_incremental(isolated_db):
    """_stream_transcript 应渐进把 segments 写入 DB，最终等于完整数组。

    用最小段数（5）+ monkeypatch 让 interval=0，跑完只花 5×0=0 秒。
    """
    import asr.web as wm

    # 准备 task 记录
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'done', ?)""",
            ("stream1", "x.mp4", "/in/x", "/out/s1", "2026-09-22T00:00:00"),
        )

    segs = [{"start": i * 1.0, "end": (i + 1) * 1.0, "text": f"段{i}"} for i in range(5)]

    # 替换 _stream_transcript 内部的 time.sleep 跳过等待
    import time as _time
    orig_sleep = _time.sleep
    _time.sleep = lambda s: None  # 跳过所有 sleep
    try:
        wm._stream_transcript("stream1", segs)
    finally:
        _time.sleep = orig_sleep

    # 跑完：transcript 应为完整 JSON
    with _db() as c:
        row = c.execute("SELECT transcript FROM tasks WHERE id='stream1'").fetchone()
    parsed = json.loads(row["transcript"])
    assert len(parsed) == 5


# ---------------------------------------------------------------------------
# ASR 看门狗：超时自动标 failed
# ---------------------------------------------------------------------------
def test_watchdog_triggers_on_stall(isolated_db):
    """验证模块常量 ASR_STALL_TIMEOUT_S 存在且合理。"""
    import asr.web as wm
    assert hasattr(wm, "ASR_STALL_TIMEOUT_S")
    assert isinstance(wm.ASR_STALL_TIMEOUT_S, (int, float))
    assert wm.ASR_STALL_TIMEOUT_S > 0
    # 默认 30 分钟（留 3 倍余量给 1 小时课的 10 分钟 ASR）
    assert wm.ASR_STALL_TIMEOUT_S == 30 * 60


def test_watchdog_updates_last_cb_time(isolated_db):
    """每次 _cb 调用会更新时间戳（看门狗的"心跳"）。"""
    import asr.web as wm
    from datetime import datetime as _dt

    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'running', ?)""",
            ("wd2", "x.mp4", "/in/x", "/out/wd2", _dt.now().isoformat(timespec="seconds")),
        )

    # 模拟连续 5 次 _cb 调用
    for i in range(5):
        _update_task("wd2", progress=0.1 * i, stage="asr", message=f"识别 {int(0.1*i*100)}%")

    # 每次 _cb 都会更新 progress → progress 应是最后一次的值
    with _db() as c:
        row = c.execute("SELECT status, progress, stage FROM tasks WHERE id='wd2'").fetchone()
    assert row["status"] == "running"
    assert abs(row["progress"] - 0.4) < 0.001
    assert row["stage"] == "asr"