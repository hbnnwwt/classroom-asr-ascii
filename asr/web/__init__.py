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
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..analyze import compute_stats
from ..artifacts import atomic_write_text
from ..config import LLMConfig, ConfigLoadError, load_config, write_llm_section
from ..feedback import (
    build_deep_messages,
    build_feedback_messages,
    build_polish_messages,
)
from ..llm import LLMError, chat, llm_enabled
from ..media import probe
from ..mdrender import render_diff, render_markdown
from ..merge import merge_audios, write_part_srts
from ..pipeline import close_cached_engines
from ..pipeline import run as pipeline_run
from .access import AccessMiddleware, loopback
from .runtime import WebRuntime
from .repository import AdmissionError, TaskRepository, task_dict
from .execution import ExecutionManager, QueueFull, TaskBusy, current_owner


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
DB_PATH = PACKAGE_DIR / "web_state.db"
UPLOAD_DIR = PACKAGE_DIR / "uploads"
OUTPUT_ROOT = PACKAGE_DIR / "outputs"
DEFAULT_RUNTIME = WebRuntime.from_environment()
_executor_lock = threading.Lock()


def _runtime() -> WebRuntime:
    injected = getattr(app.state, "runtime", None)
    if injected is not None:
        return injected
    # Compatibility constants remain useful to embedded callers; configuration stays fixed.
    return WebRuntime(DEFAULT_RUNTIME.config_path, DB_PATH, UPLOAD_DIR, OUTPUT_ROOT,
                      STATIC_DIR, DEFAULT_RUNTIME.password, settings_lock=DEFAULT_RUNTIME.settings_lock)


def _repository() -> TaskRepository:
    return TaskRepository(_runtime().db_path)


def _executor() -> ExecutionManager:
    with _executor_lock:
        manager = getattr(app.state, "execution", None)
        if manager is None:
            runtime = _runtime()
            manager = ExecutionManager(runtime.workers, runtime.max_waiting)
            app.state.execution = manager
        return manager


def _config_path(value=None) -> Path:
    try:
        return _runtime().resolve_config(value)
    except ValueError:
        raise HTTPException(400, {"code": "config_not_allowed", "message": "Web 配置由服务器固定"})


def _admission_error(exc):
    if isinstance(exc, QueueFull):
        return HTTPException(503, {"code": "queue_full"}, headers={"Retry-After": "5"})
    code = "task_busy" if isinstance(exc, TaskBusy) else exc.code
    status = 404 if code in ("task_not_found", "upload_not_found") else 409
    detail = {"code": code}
    if getattr(exc, "task_id", None):
        detail["task_id"] = exc.task_id
    return HTTPException(status, detail)

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
    return _repository().connect()


def _init_db() -> None:
    """Additive schema migrations preserve existing tasks."""
    _repository().initialize()


def _row_to_dict(row: sqlite3.Row) -> dict:
    item = task_dict(row)
    meta = item["meta"]
    active = _executor().is_active(item["id"])
    item["execution_active"] = active
    item["can_delete"] = not active and item["status"] not in ("pending", "running")
    item["transcription_status"] = "done" if item["stage"] == "ai" else item["status"]
    attempts = meta.get("ai_attempts", {})
    states = [a.get("status") for a in attempts.values()]
    legacy_ai = ("running" if item["stage"] == "ai" and item["status"] in ("pending", "running") else
                 "failed" if meta.get("ai_error") or meta.get("ai_errors") else
                 "succeeded" if meta.get("ai_files") else "idle")
    item["ai_status"] = next((s for s in ("running", "queued", "failed", "succeeded") if s in states), legacy_ai)
    item["available_formats"] = _available_formats(Path(item["output_dir"]), meta) if item["transcription_status"] == "done" else []
    return item


def _update_task(task_id: str, **fields) -> None:
    return _repository().update(task_id, owner=current_owner(), **fields)


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
    runtime = _runtime()
    runtime.upload_dir.mkdir(parents=True, exist_ok=True)
    runtime.output_root.mkdir(parents=True, exist_ok=True)
    _init_db()
    _repository().recover_interrupted()
    _executor()
    logger.info(f"[web] 数据库: {runtime.db_path}")
    yield
    _executor().shutdown()
    del app.state.execution
    close_cached_engines()


app = FastAPI(title="Classroom ASR", version="1.0", lifespan=lifespan)
app.add_middleware(AccessMiddleware, password=lambda: _runtime().password)


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    """管理界面：项目列表。"""
    return FileResponse(
        str(_runtime().static_dir / "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/task/{task_id}", response_class=HTMLResponse)
def task_page(task_id: str) -> FileResponse:
    """项目详情页：单项目转写进度 + transcript。"""
    return FileResponse(
        str(_runtime().static_dir / "task.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page() -> FileResponse:
    """设置页：LLM 配置表单（AI 反馈入口跳转目标）。"""
    return FileResponse(
        str(_runtime().static_dir / "settings.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/static/{path:path}")
def static_file(path: str) -> FileResponse:
    """静态文件：no-cache 保证改了前端立即生效。"""
    file_path = _safe_file(_runtime().static_dir, path)
    if file_path is None:
        raise HTTPException(404, "not found")
    return FileResponse(
        str(file_path),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.post("/api/uploads", status_code=201)
def create_upload_session():
    return _repository().create_upload()


@app.get("/api/uploads/{upload_id}")
def get_upload_session(upload_id: str):
    session = _repository().get_upload(upload_id)
    if session is None:
        raise HTTPException(404, {"code": "upload_not_found"})
    return session


@app.post("/api/uploads/{upload_id}/cancel")
def cancel_upload_session(upload_id: str):
    try:
        return _repository().cancel_upload(upload_id)
    except AdmissionError as exc:
        raise _admission_error(exc)


async def _accept_upload(files, name, config, diarize, quality, ai_feedback, upload_id, *, legacy=False):
    config_path = _config_path(config)
    if not files or len(files) > MAX_MERGE_PARTS:
        raise HTTPException(400, f"请选择 1 至 {MAX_MERGE_PARTS} 个文件")
    if quality not in ("fast", "balanced", "high"):
        raise HTTPException(400, "不支持的质量档位")
    runtime = _runtime()
    runtime.upload_dir.mkdir(parents=True, exist_ok=True)
    task_id = uuid.uuid4().hex[:12]
    out_dir = runtime.output_root / task_id
    parts_dir = runtime.upload_dir / f"{task_id}_parts"
    multi = len(files) > 1
    saved = []
    accepted = False
    try:
        if multi:
            parts_dir.mkdir(parents=True)
        names = []
        for index, file in enumerate(files):
            if not file.filename:
                raise HTTPException(400, "文件名为空")
            filename = Path(file.filename.replace("\\", "/")).name
            if filename in ("", ".", ".."):
                raise HTTPException(400, "文件名无效")
            names.append(filename)
            dest = parts_dir / f"{index:02d}_{filename}" if multi else runtime.upload_dir / f"{task_id}_{filename}"
            saved.append(dest)  # register before opening: partial writes/probe failure must be cleaned
            with dest.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    output.write(chunk)
            if not legacy:
                try:
                    info = await run_in_threadpool(probe, dest)
                except Exception:
                    raise HTTPException(400, f"无法探测 {filename}，请检查媒体文件")
                if not info.audio_streams:
                    raise HTTPException(400, f"{filename} 没有音轨")
        out_dir.mkdir(parents=True)
        final_name = name.strip() or Path(names[0]).stem
        display = " + ".join(names)
        meta = {"ai_feedback": ai_feedback, "quality": quality}
        if multi:
            meta["parts"] = names
        task = {"id": task_id, "name": final_name, "filename": display,
                "input_path": str(out_dir / "merged.wav" if multi else saved[0]),
                "output_dir": str(out_dir), "meta_json": json.dumps(meta, ensure_ascii=False)}
        def work():
            if not _repository().claim(task_id, current_owner()):
                return
            target = _run_multi_task if multi else _run_single_task
            target(task_id, saved if multi else saved[0], out_dir,
                   str(config_path), diarize, quality, ai_feedback)
        try:
            _executor().submit(task_id, work,
                               admit=lambda: _repository().accept_task(task, upload_id))
        except (QueueFull, TaskBusy, AdmissionError) as exc:
            raise _admission_error(exc)
        accepted = True
        data = {"task_id": task_id, "filename": display}
        if not legacy:
            data["name"] = final_name
        return JSONResponse(data)
    finally:
        if not accepted:
            for path in saved:
                path.unlink(missing_ok=True)
            if multi:
                shutil.rmtree(parts_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)


@app.post("/upload")
async def upload(
    file: UploadFile = File(...), config: str = Form("config.yaml"),
    diarize: bool = Form(False), quality: str = Form("balanced"),
    upload_id: str | None = Header(None, alias="X-Upload-Id"),
) -> JSONResponse:
    return await _accept_upload([file], "", config, diarize, quality, False, upload_id, legacy=True)


@app.get("/status/{task_id}")
def status(task_id: str) -> JSONResponse:
    """查询任务进度（前端轮询此端点）。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    return JSONResponse(_row_to_dict(row))


# 下载名剥后缀白名单：仅真实媒体/文档后缀才剥一次。任务名里的点号不是
# 分隔符（"3.2节 复习"），Path.stem 会把 ".2节 复习" 当后缀剥掉——双剥后
# 下载名截成 "3"。媒体后缀与 probe 支持面对齐；.srt 补 part_files 兜底路径。
_KNOWN_STEM_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".srt",
}


def _strip_known_ext(name: str) -> str:
    """仅当后缀在白名单内才剥一次；否则原样返回（含点号任务名不截断）。"""
    p = Path(name)
    return p.stem if p.suffix.lower() in _KNOWN_STEM_EXTS else name


def _ai_artifact(out_dir: Path, meta: dict, fmt: str) -> Path | None:
    """AI 产物定位：meta.ai_files 登记名优先，无登记 glob 兜底。

    登记优先是硬要求——改名重跑后目录里新旧两份并存，glob 顺序不定
    会取到旧文件；无登记（半程失败的老任务）glob 兜底，兼容"已产出
    一份"的可下载性。
    """
    if "ai_files" in meta:
        return _safe_file(out_dir, (meta.get("ai_files") or {}).get(fmt))
    files = sorted(out_dir.glob(f"*.{fmt}.md"))
    return next((f for f in files if _safe_file(out_dir, f.name)), None)


def _safe_file(root: Path, name) -> Path | None:
    if not isinstance(name, (str, Path)) or not str(name):
        return None
    root = Path(root).resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _output_artifact(out_dir: Path, meta: dict, fmt: str) -> Path | None:
    if fmt in _AI_FMTS:
        return _ai_artifact(out_dir, meta, fmt)
    if "output_files" in meta:
        files = meta.get("output_files") or {}
        key = "replace_log" if fmt == "replace.log" else fmt
        return _safe_file(out_dir, files.get(fmt, files.get(key)))
    # Old task compatibility is deliberately restricted to its own output directory.
    files = sorted(out_dir.glob(f"*.{fmt}"))
    return next((f for f in files if not re.match(r"^\d{2}_", f.name)
                 and not re.search(r"\.(?:feedback|polished|deep)\.md$", f.name)
                 and _safe_file(out_dir, f.name)), None)


def _available_formats(out_dir, meta):
    return [fmt for fmt in ("srt", "txt", "vtt", "md", "json", "replace.log", "mp4", *_AI_FMTS)
            if _output_artifact(out_dir, meta, fmt)]


# AI 产物三件：点评报告 / 优化逐字稿 / 深度优化讲义（成稿化）
_AI_FMTS = ("feedback", "polished", "deep")


@app.get("/download/{task_id}/{fmt}")
def download(task_id: str, fmt: str):
    """下载指定格式输出（SRT/VTT/MD/TXT/JSON/REPLACE_LOG/AI 反馈产物）。"""
    ALLOWED = {"srt", "vtt", "md", "txt", "json", "replace.log", "mp4",
               *_AI_FMTS}
    if fmt not in ALLOWED:
        raise HTTPException(400, f"不支持的格式: {fmt}")

    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    # AI stage 是转写完成后的追加阶段：进入它时转写产物（srt/txt/…）已最终
    # 落盘，锁下载只会让用户白等一次 LLM。stage=ai 放行；asr 等其余阶段
    # running 仍拒绝（转写本体可能是半成品，防半成品语义保留）。
    if row["status"] != "done" and row["stage"] != "ai":
        raise HTTPException(409, f"任务未完成: {row['status']}")

    out_dir = Path(row["output_dir"])
    meta = task_dict(row)["meta"]
    target = _output_artifact(out_dir, meta, fmt)
    if target is None:
        raise HTTPException(404, f"输出文件不存在: {fmt}")
    # 剥后缀只认白名单：任务名/文件名里的点号（"3.2节 复习.mp4" 的 ".2节 复习"
    # 不是后缀）不许被 Path.stem 连剥两次把名字截断
    stem = _strip_known_ext(row["name"] or row["filename"])
    suffix = f".{fmt}.md" if fmt in _AI_FMTS else f".{fmt}"
    return FileResponse(str(target), filename=f"{stem}{suffix}")


@app.get("/api/tasks/{task_id}/ai/{fmt}")
def ai_view(task_id: str, fmt: str, diff: bool = False):
    """AI 产物在线阅读：读 md → render_markdown → {name, html, diff}。

    与 /download 同一套 _ai_artifact 登记名解析；下载仍走 /download
    （前端查看层里直接挂它的链接），本端点只管看。

    ?diff=1（polished/deep）：DB 里的原逐字稿 vs 产物做红字比对，
    删改痕迹原地可见；无 transcript 的老任务 diff=false 退普通渲染。
    """
    if fmt not in _AI_FMTS:
        raise HTTPException(400, f"不支持的格式: {fmt}")
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    # 与 /download 同口径：stage=ai 期间放行（转写产物已落盘，AI 产物
    # 部分生成时用户可先看已落盘的那份）
    if row["status"] != "done" and row["stage"] != "ai":
        raise HTTPException(409, f"任务未完成: {row['status']}")
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except ValueError:
        meta = {}
    target = _ai_artifact(Path(row["output_dir"]), meta, fmt)
    if target is None:
        raise HTTPException(404, f"输出文件不存在: {fmt}")
    text = target.read_text(encoding="utf-8")

    diff_ok = False
    if diff and fmt in ("polished", "deep") and row["transcript"]:
        try:
            segs = json.loads(row["transcript"])
        except ValueError:
            segs = []
        if segs:
            original = "\n".join(s.get("text", "") for s in segs)
            return {"name": target.name, "diff": True,
                    "html": render_diff(original, text)}
    return {"name": target.name, "diff": diff_ok,
            "html": render_markdown(text)}


@app.get("/download/{task_id}/part_srt/{index}")
def download_part_srt(task_id: str, index: int):
    """下载某一段的独立 SRT（多段拼接任务，时间归零配原视频用）。"""
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    # 同主下载路由：AI stage 期间分段字幕已最终落盘（拼接字幕在它之前写完），
    # 放行；其余 running 阶段仍 409。
    if row["status"] != "done" and row["stage"] != "ai":
        raise HTTPException(409, f"任务未完成: {row['status']}")

    try:
        meta = json.loads(row["meta_json"] or "{}")
    except ValueError:
        meta = {}
    part_files = meta.get("part_files") or []
    parts = meta.get("parts") or []
    if not 0 <= index < len(part_files):
        raise HTTPException(404, "分段字幕不存在")

    target = _safe_file(Path(row["output_dir"]), part_files[index])
    if target is None:
        raise HTTPException(404, f"分段字幕文件缺失: {part_files[index]}")
    # 下载名用该段原名（去真实后缀），用户拿到即可对上原视频；
    # 段名含点号（"3.2节 复习.mp4"）不许被截断，兜底名（.srt）不双后缀
    name = parts[index] if index < len(parts) else part_files[index]
    return FileResponse(str(target), filename=f"{_strip_known_ext(name)}.srt")


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
    files: list[UploadFile] = File(...), name: str = Form(""),
    config: str = Form("config.yaml"), diarize: bool = Form(False),
    quality: str = Form("balanced"), ai_feedback: bool = Form(False),
    upload_id: str | None = Header(None, alias="X-Upload-Id"),
):
    return await _accept_upload(files, name, config, diarize, quality, ai_feedback, upload_id)


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
    manager = _executor()
    try:
        with manager.mutation_lock():
            if manager.is_active(task_id):
                raise TaskBusy(task_id)
            row = _repository().delete_terminal(task_id)
    except (TaskBusy, AdmissionError) as exc:
        raise _admission_error(exc)
    if row is None:
        return False
    runtime = _runtime()
    out_dir = Path(row["output_dir"]).resolve()
    if out_dir.is_relative_to(runtime.output_root) and out_dir != runtime.output_root:
        shutil.rmtree(out_dir, ignore_errors=True)
    in_path = Path(row["input_path"]).resolve()
    if in_path.is_relative_to(runtime.upload_dir) and in_path.is_file():
        in_path.unlink(missing_ok=True)
    parts_dir = runtime.upload_dir / f"{task_id}_parts"
    resolved_parts = parts_dir.resolve()
    if resolved_parts.is_relative_to(runtime.upload_dir) and resolved_parts != runtime.upload_dir and parts_dir.is_dir():
        shutil.rmtree(parts_dir, ignore_errors=True)
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

    deleted, failed, failure_details = [], [], []
    for tid in ids:
        if not isinstance(tid, str):
            continue
        try:
            if _delete_one(tid):
                deleted.append(tid)
            else:
                failed.append(tid)
                failure_details.append({"id": tid, "code": "task_not_found"})
        except HTTPException as exc:
            failed.append(tid)
            failure_details.append({"id": tid, **(exc.detail if isinstance(exc.detail, dict) else {"code": "delete_failed"})})
    return JSONResponse({"deleted": deleted, "failed": failed, "failure_details": failure_details})


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
    # stage 一并取出：running+stage=ai 的任务转写产物已终态，也要进 ZIP。
    with closing(_db()) as conn:
        rows = conn.execute(
            "SELECT id, name, filename, output_dir, status, stage, meta_json FROM tasks WHERE id IN ({})".format(
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
                # 与主下载路由同口径：AI stage 期间产物已终态可导出，
                # 其余未完成任务跳过（防半成品进 ZIP）
                if row["status"] != "done" and row["stage"] != "ai":
                    continue
                # 每个项目一个子目录：<项目名>/<原文件名>.srt 等
                folder = row["name"] or row["filename"] or row["id"]
                folder = Path(folder.replace("\\", "/")).name
                if folder in ("", ".", ".."):
                    folder = row["id"]
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
                meta = task_dict(row)["meta"]
                files = {_output_artifact(out_dir, meta, fmt) for fmt in _available_formats(out_dir, meta)}
                files.update(_safe_file(out_dir, name) for name in meta.get("part_files", []))
                for f in sorted((f for f in files if f is not None), key=lambda f: f.name):
                    zf.write(f, arcname=f"{folder_unique}/{f.name}")

            # 把失败/无输出的项目写到 manifest 里，让用户知道。
            # 已进 ZIP 的（含 AI stage 任务）不得再列成 skipped，否则 manifest 自相矛盾
            skipped = [
                {"id": r["id"], "name": r["name"], "status": r["status"]}
                for r in rows
                if r["status"] != "done" and r["stage"] != "ai"
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
# AI 反馈：LLM 可用性查询 + done 任务手动补跑
# ---------------------------------------------------------------------------
@app.get("/api/llm/status")
def llm_status(config: str = "config.yaml"):
    """LLM 可用性：前端据此显示/隐藏 AI 反馈入口。

    配置加载失败也按"未启用"返回——状态查询不许被配置错误打挂。
    """
    config_path = _config_path(config)
    try:
        cfg = load_config(config_path, strict_models=False)
    except ConfigLoadError:
        return JSONResponse({"enabled": False, "model": None})
    return JSONResponse({"enabled": llm_enabled(cfg),
                         "model": cfg.llm.model or None})


class _LLMBody(BaseModel):
    """设置页表单体。全字段可空——表单即状态：清空 base_url/model 保存 = 关闭功能。"""
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    timeout: int = 300
    max_tokens: int = 16384


@app.get("/api/llm/settings")
def llm_settings_get(config: str = "config.yaml"):
    """设置页回填：llm 节当前值。配置加载失败按空态返回（页面不许被打挂）。"""
    config_path = _config_path(config)
    try:
        cfg = load_config(config_path, strict_models=False)
    except ConfigLoadError:
        cfg = None
    llm = cfg.llm if cfg is not None else LLMConfig()
    return JSONResponse({"base_url": llm.base_url, "api_key": None,
                         "api_key_set": bool(llm.api_key),
                         "api_key_masked": "********" if llm.api_key else "",
                         "model": llm.model, "timeout": llm.timeout,
                         "max_tokens": llm.max_tokens})


@app.post("/api/llm/settings")
def llm_settings_save(body: _LLMBody, config: str = "config.yaml"):
    """保存 llm 节：文本手术写回（保注释，见 write_llm_section），原子替换。"""
    path = _config_path(config)
    base_url = body.base_url.strip()
    if base_url and not re.match(r"^https?://", base_url):
        raise HTTPException(422, "base_url 必须以 http:// 或 https:// 开头")
    if body.timeout <= 0 or body.max_tokens <= 0:
        raise HTTPException(422, "timeout / max_tokens 必须为正整数")
    if not path.is_file():
        # 不静默新建：这个文件承载全部 ASR 配置，凭空造一个空的等于毁配置
        raise HTTPException(409, f"配置文件不存在: {config}")

    with _runtime().settings_lock:
        try:
            current = load_config(path, strict_models=False).llm
        except ConfigLoadError:
            raise HTTPException(409, "服务器配置加载失败，请检查配置文件")
        key = current.api_key if body.api_key is None else body.api_key.strip()
        new_text = write_llm_section(
            path.read_text(encoding="utf-8"),
            {"base_url": base_url, "api_key": key,
             "model": body.model.strip(), "timeout": body.timeout,
             "max_tokens": body.max_tokens},
        )
        atomic_write_text(path, new_text)
    enabled = bool(base_url and body.model.strip())
    return JSONResponse({"status": "saved", "enabled": enabled})


@app.post("/api/llm/test")
def llm_test(body: _LLMBody, config: str = "config.yaml"):
    """测试连接：拿传入值试调一次（不落盘）。结果是数据不是错误，永远 200。"""
    config_path = _config_path(config)
    if not (body.base_url.strip() and body.model.strip()):
        return JSONResponse({"ok": False, "error": "base_url 与 model 不能为空"})
    # 测试调用上限 30s / 16 token：点一下按钮挂 300 秒是烂体验
    try:
        saved_key = load_config(config_path, strict_models=False).llm.api_key
    except ConfigLoadError:
        saved_key = ""
    key = saved_key if body.api_key is None else body.api_key.strip()
    cfg = SimpleNamespace(llm=LLMConfig(
        base_url=body.base_url, api_key=key, model=body.model,
        timeout=min(body.timeout, 30), max_tokens=body.max_tokens))
    try:
        reply = chat([{"role": "user", "content": "请只回复：ok"}], cfg,
                     max_tokens=16)
    except LLMError as e:
        error = str(e).replace(key, "********") if key else str(e)
        return JSONResponse({"ok": False, "error": error})
    return JSONResponse({"ok": True, "reply": (reply or "")[:50]})


def _start_ai_run(task_id: str, config: str, fmts: tuple) -> JSONResponse:
    config_path = _config_path(config)
    row = _repository().get(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    if row["status"] != "done":
        raise HTTPException(409, {"code": "task_busy" if row["status"] in ("pending", "running") else "task_not_done"})
    try:
        cfg = load_config(config_path, strict_models=False)
    except ConfigLoadError:
        cfg = None
    if cfg is None or not llm_enabled(cfg):
        raise HTTPException(409, "LLM 未配置（config.yaml 缺 llm 节）")
    def work():
        if _repository().claim(task_id, current_owner()):
            _run_ai_stage(task_id, str(config_path), fmts)
    try:
        _executor().submit(task_id, work, admit=lambda: _repository().queue_ai(task_id, fmts))
    except (QueueFull, TaskBusy, AdmissionError) as exc:
        raise _admission_error(exc)
    return JSONResponse({"status": "started"}, status_code=202)


@app.post("/api/tasks/{task_id}/feedback")
def trigger_ai_feedback(task_id: str, config: str = "config.yaml"):
    """对 done 任务手动补跑 AI 反馈（点评+优化稿两件整组）。"""
    return _start_ai_run(task_id, config, ("feedback", "polished"))


@app.post("/api/tasks/{task_id}/ai/{fmt}")
def trigger_ai_single(task_id: str, fmt: str, config: str = "config.yaml"):
    """单件重生：三件 AI 产物各自独立重新生成（含上传时没生成的深度优化）。"""
    if fmt not in _AI_FMTS:
        raise HTTPException(400, f"不支持的格式: {fmt}")
    return _start_ai_run(task_id, config, (fmt,))


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
    ai_feedback: bool,
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
        _repository().update(task_id, meta_json=json.dumps(meta, ensure_ascii=False))

        # AI 反馈（可选）：拼接字幕写完后串行追加
        if ai_feedback:
            _auto_ai(task_id, config_path)
    except Exception as e:  # noqa: BLE001
        logger.error("[web] merge task {} failed ({})", task_id, type(e).__name__)
        error = "服务器配置加载失败" if isinstance(e, ConfigLoadError) else f"{type(e).__name__}: {e}"
        _update_task(
            task_id, status="failed", error=error,
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
def _auto_ai(task_id, config_path):
    # Keep this task's executor ownership through the handoff; no unregistered thread.
    _repository().queue_ai(task_id, ("feedback", "polished"), owner=current_owner())
    if _repository().claim(task_id, current_owner()):
        _run_ai_stage(task_id, config_path)


def _run_pipeline(task_id, input_path, output_dir, config_path, diarize, quality) -> None:
    """Persist progress only while this execution still owns a nonterminal task."""
    import time
    owner = current_owner()
    _update_task(task_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"), message="开始处理")
    last_callback = [time.monotonic()]
    last_stage = [""]
    stop, expired = threading.Event(), threading.Event()
    def progress(stage, ratio, message):
        if expired.is_set():
            return
        last_callback[0] = time.monotonic()
        fields = {"progress": ratio, "stage": stage, "message": message}
        if stage != last_stage[0]:
            fields["stage_started_at"] = datetime.now().isoformat(timespec="seconds")
            last_stage[0] = stage
        _repository().update(task_id, owner=owner, **fields)
    def watchdog():
        while not stop.wait(min(60, max(0.01, ASR_STALL_TIMEOUT_S / 4))):
            elapsed = time.monotonic() - last_callback[0]
            if elapsed >= ASR_STALL_TIMEOUT_S:
                expired.set()
                error = "ASR 长时间无回调，任务已标记失败；底层执行可能仍在运行"
                if owner is not None:
                    _repository().update(task_id, owner=owner, status="failed", error=error,
                                         ended_at=datetime.now().isoformat(timespec="seconds"))
                else:
                    _update_task_failed_if_running(task_id, error)
                return
    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()
    try:
        cfg = load_config(config_path, strict_models=False)
        accumulated = []
        def segments(new_segments):
            if expired.is_set():
                return
            accumulated.extend({"start": s.start, "end": s.end, "text": s.text,
                                **({"spk": s.spk} if getattr(s, "spk", None) else {})} for s in new_segments)
            _repository().update(task_id, owner=owner, transcript=json.dumps(accumulated, ensure_ascii=False))
        result = pipeline_run(input_path, cfg,
                              {"output_dir": str(output_dir), "diarize": diarize, "quality": quality},
                              on_progress=progress, on_segments=segments)
        if expired.is_set():
            return
        meta = {**_read_meta(task_id), "duration_s": result.duration_s,
                "elapsed_s": result.elapsed_s, "rtf": result.rtf,
                "segments_count": result.segments_count, "formats": list(result.written),
                "output_files": {fmt: Path(file).name for fmt, file in result.written.items()},
                "quality": quality}
        final = getattr(result, "segments", None)
        # Legacy Result constructors may supply only a JSON file and count.
        # A real zero-segment result is authoritative and must clear progress.
        has_final = final is not None and (bool(final) or result.segments_count == 0)
        transcript = (json.dumps([{"start": s.start, "end": s.end, "text": s.text,
                                  **({"spk": s.spk} if getattr(s, "spk", None) else {})} for s in final], ensure_ascii=False)
                      if has_final else _read_transcript(result.written.get("json")))
        _update_task(task_id, status="done", progress=1.0, stage="done",
                     message=f"完成：{result.segments_count} 段",
                     ended_at=datetime.now().isoformat(timespec="seconds"),
                     meta_json=json.dumps(meta, ensure_ascii=False), transcript=transcript)
    except Exception as exc:
        logger.error("[web] task {} failed ({})", task_id, type(exc).__name__)
        if not expired.is_set():
            error = "服务器配置加载失败" if isinstance(exc, ConfigLoadError) else f"{type(exc).__name__}: {exc}"
            _update_task(task_id, status="failed", error=error,
                         ended_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        stop.set()
        watcher.join()


# ---------------------------------------------------------------------------
# AI 反馈 stage（点评 + 优化稿）。铁律：AI 失败不拖垮转写成果。
# ---------------------------------------------------------------------------
def _run_ai_stage(task_id: str, config_path: str, fmts: tuple = ("feedback", "polished")) -> None:
    """Publish complete AI artifacts atomically; failed attempts keep the last version."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    meta = _read_meta(task_id)
    files = dict(meta.get("ai_files") or {})
    errors = dict(meta.get("ai_errors") or {})
    attempts = meta.setdefault("ai_attempts", {})
    meta["ai_feedback"] = True
    meta.pop("ai_error", None)
    for fmt in fmts:
        attempts[fmt] = {"status": "running", "started_at": timestamp}
    def persist(**fields):
        meta["ai_files"] = files
        if errors:
            meta["ai_errors"] = errors
        else:
            meta.pop("ai_errors", None)
        return _update_task(task_id, meta_json=json.dumps(meta, ensure_ascii=False), **fields)
    persist(status="running", stage="ai", progress=0, message="统计课堂数据", stage_started_at=timestamp)
    try:
        cfg = load_config(config_path, strict_models=False)
        if not llm_enabled(cfg):
            raise LLMError("LLM 未配置")
        row = _repository().get(task_id)
        if row is None:
            return
        segs = _read_final_segments(task_id)
        if not segs:
            raise LLMError("无转写结果，无法生成 AI 反馈")
        transcript = "\n".join(f"{s.get('spk')}: {s['text']}" if s.get("spk") else s["text"] for s in segs)
        stem = Path((row["name"] or _strip_known_ext(row["filename"])).replace("\\", "/")).name
        builders = {
            "feedback": lambda: build_feedback_messages(transcript, compute_stats(segs)),
            "polished": lambda: build_polish_messages(transcript),
            "deep": lambda: build_deep_messages(transcript),
        }
        for index, fmt in enumerate(fmts):
            persist(progress=(index + 0.5) / len(fmts), stage="ai", message=f"生成 {fmt}")
            try:
                content = chat(builders[fmt](), cfg)
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("LLM 返回空内容或正文类型异常")
                target = Path(row["output_dir"]) / f"{task_id}_{stem}.{fmt}.md"
                atomic_write_text(target, content)
                files[fmt] = target.name
                errors.pop(fmt, None)
                attempts[fmt].update(status="succeeded", ended_at=datetime.now().isoformat(timespec="seconds"),
                                     input_chars=len(transcript), output_chars=len(content))
            except Exception as exc:
                logger.error("[web] AI {} {} failed ({})", task_id, fmt, type(exc).__name__)
                error = f"{type(exc).__name__}: {exc}"
                if cfg.llm.api_key:
                    error = error.replace(cfg.llm.api_key, "********")
                errors[fmt] = error
                attempts[fmt].update(status="failed", error=error, ended_at=datetime.now().isoformat(timespec="seconds"))
            persist()
        message = "AI 反馈完成" if not any(attempts[f]["status"] == "failed" for f in fmts) else "本次生成失败，仍可查看上次版本"
        persist(status="done", stage="done", progress=1.0, message=message)
    except Exception as exc:
        error = "服务器配置加载失败" if isinstance(exc, ConfigLoadError) else f"{type(exc).__name__}: {exc}"
        for fmt in fmts:
            attempts[fmt].update(status="failed", error=error, ended_at=datetime.now().isoformat(timespec="seconds"))
        meta["ai_error"] = error
        persist(status="done", stage="ai", progress=1.0, message="AI 反馈失败，仍可查看上次版本")


def _run_single_task(task_id, input_path, output_dir, config_path, diarize, quality, ai_feedback) -> None:
    _run_pipeline(task_id, input_path, output_dir, config_path, diarize, quality)
    row = _repository().get(task_id)
    if row and row["status"] == "done" and ai_feedback:
        _auto_ai(task_id, config_path)


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
    parser.add_argument("--config", default=os.environ.get("ASR_WEB_CONFIG", "config.yaml"))
    args = parser.parse_args()

    password = os.environ.get("ASR_WEB_PASSWORD", "")
    if not password and args.host != "localhost" and not loopback(args.host):
        parser.error("非本机监听必须先设置 ASR_WEB_PASSWORD")
    os.environ["ASR_WEB_CONFIG"] = str(Path(args.config).resolve())
    DEFAULT_RUNTIME.config_path = Path(args.config).resolve()
    DEFAULT_RUNTIME.password = password

    uvicorn.run(
        "asr.web:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
