"""
Web UI（FastAPI）：
- POST /upload        上传视频文件，返回 task_id
- GET  /status/{id}   查询任务状态/进度（轮询）
- GET  /download/{id}/{fmt} 下载指定格式输出（含旧任务合并视频 mp4）
- POST /api/projects  创建项目：files=[1 个] 普通转写；files=[多个]
                      逐段抽音轨拼接后一次转写（时间轴天然连续）
- GET  /              单页前端

数据持久化：SQLite（默认 ./web/web_state.db），重启后任务记录不丢，
但任务输出文件本身存于磁盘（不在 SQLite 里），重启后输出仍可下载。
"""
from __future__ import annotations

import json
import re
import shutil
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
from ..media import probe
from ..merge import merge_audios, write_part_srts
from ..pipeline import close_cached_engines
from ..pipeline import run as pipeline_run


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
DB_PATH = PACKAGE_DIR / "web_state.db"
UPLOAD_DIR = PACKAGE_DIR / "uploads"
OUTPUT_ROOT = PACKAGE_DIR / "outputs"

# 多段拼接：单次段数上限
MAX_MERGE_PARTS = 20

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
    # P1-1：进程退出前释放缓存的引擎（模型内存/显存）
    close_cached_engines()


app = FastAPI(title="Classroom ASR", version="1.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    """管理界面：项目列表。"""
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/task/{task_id}", response_class=HTMLResponse)
def task_page(task_id: str) -> FileResponse:
    """项目详情页：单项目转写进度 + transcript。"""
    return FileResponse(
        str(STATIC_DIR / "task.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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
    ALLOWED = {"srt", "vtt", "md", "txt", "json", "replace.log", "mp4"}
    if fmt not in ALLOWED:
        raise HTTPException(400, f"不支持的格式: {fmt}")

    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    if row["status"] != "done":
        raise HTTPException(409, f"任务未完成: {row['status']}")

    out_dir = Path(row["output_dir"])
    # 排除分段文件（{两位序号}_ 前缀，如 00_a.srt）——多段任务的整体 SRT
    # 是 merged.srt，glob 顺序不保证它排在分段文件前面
    files = [f for f in out_dir.glob(f"*.{fmt}")
             if not re.match(r"^\d{2}_", f.name)]
    if not files:
        raise HTTPException(404, f"输出文件不存在: {fmt}")
    target = files[0]
    return FileResponse(
        str(target),
        filename=f"{Path(row['filename']).stem}.{fmt}",
    )


@app.get("/download/{task_id}/part_srt/{index}")
def download_part_srt(task_id: str, index: int):
    """下载某一段的独立 SRT（多段拼接任务，时间归零配原视频用）。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    if row["status"] != "done":
        raise HTTPException(409, f"任务未完成: {row['status']}")

    try:
        meta = json.loads(row["meta_json"] or "{}")
    except ValueError:
        meta = {}
    part_files = meta.get("part_files") or []
    parts = meta.get("parts") or []
    if not 0 <= index < len(part_files):
        raise HTTPException(404, "分段字幕不存在")

    target = Path(row["output_dir"]) / part_files[index]
    if not target.exists():
        raise HTTPException(404, f"分段字幕文件缺失: {part_files[index]}")
    # 下载名用该段原名（去后缀），用户拿到即可对上原视频
    name = parts[index] if index < len(parts) else part_files[index]
    return FileResponse(str(target), filename=f"{Path(name).stem}.srt")


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
    files: list[UploadFile] = File(...),
    name: str = Form(""),
    config: str = Form("config.yaml"),
    diarize: bool = Form(False),
    quality: str = Form("balanced"),
):
    """新建项目：files=[1 个] 普通转写；files=[多个] 逐段抽音轨拼接后一次转写。

    单/多差异只在落盘布局与 meta——转写路径同一条（拼接产物就是普通输入）。
    """
    if not files:
        raise HTTPException(400, "未选择文件")
    if len(files) > MAX_MERGE_PARTS:
        raise HTTPException(400, f"单次最多拼接 {MAX_MERGE_PARTS} 段")

    task_id = uuid.uuid4().hex[:12]
    ts = datetime.now().isoformat(timespec="seconds")
    out_dir = OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    multi = len(files) > 1
    parts_dir = UPLOAD_DIR / f"{task_id}_parts"
    if multi:
        parts_dir.mkdir(parents=True, exist_ok=True)

    # 保存 + 逐个探测（无音轨/损坏文件当场 400，不进任务队列）
    saved: list[Path] = []
    try:
        for i, f in enumerate(files):
            if not f.filename:
                raise HTTPException(400, "文件名为空")
            fname = Path(f.filename).name
            # 多段：{序号}_{原名}（序号保序、防重名）；单段：老路径布局
            dest = parts_dir / f"{i:02d}_{fname}" if multi else UPLOAD_DIR / f"{task_id}_{fname}"
            with open(dest, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    out.write(chunk)
            try:
                info = probe(dest)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"无法探测 {fname}: {e}")
            if not info.audio_streams:
                raise HTTPException(400, f"{fname} 没有音轨")
            saved.append(dest)
    except HTTPException:
        # 半截会话不留垃圾
        for p in saved:
            p.unlink(missing_ok=True)
        if multi:
            shutil.rmtree(parts_dir, ignore_errors=True)
        raise

    names = [Path(f.filename).name for f in files]
    display = " + ".join(names)
    final_name = name.strip() or Path(names[0]).stem

    if multi:
        meta = {"parts": names}
        input_path = str(out_dir / "merged.wav")
        with closing(_db()) as conn:
            conn.execute(
                """INSERT INTO tasks
                   (id, name, filename, input_path, output_dir, status, created_at, meta_json)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (task_id, final_name, display, input_path, str(out_dir), ts,
                 json.dumps(meta, ensure_ascii=False)),
            )
        threading.Thread(
            target=_run_multi_task,
            args=(task_id, saved, out_dir, config, diarize, quality),
            daemon=True,
        ).start()
    else:
        with closing(_db()) as conn:
            conn.execute(
                """INSERT INTO tasks
                   (id, name, filename, input_path, output_dir, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (task_id, final_name, names[0], str(saved[0]), str(out_dir), ts),
            )
        threading.Thread(
            target=_run_pipeline,
            args=(task_id, saved[0], out_dir, config, diarize, quality),
            daemon=True,
        ).start()

    filename = display if multi else names[0]
    return JSONResponse({"task_id": task_id, "name": final_name, "filename": filename})


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
# 后台多段任务：拼音频（stage=merge）→ 转写 pipeline → 每段独立 SRT
# ---------------------------------------------------------------------------
def _read_meta(task_id: str) -> dict:
    """读当前 meta_json（损坏/缺失 → 空 dict）。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT meta_json FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["meta_json"] or "{}")
    except ValueError:
        return {}


def _read_final_segments(task_id: str) -> list[dict]:
    """读任务最终 transcript 列（done 时与状态原子写入的 JSON 数组）。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT transcript FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return []
    try:
        segs = json.loads(row["transcript"] or "[]")
    except ValueError:
        return []
    return segs if isinstance(segs, list) else []


def _run_multi_task(
    task_id: str,
    part_paths: list[Path],
    output_dir: Path,
    config_path: str,
    diarize: bool,
    quality: str,
) -> None:
    """后台线程：逐段抽音轨拼接（stage=merge）→ 转写 → 每段独立 SRT。

    进度口径：拼音频占 0~5%（pipeline 自带 0.05 起，无缝衔接）。
    """
    ts = datetime.now().isoformat(timespec="seconds")
    _update_task(task_id, status="running", started_at=ts, message="拼接音轨")

    meta = _read_meta(task_id)
    # 进度消息/文件名用原始名（段文件已被加上序号前缀）
    part_names = meta.get("parts") or [p.name for p in part_paths]

    def _cb(ratio: float, msg: str) -> None:
        _update_task(task_id, progress=ratio * 0.05, stage="merge", message=msg)

    merged_wav = Path(output_dir) / "merged.wav"
    try:
        cfg = load_config(config_path, strict_models=False)
        _, durations = merge_audios(part_paths, merged_wav, cfg, on_progress=_cb)

        # pipeline 的 done 更新会整体覆盖 meta，跑完把分段元数据补回去
        _run_pipeline(task_id, merged_wav, Path(output_dir), config_path, diarize, quality)

        # pipeline 内部兜底失败时标 failed 但不抛——看状态说话：
        # 失败就到此为止，别拿空 transcript 写出一堆空分段字幕
        with closing(_db()) as conn:
            status = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()["status"]
        if status != "done":
            return

        # 整体时间轴 → 每段一份独立 SRT（时间归零，配原视频用）
        stems = [Path(n).stem for n in part_names]
        part_files = write_part_srts(
            _read_final_segments(task_id), durations, stems, Path(output_dir),
            output_cfg=cfg.output,
        )
        meta = _read_meta(task_id)
        meta.update({
            "parts": part_names,
            "part_files": [p.name for p in part_files],
        })
        _update_task(task_id, meta_json=json.dumps(meta, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[web] 拼接任务 {task_id} 失败")
        _update_task(
            task_id, status="failed", error=f"{type(e).__name__}: {e}",
            ended_at=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        # 段文件已并入 merged.wav（成败都已无用），清掉防 uploads 膨胀
        for p in part_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        if part_paths:
            try:
                Path(part_paths[0]).parent.rmdir()  # 目录空才删，非空（共享目录）忽略
            except OSError:
                pass


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
        # 渐进 transcript：引擎每交付一个 VAD 组的段落就累积写库，前端
        # 轮询 /status 时在识别过程中就能看到文字（94 分钟课约 2~3 分钟
        # 出第一段，之后每 ~10s 滚出一组）。组结果即最终结果，无回炉；
        # done 时仍用最终 .json 覆盖（后处理 ITN/术语替换可能改写文本）。
        acc: list[dict] = []

        def _on_segs(new_segs) -> None:
            acc.extend(
                {"start": s.start, "end": s.end, "text": s.text} for s in new_segs
            )
            _update_task(task_id, transcript=json.dumps(acc, ensure_ascii=False))

        result = pipeline_run(input_path, cfg, overrides, on_progress=_cb,
                              on_segments=_on_segs)
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

        # 抽 transcript（从 .json 读 segments 数组）。必须与 done 同一笔 UPDATE：
        # 前端见 done 即停止轮询，done 之后再后补的段永远到不了页面。
        # （旧版 _stream_transcript 线程每 0.15~0.5s 逐段"伪直播"——94 分钟课
        #  1004 段要推 ~150s，前端早就停轮询，页面冻在第一段。）
        transcript_json = _read_transcript(result.written.get("json"))

        # 标记 done：阶段立刻推进，下载按钮立刻可用（transcript 原子一并写入）
        _update_task(
            task_id,
            status="done",
            progress=1.0,
            stage="done",
            message=f"完成：{result.segments_count} 段",
            ended_at=datetime.now().isoformat(timespec="seconds"),
            meta_json=json.dumps(meta, ensure_ascii=False),
            transcript=transcript_json,
        )

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