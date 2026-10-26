from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, project_root TEXT NOT NULL, target_spec TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, current_index INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                task_id TEXT NOT NULL, task_index INTEGER NOT NULL, task_json TEXT NOT NULL,
                status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0,
                report_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, task_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT, event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
            """)

    def create_run(self, run_id: str, project_root: str, target_spec: str, mode: str, metadata: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs(run_id,project_root,target_spec,mode,status,current_index,created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, project_root, target_spec, mode, "running", 0, utcnow(), utcnow(), json.dumps(metadata)),
            )

    def save_tasks(self, run_id: str, tasks: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            for index, task in enumerate(tasks):
                task_id = str(task["id"])
                status = str(task.get("status", "pending"))
                conn.execute(
                    "INSERT INTO tasks(run_id,task_id,task_index,task_json,status,retry_count,report_json,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(run_id,task_id) DO UPDATE SET task_index=excluded.task_index,task_json=excluded.task_json,status=excluded.status,retry_count=excluded.retry_count,report_json=excluded.report_json,updated_at=excluded.updated_at",
                    (run_id, task_id, index, json.dumps(task), status, int(task.get("retry_count", 0)), json.dumps(task.get("last_report", {})), utcnow()),
                )

    def update_task(self, run_id: str, task: dict[str, Any], index: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET task_index=?,task_json=?,status=?,retry_count=?,report_json=?,updated_at=? WHERE run_id=? AND task_id=?",
                (index, json.dumps(task), task.get("status", "pending"), int(task.get("retry_count", 0)), json.dumps(task.get("last_report", {})), utcnow(), run_id, task["id"]),
            )
            conn.execute("UPDATE runs SET current_index=?,updated_at=? WHERE run_id=?", (index, utcnow(), run_id))

    def update_run(self, run_id: str, status: str, current_index: int | None = None, metadata: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            if current_index is None and metadata is None:
                conn.execute("UPDATE runs SET status=?,updated_at=? WHERE run_id=?", (status, utcnow(), run_id))
            elif current_index is None:
                conn.execute("UPDATE runs SET status=?,metadata_json=?,updated_at=? WHERE run_id=?", (status, json.dumps(metadata), utcnow(), run_id))
            elif metadata is None:
                conn.execute("UPDATE runs SET status=?,current_index=?,updated_at=? WHERE run_id=?", (status, current_index, utcnow(), run_id))
            else:
                conn.execute("UPDATE runs SET status=?,current_index=?,metadata_json=?,updated_at=? WHERE run_id=?", (status, current_index, json.dumps(metadata), utcnow(), run_id))

    def event(self, run_id: str | None, event_type: str, payload: dict[str, Any], task_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(run_id,task_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, task_id, event_type, json.dumps(payload, ensure_ascii=False, default=str), utcnow()),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
            task_rows = conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY task_index", (run_id,)).fetchall()
            result["tasks"] = []
            for task_row in task_rows:
                task = json.loads(task_row["task_json"])
                task["status"] = task_row["status"]
                task["retry_count"] = task_row["retry_count"]
                task["last_report"] = json.loads(task_row["report_json"] or "{}")
                result["tasks"].append(task)
            return result

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT run_id,project_root,mode,status,current_index,created_at,updated_at FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def recent_events(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            result = []
            for row in reversed(rows):
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result
