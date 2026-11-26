"""模拟财务服务的连接管理与串行写事务。

与印制服务的 db 模块物理隔离：独立连接、独立数据库文件，互不读取
对方的库。写操作同样走 BEGIN IMMEDIATE + 进程内锁，保证额度检查与
台账写入在同一事务内原子生效。
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
        str(db_path), detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False, timeout=30)
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
    """串行化写事务：BEGIN IMMEDIATE + 全局锁。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        _write_lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            _write_lock.release()


def log_event(conn: sqlite3.Connection, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO event_log(ts,kind,payload) VALUES (?,?,?)",
        (utcnow(), kind,
         json.dumps(payload, ensure_ascii=False, sort_keys=True)))
