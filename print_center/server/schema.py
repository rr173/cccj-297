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

纸料承诺扩展
============
* ``stock_arrivals`` 记录库管按批次登记的到货张数（只追加，同批可再入库）；
  ``withdrawals`` 记录每次领活时从对应批次发出的一张纸（只追加）。
  库内可用 = 到货合计 − 已发出合计；核对/判废/停用/回执都不写这两张表，
  因此库存不会被恢复也不会被重复扣减。
* ``job_batch_policies`` 是工单允许使用的批次；没有该表记录的工单走旧流程，
  不参与纸料账。
* ``copy_commitments`` 是份级承诺：planned（未开印，可换批/撤销）→
  locked（首次领活时锁定批次）或 cancelled（撤销，不再派工/交付）。
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
                  CHECK (status IN ('open','delivered','investigating','closed')),
    -- 企业授信取件：两列同时为空 = 不使用授信，走原交付流程；
    -- 同时非空 = 每份成品从该企业共享额度按 price_per_copy（整数分）结算。
    credit_account_id TEXT,
    price_per_copy    INTEGER CHECK (price_per_copy IS NULL
                                     OR price_per_copy >= 0)
);

-- 工单的份（份号从 1 开始，保留到交付清单）
CREATE TABLE IF NOT EXISTS job_copies (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id),
    copy_no       INTEGER NOT NULL CHECK (copy_no >= 1),
    status        TEXT NOT NULL DEFAULT 'pending'
                  -- pending 待处理 / ready 可交付 / delivered 已交付 /
                  -- paused 因矛盾暂停 / investigating 交付后追查 /
                  -- cancelled 已撤销（不再派工或交付）
                  CHECK (status IN ('pending','ready','delivered',
                                    'paused','investigating','cancelled')),
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
    -- receipted 已收回执 / deactivated 代理已停用该派工 /
    -- cancelled 所属份已撤销，永不再派
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','claimed','receipted',
                                    'deactivated','cancelled')),
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

-- 批次到货登记（只追加；同一批可多次入库，未到货的批次不在表中=零库存）
CREATE TABLE IF NOT EXISTS stock_arrivals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    sheets        INTEGER NOT NULL CHECK (sheets > 0),
    note          TEXT,
    created_at    TEXT NOT NULL
);

-- 工单允许使用的批次编号（存在记录 = 该工单参与纸料账）
CREATE TABLE IF NOT EXISTS job_batch_policies (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id),
    batch_id      TEXT NOT NULL,
    PRIMARY KEY (job_id, batch_id)
);

-- 份级纸料承诺：每份当前计划/锁定使用的批次
CREATE TABLE IF NOT EXISTS copy_commitments (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id),
    copy_no       INTEGER NOT NULL,
    -- planned 未开印（可换批/撤销）/ locked 首次领活后锁定 /
    -- cancelled 已撤销
    status        TEXT NOT NULL DEFAULT 'planned'
                  CHECK (status IN ('planned','locked','cancelled')),
    batch_id      TEXT NOT NULL,
    locked_at     TEXT,
    cancelled_at  TEXT,
    PRIMARY KEY (job_id, copy_no)
);

-- 领用记录：每次领到派工时从对应批次发出一张纸。
-- 只追加、永不回改；与派工状态变更在同一事务内生效。
CREATE TABLE IF NOT EXISTS withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    dispatch_id   TEXT NOT NULL UNIQUE REFERENCES dispatches(dispatch_id),
    batch_id      TEXT NOT NULL,
    job_id        TEXT NOT NULL,
    copy_no       INTEGER NOT NULL,
    page_no       INTEGER NOT NULL,
    attempt_no    INTEGER NOT NULL,
    agent_id      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- 企业授信取件请求（柜员提交：工单号 + 份号 + 取件请求号）。
-- request_id 全局唯一幂等键；同一份同一时刻至多一个未终结请求，
-- 旧请求终结（rejected/released/delivered）后才允许换新号重新发起，
-- 因此"同一份换号"不能形成第二次交付或第二次扣费。
CREATE TABLE IF NOT EXISTS delivery_requests (
    request_id      TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id),
    copy_no         INTEGER NOT NULL,
    account_id      TEXT NOT NULL,
    amount          INTEGER NOT NULL CHECK (amount >= 0),
    -- pending   已登记，资金结果未定（财务无响应/处理中断）；
    -- held      财务已锁定额度，待清算（其他取件不能再动用这笔钱）；
    -- delivered 交付成立且财务已确认扣费（终态）；
    -- rejected  额度不足或请求冲突，未交付（终态，未产生净费用）；
    -- released  原交付条件失效，锁定已释放、未交付（终态，未产生净费用）
    status          TEXT NOT NULL
                    CHECK (status IN ('pending','held','delivered',
                                      'rejected','released')),
    -- 已判定该请求不应交付、但财务释放尚未确认时置位（仍显示待处理）
    release_pending INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    delivered_at    TEXT
);

-- 同一份至多一个"未终结"的取件请求（终态后允许换新号重新发起）
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_requests_active_copy
    ON delivery_requests(job_id, copy_no)
    WHERE status IN ('pending','held');

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
CREATE INDEX IF NOT EXISTS idx_withdrawals_batch
    ON withdrawals(batch_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_slot
    ON withdrawals(job_id, copy_no, page_no);
"""
