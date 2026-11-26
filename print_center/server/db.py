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
    finally:
        conn.close()


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
