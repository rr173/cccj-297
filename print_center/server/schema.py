"""SQLite 表结构。

设计要点
========
* 服务端对物理世界只有两类信息来源：代理 ``receipts``（产出回执）和
  现场人员 ``verifications``（核对结果）。代理本地介质不在此库中。
* ``page_slots`` 是每个（份，页）槽位的"当前认知"状态机；
  ``dispatches`` 记录每一次派工（原派工 + 补派工）。
* ``papers`` 是服务端知道的纸张身份（由回执或现场核对引入），
  同一张纸全局唯一，且每个槽位最终至多保留一张有效纸。
* 交付后 ``delivery_items`` 只追加、不回改；之后出现的矛盾写入
  ``investigations``。
"""

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 代理登记（服务端只知道代理身份，不知道它的本地介质内容）
CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    registered_at TEXT NOT NULL,
    last_seen_at  TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

-- 工单
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    page_count    INTEGER NOT NULL CHECK (page_count > 0),
    copy_count    INTEGER NOT NULL CHECK (copy_count > 0),
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','delivered','investigating','closed'))
);

-- 工单的份（份号从 1 开始，保留到交付清单）
CREATE TABLE IF NOT EXISTS job_copies (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id),
    copy_no       INTEGER NOT NULL CHECK (copy_no >= 1),
    status        TEXT NOT NULL DEFAULT 'pending'
                  -- pending 待处理 / ready 可交付 / delivered 已交付 /
                  -- paused 因矛盾暂停 / investigating 交付后追查
                  CHECK (status IN ('pending','ready','delivered',
                                    'paused','investigating')),
    paused_reason TEXT,
    PRIMARY KEY (job_id, copy_no)
);

-- 每个（份，页）槽位：同一份的同一页只能有一张可交付纸
CREATE TABLE IF NOT EXISTS page_slots (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id),
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    -- pending        尚未有结论
    -- candidate      存在候选成品（回执或停用见证），待现场核对
    -- verified       现场确认唯一保留页有效，可用于交付
    -- no_paper       现场确认未出纸（需停用前置），等待/已补打
    -- waste          现场确认已出纸但为废页，等待/已补打
    -- conflict       晚回执与现场结论矛盾，需现场选定唯一保留页
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','candidate','verified',
                                    'no_paper','waste','conflict')),
    retained_paper_id TEXT REFERENCES papers(paper_id),
    conflict_note TEXT,
    PRIMARY KEY (job_id, copy_no, page_no)
);

-- 派工：每次把一个槽位派给某个代理打印，补打会产生新派工
CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id   TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    attempt_no    INTEGER NOT NULL,              -- 1=原派工，2+=补派
    agent_id      TEXT REFERENCES agents(agent_id),
    -- queued 待领取 / claimed 已领取（结果未知）/
    -- receipted 已收回执 / deactivated 代理已停用该派工
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','claimed','receipted',
                                    'deactivated')),
    created_at    TEXT NOT NULL,
    claimed_at    TEXT,
    receipted_at  TEXT,
    deactivated_at TEXT,
    UNIQUE (job_id, copy_no, page_no, attempt_no)
);

-- 服务端知道的纸张（身份唯一）。同一张纸只能属于一个槽位
CREATE TABLE IF NOT EXISTS papers (
    paper_id      TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    dispatch_id   TEXT NOT NULL REFERENCES dispatches(dispatch_id),
    source        TEXT NOT NULL CHECK (source IN ('receipt','onsite','attest')),
    -- candidate 候选 / verified 现场确认保留 / waste 废页 /
    -- isolated 矛盾隔离 / attest 停用见证（仍待现场核对）
    state         TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (state IN ('candidate','verified','waste',
                                   'isolated','attest')),
    created_at    TEXT NOT NULL,
    UNIQUE (job_id, copy_no, page_no, paper_id)
);

-- 回执（只追加、去重；可能重复、延迟、乱序到达）
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id    TEXT PRIMARY KEY,              -- 代理侧幂等键
    dispatch_id   TEXT NOT NULL REFERENCES dispatches(dispatch_id),
    agent_id      TEXT NOT NULL,
    paper_id      TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    is_duplicate  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (dispatch_id, paper_id)
);

-- 现场核对动作（只追加，全部保留作为证据）
CREATE TABLE IF NOT EXISTS verifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    action        TEXT NOT NULL
                  CHECK (action IN ('no_paper','retrieve','waste',
                                    'resolve_conflict')),
    paper_id      TEXT,
    dispatch_id   TEXT,
    agent_id      TEXT,
    note          TEXT,
    created_at    TEXT NOT NULL
);

-- 停用确认：代理声明"已不会再执行该派工"。
-- 它不是出纸证据；若代理本地记录显示已执行，evidence_paper_id
-- 只能作为待核对的见证（attest），仍须现场检查。
CREATE TABLE IF NOT EXISTS deactivations (
    dispatch_id   TEXT PRIMARY KEY REFERENCES dispatches(dispatch_id),
    agent_id      TEXT NOT NULL,
    executed_local INTEGER NOT NULL,             -- 代理本地是否记录已执行
    evidence_paper_id TEXT,
    confirmed_at  TEXT NOT NULL
);

-- 交付清单（只追加；交付后不回改）
CREATE TABLE IF NOT EXISTS delivery_items (
    job_id        TEXT NOT NULL,
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    paper_id      TEXT NOT NULL,
    dispatch_id   TEXT NOT NULL,
    delivered_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, copy_no, page_no)
);

-- 交付后出现的矛盾：只立追查，不改交付清单
CREATE TABLE IF NOT EXISTS investigations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    detail        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0
);

-- 全量事件日志（证据链）
CREATE TABLE IF NOT EXISTS event_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    payload       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_slot
    ON dispatches(job_id, copy_no, page_no);
CREATE INDEX IF NOT EXISTS idx_papers_slot
    ON papers(job_id, copy_no, page_no);
CREATE INDEX IF NOT EXISTS idx_slots_status
    ON page_slots(job_id, copy_no, status);
"""
