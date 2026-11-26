"""模拟财务服务的 SQLite 表结构。

资金模型（只追加台账 + 请求状态机）
==================================
* ``enterprise_accounts`` 管理各企业的初始额度（整数分）。同一企业的
  不同工单共用同一账户额度。
* ``fund_requests`` 按取件请求号（request_id）记录每一笔资金处理，
  request_id 是全局幂等键：
  - held     额度已锁定（待清算）：其他取件不能再动用这部分额度；
  - settled  确已扣费：hold 转 settle，留下恰好一次净扣费；
  - voided   未产生净费用：hold 已释放，额度恢复可用；
  - pending  已受理但结果未定（本服务同步处理，正常不会落此状态，
             保留给"受理与落库之间"的崩溃窗口与外部观测口径）。
* ``fund_ledger`` 是只追加的资金台账：hold / settle / release 各一条，
  任何流程都不更新或删除既有台账行，因此净扣费可直接由台账汇总复核。
"""

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 企业账户与初始共享额度（单位：整数分）
CREATE TABLE IF NOT EXISTS enterprise_accounts (
    account_id   TEXT PRIMARY KEY,
    name         TEXT,
    credit_limit INTEGER NOT NULL CHECK (credit_limit >= 0),
    created_at   TEXT NOT NULL
);

-- 取件请求的资金处理记录（request_id 为印制侧取件请求号，全局幂等）
CREATE TABLE IF NOT EXISTS fund_requests (
    request_id   TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES enterprise_accounts(account_id),
    amount       INTEGER NOT NULL CHECK (amount >= 0),
    -- pending 待处理 / held 待清算（已锁额）/
    -- settled 确已扣费 / voided 未产生净费用
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','held','settled','voided')),
    reference    TEXT,                       -- 溯源：工单号/份号等
    note         TEXT,
    held_at      TEXT,
    settled_at   TEXT,
    voided_at    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 只追加资金台账（证据账；永不 UPDATE/DELETE）
CREATE TABLE IF NOT EXISTS fund_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES fund_requests(request_id),
    account_id  TEXT NOT NULL REFERENCES enterprise_accounts(account_id),
    -- hold 锁定额度 / settle 确认扣费 / release 释放锁定
    kind        TEXT NOT NULL CHECK (kind IN ('hold','settle','release')),
    amount      INTEGER NOT NULL CHECK (amount >= 0),
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_request
    ON fund_ledger(request_id);
CREATE INDEX IF NOT EXISTS idx_ledger_account
    ON fund_ledger(account_id);
CREATE INDEX IF NOT EXISTS idx_fund_requests_account
    ON fund_requests(account_id);

-- 全量事件日志（证据链）
CREATE TABLE IF NOT EXISTS event_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""
