"""
Web UI（FastAPI）：
- POST /upload        上传视频文件，返回 task_id
- GET  /status/{id}   查询任务状态/进度（轮询）
- GET  /download/{id}/{fmt} 下载指定格式输出
- GET  /              单页前端

数据持久化：SQLite（默认 ./web/web_state.db），重启后任务记录不丢，
但任务输出文件本身存于磁盘（不在 SQLite 里），重启后输出仍可下载。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger

from ..config import ConfigLoadError, load_config
from ..pipeline import run as pipeline_run


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
DB_PATH = PACKAGE_DIR / "web_state.db"
UPLOAD_DIR = PACKAGE_DIR / "uploads"
OUTPUT_ROOT = PACKAGE_DIR / "outputs"

# ASR 看门狗：FunASR PUNC 阶段可能死锁（无任何 callback），超过这个时间无回调
# 就主动标 failed，防止任务永远卡在 "running"。30 分钟对 1 小时课足够（ASR
# 阶段本身最多 10 分钟，留 3 倍余量）。
ASR_STALL_TIMEOUT_S = 30 * 60


# ---------------------------------------------------------------------------
# SQLite 持久化
# ---------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db() -> None:
    """建表 + 增量迁移（老库加新列时用 ALTER TABLE，不会重建表/丢数据）。"""
    with closing(_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           TEXT PRIMARY KEY,
                filename     TEXT NOT NULL,
                input_path   TEXT NOT NULL,
                output_dir   TEXT NOT NULL,
                status       TEXT NOT NULL,
                progress     REAL DEFAULT 0,
                stage        TEXT DEFAULT '',
                message      TEXT DEFAULT '',
                error        TEXT DEFAULT '',
                created_at   TEXT NOT NULL,
                started_at   TEXT,
                ended_at     TEXT,
                meta_json    TEXT DEFAULT '',
                transcript   TEXT DEFAULT ''
            )
        """)
        # 迁移：补齐缺失列（PRIMARY KEY 不在迁移范围）
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for col, decl in _MIGRATIONS:
            if col not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")


# 新增列只需往这里加一行（PRIMARY KEY 已含在 CREATE TABLE 中，不要重复）
_MIGRATIONS = [
    ("transcript", "TEXT DEFAULT ''"),
    ("name", "TEXT DEFAULT ''"),
    ("stage_started_at", "TEXT DEFAULT ''"),
]


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"] or "",
        "filename": row["filename"],
        "input_path": row["input_path"],
        "output_dir": row["output_dir"],
        "status": row["status"],
        "progress": row["progress"],
        "stage": row["stage"],
        "message": row["message"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "stage_started_at": row["stage_started_at"] or "",
        "meta": json.loads(row["meta_json"] or "{}"),
        "transcript": row["transcript"] or "",
    }


def _update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    with closing(_db()) as conn:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", vals)


def _update_task_failed_if_running(task_id: str, error: str) -> bool:
    """SQL 级终态守卫：仅 running 可被标 failed（done 等终态不可污染）。"""
    with closing(_db()) as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='failed', error=?, ended_at=? "
            "WHERE id=? AND status='running'",
            (error, datetime.now().isoformat(timespec="seconds"), task_id),
        )
        return cur.rowcount > 0


def _read_transcript(json_path) -> str:
    """从 pipeline 输出的 .json 读 segments → JSON 编码（每段含 start/end/text）。
    失败 → 空串。前端伪实时渲染时拿这个数组逐段 fade-in。
    """
    if not json_path or not Path(json_path).exists():
        return ""
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        segs = [
            {"start": s.get("start", 0.0), "end": s.get("end", 0.0), "text": s.get("text", "")}
            for s in data.get("segments", [])
        ]
        return json.dumps(segs, ensure_ascii=False)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _init_db()
    logger.info(f"[web] 数据库: {DB_PATH}")
    yield


app = FastAPI(title="Classroom ASR", version="1.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    """管理界面：项目列表。"""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/task/{task_id}", response_class=HTMLResponse)
def task_page(task_id: str) -> FileResponse:
    """项目详情页：单项目转写进度 + transcript。"""
    return FileResponse(str(STATIC_DIR / "task.html"))


@app.get("/static/{path:path}")
def static_file(path: str) -> FileResponse:
    """静态文件：no-cache 保证改了前端立即生效。"""
    file_path = STATIC_DIR / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(
        str(file_path),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    config: str = Form("config.yaml"),
    diarize: bool = Form(False),
    quality: str = Form("balanced"),
) -> JSONResponse:
    """接收上传文件，创建后台任务，返回 task_id。"""
    if not file.filename:
        raise HTTPException(400, "文件名为空")

    task_id = uuid.uuid4().hex[:12]
    ts = datetime.now().isoformat(timespec="seconds")
    save_path = UPLOAD_DIR / f"{task_id}_{Path(file.filename).name}"

    # 保存上传文件
    with open(save_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    # 任务输出目录：每个任务一个目录，输出文件存在这里
    out_dir = OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    with closing(_db()) as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (task_id, file.filename, str(save_path), str(out_dir), ts),
        )

    # 后台启动 pipeline
    threading.Thread(
        target=_run_pipeline,
        args=(task_id, save_path, out_dir, config, diarize, quality),
        daemon=True,
    ).start()

    return JSONResponse({"task_id": task_id, "filename": file.filename})


@app.get("/status/{task_id}")
def status(task_id: str) -> JSONResponse:
    """查询任务进度（前端轮询此端点）。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    return JSONResponse(_row_to_dict(row))


@app.get("/download/{task_id}/{fmt}")
def download(task_id: str, fmt: str):
    """下载指定格式输出（SRT/VTT/MD/TXT/JSON/REPLACE_LOG）。"""
    ALLOWED = {"srt", "vtt", "md", "txt", "json", "replace.log"}
    if fmt not in ALLOWED:
        raise HTTPException(400, f"不支持的格式: {fmt}")

    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    if row["status"] != "done":
        raise HTTPException(409, f"任务未完成: {row['status']}")

    out_dir = Path(row["output_dir"])
    files = list(out_dir.glob(f"*.{fmt}"))
    if not files:
        raise HTTPException(404, f"输出文件不存在: {fmt}")
    target = files[0]
    return FileResponse(
        str(target),
        filename=f"{Path(row['filename']).stem}.{fmt}",
    )


# ---------------------------------------------------------------------------
# 项目管理 API（管理界面用）
# 设计：项目 = 一次转写（1 个文件 = 1 个项目），与 tasks 表一对一。
# 列出支持 ?q= 名称/文件名模糊搜索、?status= 状态筛选。
# ---------------------------------------------------------------------------
_ALLOWED_STATUS_FILTER = {"pending", "running", "done", "failed"}


@app.get("/api/projects")
def list_projects(q: str = "", status: str = ""):
    """列出所有项目。支持 ?q= 搜索、?status= 筛选。"""
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list = []
    if q:
        sql += " AND (name LIKE ? OR filename LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
    if status:
        if status not in _ALLOWED_STATUS_FILTER:
            raise HTTPException(400, f"不支持的状态: {status}")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"

    with closing(_db()) as conn:
        rows = conn.execute(sql, params).fetchall()

    items = [_row_to_dict(r) for r in rows]
    # 列表不返 transcript（太大），前端按需 GET 单个详情
    for it in items:
        it.pop("transcript", None)
    return JSONResponse({"items": items, "total": len(items)})


@app.get("/api/projects/{task_id}")
def get_project(task_id: str):
    """单个项目详情（含 transcript）。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "项目不存在")
    return JSONResponse(_row_to_dict(row))


@app.post("/api/projects")
async def create_project(
    file: UploadFile = File(...),
    name: str = Form(""),
    config: str = Form("config.yaml"),
    diarize: bool = Form(False),
    quality: str = Form("balanced"),
):
    """新建项目：上传文件 + 启动转写后台任务。"""
    if not file.filename:
        raise HTTPException(400, "文件名为空")

    task_id = uuid.uuid4().hex[:12]
    ts = datetime.now().isoformat(timespec="seconds")
    save_path = UPLOAD_DIR / f"{task_id}_{Path(file.filename).name}"

    with open(save_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    out_dir = OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # name 默认用文件名去后缀
    final_name = name.strip() or Path(file.filename).stem

    with closing(_db()) as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, name, filename, input_path, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (task_id, final_name, file.filename, str(save_path), str(out_dir), ts),
        )

    threading.Thread(
        target=_run_pipeline,
        args=(task_id, save_path, out_dir, config, diarize, quality),
        daemon=True,
    ).start()

    return JSONResponse({"task_id": task_id, "name": final_name, "filename": file.filename})


@app.patch("/api/projects/{task_id}")
def update_project(task_id: str, payload: dict):
    """改项目名。只允许改 name 字段（避免误改 status/progress）。"""
    name = payload.get("name")
    if not isinstance(name, str):
        raise HTTPException(400, "name 必须是字符串")
    name = name.strip()
    if not name:
        raise HTTPException(400, "name 不能为空")

    with closing(_db()) as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "项目不存在")
        conn.execute("UPDATE tasks SET name=? WHERE id=?", (name, task_id))

    return JSONResponse({"id": task_id, "name": name})


@app.delete("/api/projects/{task_id}")
def delete_project(task_id: str):
    """删项目：DB 行 + 上传文件 + 输出目录全清。"""
    if not _delete_one(task_id):
        raise HTTPException(404, "项目不存在")
    return JSONResponse({"deleted": task_id})


def _delete_one(task_id: str) -> bool:
    """删除单个项目（DB + 磁盘）。返回 True 成功，False 不存在。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    import shutil as _sh
    out_dir = Path(row["output_dir"])
    if out_dir.exists():
        _sh.rmtree(out_dir, ignore_errors=True)
    in_path = Path(row["input_path"])
    if in_path.exists():
        try:
            in_path.unlink()
        except OSError:
            pass
    return True


# ---------------------------------------------------------------------------
# 批量操作
# ---------------------------------------------------------------------------
@app.post("/api/projects/batch-delete")
def batch_delete(payload: dict):
    """批量删除。body: {"ids": ["id1", "id2", ...]}。返回删除数和失败列表。"""
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 必须是非空数组")
    # 限制单次最多 50 个，防止误操作把 DB 全删了
    if len(ids) > 50:
        raise HTTPException(400, "单次最多删除 50 个项目")

    deleted, failed = [], []
    for tid in ids:
        if not isinstance(tid, str):
            continue
        if _delete_one(tid):
            deleted.append(tid)
        else:
            failed.append(tid)
    return JSONResponse({"deleted": deleted, "failed": failed})


@app.post("/api/projects/batch-export")
def batch_export(payload: dict):
    """批量导出：把多个项目的输出文件打包成 ZIP 流式返回。"""
    from fastapi.responses import StreamingResponse
    import zipfile
    import io as _io

    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 必须是非空数组")
    if len(ids) > 20:
        raise HTTPException(400, "单次最多导出 20 个项目（避免内存爆炸）")

    # 预先查 DB，按 name 组织文件路径（用 name 而不是 id 当 zip 内目录名更友好）
    with closing(_db()) as conn:
        rows = conn.execute(
            "SELECT id, name, filename, output_dir, status FROM tasks WHERE id IN ({})".format(
                ",".join("?" * len(ids))
            ),
            ids,
        ).fetchall()

    if not rows:
        raise HTTPException(404, "未找到任何项目")

    def _gen():
        """流式生成 ZIP，避免大文件占内存。"""
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            seen_names: set[str] = set()
            for row in rows:
                if row["status"] != "done":
                    continue
                # 每个项目一个子目录：<项目名>/<原文件名>.srt 等
                folder = row["name"] or row["filename"] or row["id"]
                # 防重名
                folder_unique = folder
                suffix = 2
                while folder_unique in seen_names:
                    folder_unique = f"{folder}_{suffix}"
                    suffix += 1
                seen_names.add(folder_unique)

                out_dir = Path(row["output_dir"])
                if not out_dir.exists():
                    continue
                for f in out_dir.glob("*"):
                    if f.is_file():
                        zf.write(f, arcname=f"{folder_unique}/{f.name}")

            # 把失败/无输出的项目写到 manifest 里，让用户知道
            skipped = [
                {"id": r["id"], "name": r["name"], "status": r["status"]}
                for r in rows if r["status"] != "done"
            ]
            if skipped:
                import json as _j
                zf.writestr("_skipped.json", _j.dumps(skipped, ensure_ascii=False, indent=2))

        buf.seek(0)
        # 按 64KB chunk 流出去
        while chunk := buf.read(64 * 1024):
            yield chunk

    return StreamingResponse(
        _gen(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="projects_export.zip"'},
    )


# ---------------------------------------------------------------------------
# 后台 pipeline 包装
# ---------------------------------------------------------------------------
def _run_pipeline(
    task_id: str,
    input_path: Path,
    output_dir: Path,
    config_path: str,
    diarize: bool,
    quality: str,
) -> None:
    """后台线程：调用 pipeline.run()，通过回调更新 SQLite 进度。"""
    ts = datetime.now().isoformat(timespec="seconds")
    _update_task(task_id, status="running", started_at=ts, message="开始处理")

    # ASR 看门狗：如果 pipeline 跑在后台线程里卡住（比如 FunASR PUNC 阶段死锁），
    # 超过 ASR_STALL_TIMEOUT_S 分钟没有任何回调，就主动标 failed 并抛异常退出。
    # 这样前端会看到"失败"而不是"永远处理中"。
    import time as _t
    last_cb_time = [_t.time()]
    watchdog_exc: list[Exception | None] = [None]
    stop = threading.Event()

    last_stage = [""]  # 闭包：记录上次 callback 的 stage，变了就更新时间戳

    def _cb(stage: str, ratio: float, msg: str) -> None:
        last_cb_time[0] = _t.time()
        fields: dict = {"progress": ratio, "stage": stage, "message": msg}
        # 阶段切换：写 stage_started_at（前端用来显示"已用时间"，不被页面刷新重置）
        if stage != last_stage[0]:
            fields["stage_started_at"] = datetime.now().isoformat(timespec="seconds")
            last_stage[0] = stage
        _update_task(task_id, **fields)

    def _watchdog() -> None:
        while not stop.wait(60):          # wait 兼做 sleep + 可中断
            elapsed = _t.time() - last_cb_time[0]
            if elapsed >= ASR_STALL_TIMEOUT_S:
                err = f"ASR 阶段无回调超过 {int(elapsed // 60)} 分钟，主动终止"
                watchdog_exc[0] = RuntimeError(err)
                logger.error(f"[web] 任务 {task_id} 看门狗触发: {err}")
                _update_task_failed_if_running(task_id, str(watchdog_exc[0]))
                return

    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        cfg = load_config(config_path, strict_models=False)
        overrides = {
            "output_dir": str(output_dir),
            "diarize": diarize,
            "quality": quality,
        }
        result = pipeline_run(input_path, cfg, overrides, on_progress=_cb)
        # pipeline_run 返回前再检查一次看门狗：可能在最后一次 callback 之后卡住
        if watchdog_exc[0] is not None:
            raise watchdog_exc[0]

        meta = {
            "duration_s": result.duration_s,
            "elapsed_s": result.elapsed_s,
            "rtf": result.rtf,
            "segments_count": result.segments_count,
            "formats": list(result.written.keys()),
        }

        # 抽 transcript（从 .json 读 segments 数组）
        transcript_json = _read_transcript(result.written.get("json"))
        try:
            segs = json.loads(transcript_json) if transcript_json else []
        except Exception:
            segs = []

        # 标记 done：阶段立刻推进，下载按钮立刻可用
        _update_task(
            task_id,
            status="done",
            progress=1.0,
            stage="done",
            message=f"完成：{result.segments_count} 段",
            ended_at=datetime.now().isoformat(timespec="seconds"),
            meta_json=json.dumps(meta, ensure_ascii=False),
        )

        # 后台渐进推 transcript：每 300ms 推一段，前端看起来像"实时"
        if segs:
            threading.Thread(
                target=_stream_transcript,
                args=(task_id, segs),
                daemon=True,
            ).start()
        else:
            _update_task(task_id, transcript=transcript_json)

    except (ConfigLoadError, FileNotFoundError, RuntimeError) as e:
        logger.exception(f"[web] 任务 {task_id} 失败")
        _update_task(
            task_id,
            status="failed",
            error=f"{type(e).__name__}: {e}",
            ended_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[web] 任务 {task_id} 未预期失败")
        _update_task(
            task_id,
            status="failed",
            error=f"Unexpected: {type(e).__name__}: {e}",
            ended_at=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        stop.set()  # pipeline 结束（成功/失败都算）：停掉看门狗线程


def _stream_transcript(task_id: str, segs: list) -> None:
    """后台线程：渐进把 segments 写入 DB，前端轮询时增量渲染。"""
    import time

    # 根据段数动态间隔：段多间隔短，避免用户等太久
    if len(segs) <= 20:
        interval = 0.5
    elif len(segs) <= 60:
        interval = 0.3
    else:
        interval = 0.15

    for i in range(1, len(segs) + 1):
        partial = json.dumps(segs[:i], ensure_ascii=False)
        _update_task(task_id, transcript=partial)
        time.sleep(interval)
    # 全部推完：写最终版
    _update_task(task_id, transcript=json.dumps(segs, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 启动入口（python -m asr.web 或 uvicorn asr.web:app）
# ---------------------------------------------------------------------------
def main() -> None:
    """命令行启动：python -m asr.web [--host 0.0.0.0] [--port 8000]"""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Classroom ASR Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "asr.web:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()