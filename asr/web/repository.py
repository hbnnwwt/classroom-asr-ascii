"""SQLite task ownership and atomic upload admission, without process-local races."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AdmissionError(Exception):
    def __init__(self, code: str, task_id: str | None = None):
        super().__init__(code)
        self.code, self.task_id = code, task_id


def task_dict(row) -> dict:
    result = dict(row)
    try:
        result["meta"] = json.loads(result.pop("meta_json") or "{}")
    except (ValueError, TypeError):
        result["meta"] = {}
    if not isinstance(result["meta"], dict):
        result["meta"] = {}
    for key in ("name", "stage_started_at", "transcript"):
        result[key] = result.get(key) or ""
    return result


class TaskRepository:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self):
        with self.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, input_path TEXT NOT NULL,
                output_dir TEXT NOT NULL, status TEXT NOT NULL, progress REAL DEFAULT 0,
                stage TEXT DEFAULT '', message TEXT DEFAULT '', error TEXT DEFAULT '',
                created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
                meta_json TEXT DEFAULT '', transcript TEXT DEFAULT '', name TEXT DEFAULT '',
                stage_started_at TEXT DEFAULT '', execution_id TEXT DEFAULT '')""")
            existing = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            for col in ("name", "transcript", "stage_started_at", "execution_id"):
                if col not in existing:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT ''")
            conn.execute("""CREATE TABLE IF NOT EXISTS upload_sessions (
                upload_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                task_id TEXT, created_at TEXT NOT NULL)""")

    def get(self, task_id):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return task_dict(row) if row else None

    def update(self, task_id, *, owner=None, **fields):
        allowed = {"status", "progress", "stage", "message", "error", "started_at",
                   "ended_at", "meta_json", "transcript", "name", "stage_started_at", "execution_id"}
        if not fields or not set(fields) <= allowed:
            if fields:
                raise ValueError("unknown task fields")
            return False
        sql = "UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?"
        params = [*fields.values(), task_id]
        if owner is not None:
            sql += " AND execution_id=? AND status IN ('pending','running')"
            params.append(owner)
        with closing(self.connect()) as conn:
            return conn.execute(sql, params).rowcount > 0

    def claim(self, task_id, owner):
        with closing(self.connect()) as conn:
            return conn.execute("UPDATE tasks SET execution_id=?,status='running' WHERE id=? AND status='pending'",
                                (owner, task_id)).rowcount > 0

    def create_upload(self):
        result = {"upload_id": uuid.uuid4().hex, "status": "open", "task_id": None}
        with closing(self.connect()) as conn:
            conn.execute("INSERT INTO upload_sessions VALUES (?, 'open', NULL, ?)",
                         (result["upload_id"], now()))
        return result

    def get_upload(self, upload_id):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT upload_id,status,task_id FROM upload_sessions WHERE upload_id=?",
                               (upload_id,)).fetchone()
        return dict(row) if row else None

    def cancel_upload(self, upload_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM upload_sessions WHERE upload_id=?", (upload_id,)).fetchone()
            if not row:
                raise AdmissionError("upload_not_found")
            if row["status"] == "accepted":
                raise AdmissionError("upload_already_accepted", row["task_id"])
            conn.execute("UPDATE upload_sessions SET status='cancelled' WHERE upload_id=?", (upload_id,))
        return {"upload_id": upload_id, "status": "cancelled", "task_id": None}

    def accept_task(self, task, upload_id=None):
        with self.transaction() as conn:
            if upload_id:
                row = conn.execute("SELECT * FROM upload_sessions WHERE upload_id=?", (upload_id,)).fetchone()
                if not row:
                    raise AdmissionError("upload_not_found")
                if row["status"] != "open":
                    raise AdmissionError("upload_already_accepted" if row["status"] == "accepted"
                                         else "upload_cancelled", row["task_id"])
            fields = {"status": "pending", "created_at": now(), **task}
            conn.execute("INSERT INTO tasks (" + ",".join(fields) + ") VALUES (" +
                         ",".join("?" for _ in fields) + ")", list(fields.values()))
            if upload_id:
                conn.execute("UPDATE upload_sessions SET status='accepted',task_id=? WHERE upload_id=?",
                             (task["id"], upload_id))

    def queue_ai(self, task_id, fmts, *, owner=None):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise AdmissionError("task_not_found")
            if row["status"] != "done":
                raise AdmissionError("task_busy" if row["status"] in ("pending", "running") else "task_not_done")
            meta = task_dict(row)["meta"]
            meta["ai_feedback"] = True
            meta.pop("ai_error", None)
            errors = meta.get("ai_errors") or {}
            attempts = meta.setdefault("ai_attempts", {})
            for fmt in fmts:
                attempts[fmt] = {"status": "queued", "queued_at": now()}
                errors.pop(fmt, None)
            if errors:
                meta["ai_errors"] = errors
            else:
                meta.pop("ai_errors", None)
            conn.execute("""UPDATE tasks SET status='pending', stage='ai', progress=0,
                         message='等待 AI 处理', stage_started_at=?, meta_json=?, execution_id=? WHERE id=?""",
                         (now(), json.dumps(meta, ensure_ascii=False), owner or "", task_id))

    def delete_terminal(self, task_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            if row["status"] in ("pending", "running"):
                raise AdmissionError("task_busy")
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return task_dict(row)

    def recover_interrupted(self):
        with self.transaction() as conn:
            for row in conn.execute("SELECT * FROM tasks WHERE status IN ('pending','running')").fetchall():
                if row["stage"] == "ai":
                    meta = task_dict(row)["meta"]
                    for attempt in meta.get("ai_attempts", {}).values():
                        if attempt.get("status") in ("queued", "running"):
                            attempt.update(status="failed", error="服务重启，AI 生成中断", ended_at=now())
                    meta["ai_error"] = "服务重启，AI 生成中断；可查看上次版本"
                    conn.execute("UPDATE tasks SET status='done',progress=1,message=?,meta_json=?,execution_id='' WHERE id=?",
                                 ("AI 生成中断", json.dumps(meta, ensure_ascii=False), row["id"]))
                else:
                    conn.execute("UPDATE tasks SET status='failed',error=?,ended_at=?,execution_id='' WHERE id=?",
                                 ("服务重启，转写中断", now(), row["id"]))
