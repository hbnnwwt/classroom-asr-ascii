"""Web UI 单测（FastAPI TestClient + SQLite）。"""
import json
from contextlib import closing
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
    """GET / 返回管理界面 index.html，带 no-cache（HTML 也不能被启发式缓存钉死）。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "项目管理" in r.text
    assert "list.js" in r.text
    assert "no-cache" in r.headers.get("Cache-Control", "")


def test_task_page_serves_html(client):
    """GET /task/{id} 返回详情页 task.html，带 no-cache。"""
    r = client.get("/task/abc123")
    assert r.status_code == 200
    assert "项目详情" in r.text
    assert "task.js" in r.text
    assert "no-cache" in r.headers.get("Cache-Control", "")


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
    r = client.get("/static/theme.js")
    assert r.status_code == 200
    assert "theme-toggle" in r.text
    assert "no-cache" in r.headers.get("Cache-Control", "")


def test_static_file_not_found(client):
    """不存在的静态文件 → 404。"""
    r = client.get("/static/nonexistent.js")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# AI 反馈产物下载：feedback/polished fmt + md 防抢占
# ---------------------------------------------------------------------------
def _mk_done_task_with_ai_files(isolated_db, task_id="dl1"):
    """造一个 done 任务，outputs 里同时有转写 md 和两个 AI 产物。"""
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{task_id}_测试课.md").write_text("转写正文", encoding="utf-8")
    (out_dir / f"{task_id}_测试课.feedback.md").write_text("# AI 课堂反馈", encoding="utf-8")
    (out_dir / f"{task_id}_测试课.polished.md").write_text("# 优化稿", encoding="utf-8")
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, progress, created_at, meta_json)"
            " VALUES (?, '测试课', 'l.mp4', 'x', ?, 'done', 1.0, '2026-10-21', ?)",
            (task_id, str(out_dir), json.dumps(
                {"ai_files": {"feedback": f"{task_id}_测试课.feedback.md",
                              "polished": f"{task_id}_测试课.polished.md"}},
                ensure_ascii=False)),
        )
    return out_dir


def test_download_md_not_hijacked_by_ai_files(client, isolated_db):
    """md fmt 必须返回转写 md，不能被 .feedback.md/.polished.md 抢占。"""
    _mk_done_task_with_ai_files(isolated_db)
    r = client.get("/download/dl1/md")
    assert r.status_code == 200
    assert r.text == "转写正文"


def test_download_feedback_and_polished_fmt(client, isolated_db):
    _mk_done_task_with_ai_files(isolated_db)
    r = client.get("/download/dl1/feedback")
    assert r.status_code == 200
    assert "# AI 课堂反馈" in r.text
    assert "feedback.md" in r.headers.get("content-disposition", "")
    r = client.get("/download/dl1/polished")
    assert r.status_code == 200
    assert "# 优化稿" in r.text


def test_download_feedback_missing_404(client, isolated_db):
    """任务没有 AI 产物 → feedback fmt 404（不 500）。"""
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / "dl2"
    out_dir.mkdir(parents=True, exist_ok=True)
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, progress, created_at)"
            " VALUES ('dl2', 'n', 'a.mp4', 'x', ?, 'done', 1.0, '2026-10-21')",
            (str(out_dir),),
        )
    assert client.get("/download/dl2/feedback").status_code == 404


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
    _install_sync_pipeline(monkeypatch)

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
    _install_sync_pipeline(monkeypatch)

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
    """POST /api/projects 单文件创建项目并启动后台任务（老行为）。"""
    _install_sync_pipeline(monkeypatch)
    _install_fake_probe(monkeypatch, [_mk_info("a.mp4")])

    r = client.post(
        "/api/projects",
        files=[("files", ("a.mp4", b"FAKE", "video/mp4"))],
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
    _install_sync_pipeline(monkeypatch)
    _install_fake_probe(monkeypatch, [_mk_info("lecture_03.mp4")])

    r = client.post(
        "/api/projects",
        files=[("files", ("lecture_03.mp4", b"X", "video/mp4"))],
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
class _InlineThread:
    """threading.Thread 替身：start() 在当前线程直接执行 target。

    端点用 threading.Thread 起 _run_pipeline 后台线程；测试若让线程真跑，
    它可能活过测试边界，把 UPDATE 写到下一个测试刚切换的 tmp DB
    （顺序型偶发 sqlite3.OperationalError: no such table）。内联执行后
    请求返回即全部写完，线程不可能泄漏。
    """

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _install_sync_pipeline(monkeypatch):
    """端点测试统一注入：_run_pipeline 换同步假实现 + asr.web 的 Thread 内联。

    只替换 asr.web 命名空间内的 threading（不动全局 threading 模块，
    避免影响 TestClient 的 anyio 工作线程）。
    """
    import types
    import asr.web as wm
    monkeypatch.setattr(wm, "_run_pipeline", _run_pipeline_sync_patch)
    monkeypatch.setattr(wm, "threading", types.SimpleNamespace(Thread=_InlineThread))


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
# transcript 与 done 原子写入（回归：页面冻在第一段）
# ---------------------------------------------------------------------------
def test_run_pipeline_writes_transcript_atomically_with_done(isolated_db, monkeypatch):
    """回归（真机：页面只显示第 1 段"(0/1)"，SRT 却完整）：
    旧版先标 done、再开 _stream_transcript 线程逐段"伪直播"（1004 段要推
    ~150s），而前端见 done 即停轮询——_run_pipeline 返回时 DB 里只剩
    推流线程已写到的部分前缀。现在 transcript 与 done 同一笔 UPDATE，
    _run_pipeline 同步返回时 DB 必须已是终态全量。
    """
    import asr.web as wm

    segs = [{"start": i * 1.0, "end": (i + 1) * 1.0, "text": f"第{i}段。"} for i in range(3)]
    out_dir = wm.OUTPUT_ROOT / "atom1"
    out_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run(input_path, cfg, overrides, on_progress=None, on_segments=None):
        """写真实形状的 .json（pipeline 实际产物），返回最小 Result。"""
        j = out_dir / "fake.json"
        j.write_text(json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
        (out_dir / "fake.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
        from asr.pipeline import Result
        return Result(
            input_path=Path("/tmp/x"), output_dir=out_dir, duration_s=3.0,
            elapsed_s=0.1, rtf=0.03,
            written={"srt": out_dir / "fake.srt", "json": j},
            replace_log=[], segments_count=len(segs),
        )

    monkeypatch.setattr(wm, "pipeline_run", _fake_run)

    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            ("atom1", "x.mp4", "/in/x", str(out_dir), "2026-09-22T00:00:00"),
        )

    cfg_path = str(Path(__file__).parent.parent / "config.yaml")
    wm._run_pipeline("atom1", Path("/in/x"), out_dir, cfg_path, diarize=False, quality="balanced")

    # 同步返回即查：done 与全量 transcript 必须同时可见（无后补窗口）
    with _db() as c:
        row = c.execute("SELECT status, transcript FROM tasks WHERE id='atom1'").fetchone()
    assert row["status"] == "done"
    assert json.loads(row["transcript"]) == segs


def test_run_pipeline_streams_transcript_incrementally(isolated_db, monkeypatch):
    """渐进交付：引擎 on_segments 每交付一组 → DB transcript 立即增长
    （任务 running 时前端轮询就能拿到已识别文字）；done 时最终 .json
    覆盖（后处理 ITN 改写前缀文本），DB 收敛到最终版。"""
    import asr.web as wm

    out_dir = wm.OUTPUT_ROOT / "inc1"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshots: list[str] = []          # 每次交付后的 DB transcript 快照

    def _fake_run(input_path, cfg, overrides, on_progress=None, on_segments=None):
        # 模拟引擎逐组交付两组，随后 pipeline 写最终 json（ITN 改写段0 文本）
        from asr.engines.base import Segment
        if on_segments:
            on_segments([Segment(start=0.1, end=0.9, text="二零二五年。")])
            with _db() as c:
                snapshots.append(c.execute(
                    "SELECT transcript, status FROM tasks WHERE id='inc1'").fetchone()["transcript"])
            on_segments([Segment(start=2.8, end=3.6, text="第二组。")])
            with _db() as c:
                snapshots.append(c.execute(
                    "SELECT transcript, status FROM tasks WHERE id='inc1'").fetchone()["transcript"])
        j = out_dir / "fake.json"
        j.write_text(json.dumps({"segments": [
            {"start": 0.1, "end": 0.9, "text": "2025年。"},   # ITN 改写
            {"start": 2.8, "end": 3.6, "text": "第二组。"},
        ]}, ensure_ascii=False), encoding="utf-8")
        (out_dir / "fake.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
        from asr.pipeline import Result
        return Result(
            input_path=Path("/tmp/x"), output_dir=out_dir, duration_s=3.6,
            elapsed_s=0.1, rtf=0.03,
            written={"srt": out_dir / "fake.srt", "json": j},
            replace_log=[], segments_count=2,
        )

    monkeypatch.setattr(wm, "pipeline_run", _fake_run)

    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            ("inc1", "x.mp4", "/in/x", str(out_dir), "2026-09-22T00:00:00"),
        )

    cfg_path = str(Path(__file__).parent.parent / "config.yaml")
    wm._run_pipeline("inc1", Path("/in/x"), out_dir, cfg_path, diarize=False, quality="balanced")

    # 识别过程中 DB 已有增量文字（前端轮询可见）
    assert len(json.loads(snapshots[0])) == 1
    assert json.loads(snapshots[0])[0]["text"] == "二零二五年。"
    assert len(json.loads(snapshots[1])) == 2
    # done 后：最终版覆盖（段0 被 ITN 改写）
    with _db() as c:
        row = c.execute("SELECT status, transcript FROM tasks WHERE id='inc1'").fetchone()
    assert row["status"] == "done"
    final = json.loads(row["transcript"])
    assert [s["text"] for s in final] == ["2025年。", "第二组。"]


def test_read_transcript_missing_file_returns_empty():
    """_read_transcript：json 路径缺失/不存在 → 空串（不抛）。"""
    from asr.web import _read_transcript
    assert _read_transcript(None) == ""
    assert _read_transcript(Path("Z:/nonexistent/x.json")) == ""


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


# ---------------------------------------------------------------------------
# P0-6：看门狗终态守卫（done 任务不可被改写为 failed）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 多段拼接：POST /api/projects files=[...] 直传 + _run_multi_task 编排
# ---------------------------------------------------------------------------
from asr.media import MediaInfo


def _mk_info(path):
    """创建路径校验用的 MediaInfo（只判有无音轨，与 ffprobe 输出同形）。"""
    p = Path(path)
    return MediaInfo(
        path=p, duration_s=60.0,
        video_streams=[{"codec_type": "video", "codec_name": "h264"}],
        audio_streams=[{"codec_type": "audio", "codec_name": "aac"}],
    )


def _install_fake_probe(monkeypatch, infos):
    """按文件名把 wm.probe 换成假实现（自动剥掉落盘加的 task/序号前缀）。"""
    import re as _re
    import asr.web as wm
    table = {Path(i.path).name: i for i in infos}

    def _lookup(p):
        name = Path(p).name
        if name in table:
            return table[name]
        return table[_re.sub(r"^([0-9a-f]{12}|\d{2})_", "", name)]

    monkeypatch.setattr(wm, "probe", _lookup)


def test_create_project_multi_files(client, monkeypatch):
    """2 个文件：存入 {task_id}_parts/{序号}_，meta 记 parts，起 _run_multi_task。"""
    import asr.web as wm
    _install_fake_probe(monkeypatch, [_mk_info("a.mp4"), _mk_info("b.mp4")])

    recorded = {}

    def _fake_multi(task_id, part_paths, out_dir, config_path, diarize, quality):
        recorded["paths"] = list(part_paths)
        _update_task(task_id, status="done", progress=1.0)

    monkeypatch.setattr(wm, "_run_multi_task", _fake_multi)
    import types
    monkeypatch.setattr(wm, "threading", types.SimpleNamespace(Thread=_InlineThread))

    r = client.post("/api/projects",
                    files=[("files", ("a.mp4", b"X", "video/mp4")),
                           ("files", ("b.mp4", b"Y", "video/mp4"))],
                    data={"name": "第三次课", "diarize": "false", "quality": "balanced"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    assert r.json()["name"] == "第三次课"
    assert r.json()["filename"] == "a.mp4 + b.mp4"

    # 段文件带序号前缀保序落盘
    assert [p.name for p in recorded["paths"]] == ["00_a.mp4", "01_b.mp4"]
    # DB：meta 记录分段
    with _db() as c:
        row = c.execute("SELECT meta_json FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert json.loads(row["meta_json"])["parts"] == ["a.mp4", "b.mp4"]


def test_create_project_single_file_plain(client, monkeypatch):
    """1 个文件：老行为——UPLOAD_DIR/{task_id}_{名}，无 meta，直接 _run_pipeline。"""
    import asr.web as wm
    _install_fake_probe(monkeypatch, [_mk_info("a.mp4")])

    recorded = {}

    def _fake_pipeline(task_id, input_path, output_dir, config_path, diarize, quality):
        recorded["input"] = str(input_path)
        _update_task(task_id, status="done", progress=1.0)

    monkeypatch.setattr(wm, "_run_pipeline", _fake_pipeline)
    import types
    monkeypatch.setattr(wm, "threading", types.SimpleNamespace(Thread=_InlineThread))

    r = client.post("/api/projects", files=[("files", ("a.mp4", b"X", "video/mp4"))])
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    assert recorded["input"] == str(wm.UPLOAD_DIR / f"{task_id}_a.mp4")
    assert not (wm.UPLOAD_DIR / f"{task_id}_parts").exists(), "单文件不建 parts 目录"

    with _db() as c:
        row = c.execute("SELECT meta_json FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert not json.loads(row["meta_json"] or "{}").get("parts")


def test_create_project_rejects_no_audio(client, monkeypatch):
    """无音轨的文件 → 400 带文件名，且不留半截文件。"""
    import asr.web as wm
    silent = MediaInfo(path=Path("s.mp4"), duration_s=10.0,
                       video_streams=[{"codec_name": "h264"}], audio_streams=[])
    _install_fake_probe(monkeypatch, [_mk_info("a.mp4"), silent])

    r = client.post("/api/projects",
                    files=[("files", ("a.mp4", b"X", "video/mp4")),
                           ("files", ("s.mp4", b"Y", "video/mp4"))])
    assert r.status_code == 400
    assert "s.mp4" in r.json()["detail"]
    # 失败不留垃圾：无 parts 目录残留
    assert not any(p.name.endswith("_parts") for p in wm.UPLOAD_DIR.iterdir())


def test_create_project_rejects_too_many(client):
    """超过 MAX_MERGE_PARTS → 400。"""
    files = [("files", (f"p{i:02d}.mp4", b"X", "video/mp4")) for i in range(21)]
    r = client.post("/api/projects", files=files)
    assert r.status_code == 400


def test_create_project_requires_files(client):
    """不带文件 → 422（FastAPI 必填校验）。"""
    r = client.post("/api/projects", data={"name": "x"})
    assert r.status_code == 422


def test_download_legacy_merged_mp4(client):
    """旧版合并任务（meta.merge_mode）的 merged.mp4 仍可下载（存量不破坏）。"""
    out_dir = OUTPUT_ROOT / "legacy1"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "merged.mp4").write_bytes(b"MERGED")
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir,
               status, progress, created_at, meta_json)
               VALUES (?, ?, ?, ?, 'done', 1.0, ?, ?)""",
            ("legacy1", "a.mp4 + b.mp4", str(out_dir / "merged.mp4"), str(out_dir),
             "2026-10-19T00:00:00", json.dumps({"merge_mode": "merge_only"})),
        )

    r = client.get("/download/legacy1/mp4")
    assert r.status_code == 200
    assert r.content == b"MERGED"


# ---------------------------------------------------------------------------
# _run_multi_task：拼音频编排（merge_audios + pipeline 全 mock）
# ---------------------------------------------------------------------------
def _mk_multi_env(monkeypatch):
    """插好桩的 _run_multi_task 测试环境：DB 行 + mock merge/pipeline。"""
    import types
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / "mt1"
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = [wm.UPLOAD_DIR / "p1.mp4", wm.UPLOAD_DIR / "p2.mp4"]

    merge_calls = {}

    def _fake_merge(part_paths, out, cfg, on_progress=None):
        merge_calls["n"] = len(part_paths)
        merge_calls["cfg"] = cfg
        if on_progress:
            on_progress(0.5, "提取音轨 1/2")
        out = Path(out)
        out.write_bytes(b"WAVDATA")
        return out, [1.0, 1.0]

    monkeypatch.setattr(wm, "merge_audios", _fake_merge)

    from asr.config import OutputConfig
    fake_cfg = types.SimpleNamespace(output=OutputConfig())
    monkeypatch.setattr(wm, "load_config", lambda *a, **kw: fake_cfg)

    pipeline_calls = {}

    def _fake_pipeline(task_id, input_path, output_dir, config_path, diarize, quality):
        pipeline_calls["input"] = str(input_path)
        # 模拟真实 pipeline 的 done：meta 覆盖 + transcript 原子写入
        _update_task(task_id, status="done", progress=1.0,
                     meta_json=json.dumps({"segments_count": 3}),
                     transcript=json.dumps([
                         {"start": 0.1, "end": 0.4, "text": "第一段话"},
                         {"start": 1.2, "end": 1.5, "text": "第二段话"},
                     ], ensure_ascii=False))

    monkeypatch.setattr(wm, "_run_pipeline", _fake_pipeline)

    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at, meta_json)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            ("mt1", "p1.mp4 + p2.mp4", str(out_dir / "merged.wav"), str(out_dir),
             "2026-10-20T00:00:00",
             json.dumps({"parts": ["p1.mp4", "p2.mp4"]})),
        )
    return wm, out_dir, parts, merge_calls, pipeline_calls


def test_run_multi_task_merges_then_pipeline(isolated_db, monkeypatch):
    """先拼音频（stage=merge）再 pipeline 吃 merged.wav；meta 保留 parts。"""
    wm, out_dir, parts, merge_calls, pipeline_calls = _mk_multi_env(monkeypatch)

    wm._run_multi_task("mt1", parts, out_dir, "config.yaml", False, "balanced")

    assert merge_calls["n"] == 2
    assert hasattr(merge_calls["cfg"], "output"), "配置透传给 merge_audios"
    assert pipeline_calls["input"] == str(out_dir / "merged.wav"), "pipeline 吃拼接产物"

    with _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id='mt1'").fetchone()
    d = _row_to_dict(row)
    assert d["status"] == "done"
    assert d["meta"]["parts"] == ["p1.mp4", "p2.mp4"], "done 后 parts 元数据不丢"
    assert d["meta"]["segments_count"] == 3, "pipeline 的 meta 与分段 meta 合并"

    # 每段一份独立 SRT：文件落盘 + meta 记录，内容按段边界切分且时间归零
    assert d["meta"]["part_files"] == ["00_p1.srt", "01_p2.srt"]
    a = (out_dir / "00_p1.srt").read_text(encoding="utf-8")
    b = (out_dir / "01_p2.srt").read_text(encoding="utf-8")
    assert "第一段话" in a and "第二段话" not in a
    assert "第二段话" in b and "00:00:00,200 --> 00:00:00,500" in b, "时间归零"


def test_download_part_srt(client, monkeypatch):
    """/download/{id}/part_srt/{i}：下载某段独立 SRT；越界 404。

    同时钉住主 SRT 下载不被分段文件（00_/01_ 前缀）劫持。
    """
    wm, out_dir, parts, _, _ = _mk_multi_env(monkeypatch)
    wm._run_multi_task("mt1", parts, out_dir, "config.yaml", False, "balanced")

    r = client.get("/download/mt1/part_srt/1")
    assert r.status_code == 200
    assert "第二段话" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")

    assert client.get("/download/mt1/part_srt/5").status_code == 404
    assert client.get("/download/mt1/part_srt/0").status_code == 200

    # 主 SRT：输出目录里 00_p1.srt 排序在前，但整体字幕必须是 merged.srt
    (out_dir / "merged.srt").write_text("整体字幕", encoding="utf-8")
    r = client.get("/download/mt1/srt")
    assert r.status_code == 200
    assert r.text == "整体字幕", "分段文件不得劫持主 SRT 下载"


def test_download_part_srt_rejects_undone(client):
    """任务未完成 → 409（与主下载路由口径一致）。"""
    out_dir = OUTPUT_ROOT / "undone1"
    out_dir.mkdir(parents=True, exist_ok=True)
    with _db() as c:
        c.execute(
            """INSERT INTO tasks (id, filename, input_path, output_dir,
               status, created_at, meta_json)
               VALUES (?, ?, ?, ?, 'running', ?, ?)""",
            ("undone1", "a.mp4 + b.mp4", "/in", str(out_dir),
             "2026-10-20T00:00:00",
             json.dumps({"parts": ["a.mp4", "b.mp4"],
                         "part_files": ["00_a.srt", "01_b.srt"]})),
        )
    r = client.get("/download/undone1/part_srt/0")
    assert r.status_code == 409


def test_run_multi_task_failure_marks_failed(isolated_db, monkeypatch):
    """拼音频失败 → 任务 failed + 错误信息（不得卡 running）。"""
    wm, out_dir, parts, _, _ = _mk_multi_env(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("视频无音轨")

    monkeypatch.setattr(wm, "merge_audios", _boom)

    wm._run_multi_task("mt1", parts, out_dir, "config.yaml", False, "balanced")

    with _db() as c:
        row = c.execute("SELECT status, error FROM tasks WHERE id='mt1'").fetchone()
    assert row["status"] == "failed"
    assert "无音轨" in row["error"]


def test_run_multi_task_pipeline_failed_skips_part_srts(isolated_db, monkeypatch):
    """pipeline 内部标 failed（吞异常不抛）→ 不写空分段字幕，meta 不带 part_files。

    真实事故：ASR 阶段 OSError 后 pipeline 兜底标 failed 并正常返回，
    _run_multi_task 无从得知，继续用空 transcript 写出三个空 SRT。
    """
    wm, out_dir, parts, _, _ = _mk_multi_env(monkeypatch)

    def _pipeline_fails_silently(task_id, *a, **kw):
        _update_task(task_id, status="failed", stage="asr", progress=0.214,
                     error="Unexpected: OSError: [Errno 22] Invalid argument")

    monkeypatch.setattr(wm, "_run_pipeline", _pipeline_fails_silently)

    wm._run_multi_task("mt1", parts, out_dir, "config.yaml", False, "balanced")

    with _db() as c:
        row = c.execute("SELECT status, meta_json FROM tasks WHERE id='mt1'").fetchone()
    assert row["status"] == "failed", "失败状态不得被后续 meta 更新掩盖"
    meta = json.loads(row["meta_json"] or "{}")
    assert not meta.get("part_files"), "失败任务不应登记分段字幕"
    written = [p.name for p in out_dir.glob("*.srt")]
    assert not any(re.match(r"^\d{2}_", n) for n in written), \
        f"失败后不应写出分段 SRT: {written}"


class TestWatchdog:
    def test_done_task_not_flipped_by_watchdog(self, isolated_db):
        """P0-6：任务已 done，看门狗不得改写为 failed。"""
        from asr.web import _update_task_failed_if_running

        with closing(_db()) as conn:
            conn.execute("DELETE FROM tasks")
            conn.execute(
                "INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)"
                " VALUES ('t1','f','i','o','done','2026-01-01')"
            )
        flipped = _update_task_failed_if_running("t1", "watchdog fired")
        assert flipped is False
        with closing(_db()) as conn:
            assert conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()[0] == "done"

    def test_running_task_flipped_by_watchdog(self, isolated_db):
        """P0-6：任务 running，看门狗守卫可原子标 failed（SQL 级 WHERE status='running'）。"""
        from asr.web import _update_task_failed_if_running

        with closing(_db()) as conn:
            conn.execute("DELETE FROM tasks")
            conn.execute(
                "INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)"
                " VALUES ('t2','f','i','o','running','2026-01-01')"
            )
        flipped = _update_task_failed_if_running("t2", "watchdog fired")
        assert flipped is True
        with closing(_db()) as conn:
            assert conn.execute("SELECT status FROM tasks WHERE id='t2'").fetchone()[0] == "failed"


# ---------------------------------------------------------------------------
# pipeline done 的 meta 读-改-写：done 不许冲掉既有 meta 键
# ---------------------------------------------------------------------------
def test_pipeline_done_preserves_existing_meta(isolated_db, monkeypatch, tmp_path):
    """done 前写进 meta 的键（parts/ai_feedback/merge_mode）在 done 后仍在。"""
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / "pm1"
    out_dir.mkdir(parents=True, exist_ok=True)
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, created_at, meta_json)"
            " VALUES ('pm1', '课', 'a.mp4', 'x', ?, 'pending', '2026-10-21', ?)",
            (str(out_dir), json.dumps(
                {"parts": ["a.mp4", "b.mp4"], "ai_feedback": True,
                 "merge_mode": "concat"}, ensure_ascii=False)),
        )
    jp = tmp_path / "out.json"
    jp.write_text(json.dumps({"segments": [
        {"start": 0.0, "end": 1.0, "text": "嗯", "spk": "教师"}]},
        ensure_ascii=False), encoding="utf-8")

    class FakeResult:
        duration_s = 10.0
        elapsed_s = 1.0
        rtf = 0.1
        segments_count = 1
        written = {"json": str(jp)}

    monkeypatch.setattr(wm, "pipeline_run", lambda *a, **kw: FakeResult())
    wm._run_pipeline("pm1", tmp_path / "in.wav", out_dir, "config.yaml",
                     False, "balanced")

    with closing(wm._db()) as conn:
        row = conn.execute("SELECT status, meta_json FROM tasks WHERE id='pm1'").fetchone()
    assert row["status"] == "done"
    meta = json.loads(row["meta_json"])
    assert meta["parts"] == ["a.mp4", "b.mp4"]      # 现网多段任务依赖
    assert meta["ai_feedback"] is True              # AI 开关依赖（Task 7）
    assert meta["merge_mode"] == "concat"           # 旧版任务依赖
    assert "duration_s" in meta                     # pipeline 自己的键照写