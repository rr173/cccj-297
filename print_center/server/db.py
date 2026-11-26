"""SQLite 连接管理与审计日志。

演示环境用单进程 uvicorn + 写锁串行化所有写事务，保证状态机检查与
更新在同一事务内完成（SQLite 默认隔离级别下显式 BEGIN IMMEDIATE）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .schema import SCHEMA_SQL

_write_lock = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """对早期演示库做无损补列/证据回填。新库的 SCHEMA_SQL 已含最终结构。"""
    jc_cols = _table_columns(conn, "job_copies")
    if "copy_kind" not in jc_cols:
        conn.execute(
            "ALTER TABLE job_copies ADD COLUMN copy_kind TEXT NOT NULL "
            "DEFAULT 'original'")
    di_cols = _table_columns(conn, "delivery_items")
    if "request_id" not in di_cols:
        conn.execute("ALTER TABLE delivery_items ADD COLUMN request_id TEXT")
    if "exchange_id" not in di_cols:
        conn.execute("ALTER TABLE delivery_items ADD COLUMN exchange_id TEXT")

    # 旧库没有 custody：已冻结交付清单的份视为原交付份仍在手。
    conn.execute(
        "INSERT OR IGNORE INTO copy_custody("
        "physical_job_id, physical_copy_no, logical_job_id, "
        "logical_copy_no, status, delivered_at) "
        "SELECT job_id, copy_no, job_id, copy_no, 'with_customer', "
        "MIN(delivered_at) FROM delivery_items "
        "GROUP BY job_id, copy_no")
    conn.commit()


class WriteTx:
    """串行化的写事务上下文：BEGIN IMMEDIATE + 全局锁。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._committed = False

    def __enter__(self) -> sqlite3.Connection:
        _write_lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.conn.commit()
                self._committed = True
            else:
                self.conn.rollback()
        finally:
            _write_lock.release()


def log_event(conn: sqlite3.Connection, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO event_log(ts, kind, payload) VALUES (?,?,?)",
        (utcnow(), kind, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
