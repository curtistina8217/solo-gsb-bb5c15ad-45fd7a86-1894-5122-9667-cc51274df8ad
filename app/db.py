"""SQLite 持久化（标准库 sqlite3，WAL 模式）。"""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建库建表（幂等），容器启动时调用。"""
    with _lock:
        global _conn
        if _conn is None:
            _conn = _connect()
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                status       TEXT NOT NULL DEFAULT 'pending',
                request_json TEXT NOT NULL,
                result_json  TEXT,
                error        TEXT,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            """
        )
        _conn.commit()


def _db() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


def create_task(payload: dict) -> int:
    now = time.time()
    with _lock:
        cur = _db().execute(
            "INSERT INTO tasks (status, request_json, created_at, updated_at)"
            " VALUES ('pending', ?, ?, ?)",
            (json.dumps(payload, ensure_ascii=False), now, now),
        )
        _db().commit()
        return int(cur.lastrowid)


def get_task(task_id: int) -> dict | None:
    with _lock:
        row = _db().execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def mark_running(task_id: int) -> None:
    with _lock:
        _db().execute(
            "UPDATE tasks SET status='running', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        _db().commit()


def finish_task(task_id: int, result: dict) -> None:
    with _lock:
        _db().execute(
            "UPDATE tasks SET status='succeeded', result_json=?, updated_at=?"
            " WHERE id=?",
            (json.dumps(result, ensure_ascii=False), time.time(), task_id),
        )
        _db().commit()


def fail_task(task_id: int, error: str) -> None:
    with _lock:
        _db().execute(
            "UPDATE tasks SET status='failed', error=?, updated_at=? WHERE id=?",
            (error, time.time(), task_id),
        )
        _db().commit()


def pending_task_ids() -> list[int]:
    """启动时恢复未完成任务（pending/running）。"""
    with _lock:
        rows = _db().execute(
            "SELECT id FROM tasks WHERE status IN ('pending','running')"
            " ORDER BY id"
        ).fetchall()
    return [r["id"] for r in rows]
