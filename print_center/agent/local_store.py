"""打印代理的本地介质（服务端不可读取）。

代理在自己的 SQLite 里持久化：
* 见过/执行过哪些派工——用于区分"已经执行过的派工"和"第一次见到的派工"；
* 实际产出的纸张身份与回执幂等键——崩溃恢复后凭同一 receipt_id 重发，
  绝不会因重连而重复出纸。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 代理本地的派工认知（与服务端库物理隔离）
CREATE TABLE IF NOT EXISTS local_dispatches (
    dispatch_id   TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    attempt_no    INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    -- seen 已领取但未打印 / executed 已实际产出纸张
    status        TEXT NOT NULL CHECK (status IN ('seen','executed')),
    paper_id      TEXT,
    receipt_id    TEXT,
    produced_at   TEXT,
    receipt_acked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS local_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
"""


def open_local(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
