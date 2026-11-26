"""领域状态机：服务端所有业务规则集中在此。

不变量（贯穿全部操作）
======================
1. 服务端只通过 ``receipts`` 和 ``verifications`` 获知物理世界；
   claimed 而无回执/核对结论的派工永远是"未知"，既不算失败也不算成功。
2. 停用确认（deactivations）只表达"代理不会再执行该派工"，
   不表达是否出过纸；其见证纸一律进入 attest 状态等待现场核对。
3. 补打前置：原派工 deactivated 且槽位经现场判定为 no_paper / waste。
   失联期间没有停用确认，超时不会派新活。
4. 每张纸全局唯一；一个槽位最终至多一张 verified 保留页。
5. 废页（waste）不可被晚回执复活；已交付清单不被任何后续事件回改，
   矛盾只立 investigations。

纸料承诺（指定批次的工单才参与，旧流程工单不建账）
==================================================
6. 库内可用 = 批次到货合计 − 已发出合计；只有"登记到货"和"领活发纸"
   会改变库存。核对未出纸、判废、停用、重收/晚收回执都不恢复库存，
   也不会再次扣减。
7. 接单承诺：指定批次的工单只有在"每份的全部原印页都能落实到同一允许
   批次、且不挤掉已接工单的原印用纸"时才受理，否则整单不建并说明缺料。
   未开印（planned）的份可被重排批次；只要存在能兑现全部承诺的安排，
   就不得因此前的分配而拒单。
8. 代理第一次领到某份的页任务时才对外锁定该份批次；此后该份剩余原印
   与补打都只能用这一批，哪怕首张没印出来。未开印的份可换批或整份
   撤销，撤销后不再派工或交付。
9. 每次领到派工就从对应批次发出一张纸，领用与派工在同一事务内生效；
   补打还须满足同批且不吃掉任何已承诺的原印用纸，缺料的补打继续等待，
   不挡住其他可领任务，同批补入足够纸后可以继续。
10. 指定批次工单的成品只能凭"已有领用记录的派工"来认定（回执与现场
    找回都校验领用记录）。
"""
from __future__ import annotations

import json
import re
import uuid
from collections import deque
from datetime import datetime, timezone

from .db import WriteTx, log_event, row_to_dict, utcnow
from . import finance_client


class DomainError(Exception):
    """4xx 业务错误，携带稳定错误码。"""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _coerce_copy_no(value) -> int:
    """接受 3、"3"、"copy-3" 等常见份号表达。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return int(value)
    text = value.strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"(\d+)\s*$", text)
    if match:
        return int(match.group(1))
    raise ValueError(f"invalid copy number: {value!r}")


def _get(conn, sql: str, params=()):
    return conn.execute(sql, params).fetchone()


def _all(conn, sql: str, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _slot(conn, job_id: str, copy_no: int, page_no: int):
    row = _get(
        conn,
        "SELECT * FROM page_slots WHERE job_id=? AND copy_no=? AND page_no=?",
        (job_id, copy_no, page_no),
    )
    if row is None:
        raise DomainError("slot_not_found",
                          f"槽位 {job_id}/{copy_no}/{page_no} 不存在", 404)
    return dict(row)


def _dispatch(conn, dispatch_id: str):
    row = _get(conn, "SELECT * FROM dispatches WHERE dispatch_id=?",
               (dispatch_id,))
    if row is None:
        raise DomainError("dispatch_not_found",
                          f"派工 {dispatch_id} 不存在", 404)
    return dict(row)


def _paper(conn, paper_id: str):
    return row_to_dict(_get(
        conn, "SELECT * FROM papers WHERE paper_id=?", (paper_id,)))


def _latest_dispatch(conn, job_id: str, copy_no: int, page_no: int):
    return row_to_dict(_get(
        conn,
        "SELECT * FROM dispatches WHERE job_id=? AND copy_no=? AND page_no=? "
        "ORDER BY attempt_no DESC LIMIT 1",
        (job_id, copy_no, page_no),
    ))


def _refresh_copy_status(conn, job_id: str, copy_no: int) -> str:
    """根据全部槽位状态重算份状态；返回新状态。"""
    copy = dict(_get(conn,
                     "SELECT * FROM job_copies WHERE job_id=? AND copy_no=?",
                     (job_id, copy_no)))
    if copy["status"] in ("delivered", "investigating", "cancelled"):
        return copy["status"]  # 已交付/追查中/已撤销不回改

    slots = _all(
        conn,
        "SELECT status FROM page_slots WHERE job_id=? AND copy_no=?",
        (job_id, copy_no),
    )
    unresolved_conflict = any(s["status"] == "conflict" for s in slots)
    all_verified = all(s["status"] == "verified" for s in slots)

    if unresolved_conflict:
        new_status, reason = "paused", "存在矛盾槽位，等待现场选定唯一保留页"
    elif all_verified:
        new_status, reason = "ready", None
    else:
        new_status, reason = "pending", None

    conn.execute(
        "UPDATE job_copies SET status=?, paused_reason=? "
        "WHERE job_id=? AND copy_no=?",
        (new_status, reason, job_id, copy_no),
    )
    return new_status


def _pause_copy(conn, job_id: str, copy_no: int, reason: str) -> None:
    conn.execute(
        "UPDATE job_copies SET status='paused', paused_reason=? "
        "WHERE job_id=? AND copy_no=? AND status NOT IN ('delivered',"
        "'investigating','cancelled')",
        (reason, job_id, copy_no),
    )


def _open_investigation(conn, job_id: str, copy_no: int, page_no: int,
                        reason: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO investigations(job_id,copy_no,page_no,reason,detail,"
        "created_at) VALUES (?,?,?,?,?,?)",
        (job_id, copy_no, page_no, reason, detail, utcnow()),
    )
    conn.execute(
        "UPDATE job_copies SET status='investigating' "
        "WHERE job_id=? AND copy_no=? AND status='delivered'",
        (job_id, copy_no),
    )
    log_event(conn, "investigation_opened", {
        "job_id": job_id, "copy_no": copy_no, "page_no": page_no,
        "reason": reason, "detail": detail,
    })


def _record_verification(conn, job_id, copy_no, page_no, action,
                         paper_id=None, dispatch_id=None, agent_id=None,
                         note=None) -> None:
    conn.execute(
        "INSERT INTO verifications(job_id,copy_no,page_no,action,paper_id,"
        "dispatch_id,agent_id,note,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, copy_no, page_no, action, paper_id, dispatch_id, agent_id,
         note, utcnow()),
    )


# ---------------------------------------------------------------------------
# 纸料账：批次库存、承诺需求、可行性求解
# ---------------------------------------------------------------------------

def _is_tracked(c, job_id: str) -> bool:
    """工单是否指定了批次（参与纸料账）。"""
    return _get(c, "SELECT 1 FROM job_batch_policies WHERE job_id=? LIMIT 1",
                (job_id,)) is not None


def _allowed_batches(c, job_id: str, copy_no: int | None = None) -> list[str]:
    """工单允许批次。

    指定 copy_no 且该份有单独指定（备品创建时常用）时，以份级列表为准；
    否则回退到工单级 ``job_batch_policies``。
    """
    if copy_no is not None:
        rows = c.execute(
            "SELECT batch_id FROM copy_allowed_batches WHERE job_id=? "
            "AND copy_no=? ORDER BY batch_id", (job_id, copy_no)).fetchall()
        if rows:
            return [r["batch_id"] for r in rows]
    return [r["batch_id"] for r in c.execute(
        "SELECT batch_id FROM job_batch_policies WHERE job_id=? "
        "ORDER BY batch_id", (job_id,)).fetchall()]


def _recalled_batch_ids(c) -> set[str]:
    return {r["batch_id"] for r in c.execute(
        "SELECT batch_id FROM paper_batch_recalls").fetchall()}


def _filter_assignable_batches(c, batches: list[str]) -> list[str]:
    recalled = _recalled_batch_ids(c)
    return [b for b in batches if b not in recalled]


def _arrived_by_batch(c) -> dict[str, int]:
    return {r["batch_id"]: r["n"] for r in c.execute(
        "SELECT batch_id, SUM(sheets) AS n FROM stock_arrivals "
        "GROUP BY batch_id").fetchall()}


def _withdrawn_by_batch(c) -> dict[str, int]:
    return {r["batch_id"]: r["n"] for r in c.execute(
        "SELECT batch_id, COUNT(*) AS n FROM withdrawals "
        "GROUP BY batch_id").fetchall()}


def _all_batch_ids(c) -> set[str]:
    ids: set[str] = set()
    for sql in ("SELECT DISTINCT batch_id FROM stock_arrivals",
                "SELECT DISTINCT batch_id FROM withdrawals",
                "SELECT DISTINCT batch_id FROM job_batch_policies",
                "SELECT DISTINCT batch_id FROM copy_commitments"):
        ids |= {r[0] for r in c.execute(sql).fetchall()}
    return ids


def _available_by_batch(c) -> dict[str, int]:
    """各批库内可用 = 到货合计 − 已发出合计（未出现的批次为 0）。"""
    arrived = _arrived_by_batch(c)
    withdrawn = _withdrawn_by_batch(c)
    return {b: arrived.get(b, 0) - withdrawn.get(b, 0)
            for b in _all_batch_ids(c)}


def _demand_by_batch(c, statuses=("planned", "locked")) -> dict[str, int]:
    """各批已承诺的原印需求：承诺份上尚未领取的原派工张数。"""
    marks = ",".join(["?"] * len(statuses))
    return {r["b"]: r["n"] for r in c.execute(
        f"SELECT cc.batch_id AS b, COUNT(*) AS n "
        f"FROM copy_commitments cc JOIN dispatches d "
        f"ON d.job_id=cc.job_id AND d.copy_no=cc.copy_no "
        f"AND d.attempt_no=1 AND d.status='queued' "
        f"WHERE cc.status IN ({marks}) GROUP BY cc.batch_id",
        tuple(statuses)).fetchall()}


def _planned_items(c) -> list[dict]:
    """未开印（可重排）的份：键、剩余原印需求、允许批次。"""
    rows = _all(
        c, "SELECT cc.job_id, cc.copy_no, "
           "(SELECT COUNT(*) FROM dispatches d "
           " WHERE d.job_id=cc.job_id AND d.copy_no=cc.copy_no "
           " AND d.attempt_no=1 AND d.status='queued') AS demand "
           "FROM copy_commitments cc WHERE cc.status='planned' "
           "ORDER BY cc.job_id, cc.copy_no")
    return [{"key": (r["job_id"], r["copy_no"]), "demand": r["demand"],
             "allowed": _allowed_batches(c, r["job_id"], r["copy_no"])}
            for r in rows]


def _fractional_deficit(caps: dict[str, int], items: list[dict]) -> int:
    """分数松弛（允许把一份拆到多批）下的总缺口；0 表示松弛可行。

    用作回溯剪枝的必要条件与缺料说明的缺口估计。Edmonds-Karp 最大流。
    """
    total = sum(it["demand"] for it in items)
    if total == 0:
        return 0
    batches = sorted(caps)
    bidx = {b: i for i, b in enumerate(batches)}
    n, m = len(items), len(batches)
    src, snk = 0, n + m + 1
    graph: list[list[list[int]]] = [[] for _ in range(n + m + 2)]

    def add(u: int, v: int, cap: int) -> None:
        graph[u].append([v, cap, len(graph[v])])
        graph[v].append([u, 0, len(graph[u]) - 1])

    for i, it in enumerate(items):
        add(src, 1 + i, it["demand"])
        for b in it["allowed"]:
            j = bidx.get(b)
            if j is not None:
                add(1 + i, 1 + n + j, it["demand"])
    for j in range(m):
        add(1 + n + j, snk, max(0, caps[batches[j]]))

    flow = 0
    while True:
        prev: list[tuple[int, int] | None] = [None] * len(graph)
        prev[src] = (src, -1)
        queue = deque([src])
        while queue and prev[snk] is None:
            u = queue.popleft()
            for ei, edge in enumerate(graph[u]):
                if edge[1] > 0 and prev[edge[0]] is None:
                    prev[edge[0]] = (u, ei)
                    queue.append(edge[0])
        if prev[snk] is None:
            break
        bottleneck = None
        v = snk
        while v != src:
            u, ei = prev[v]
            cap = graph[u][ei][1]
            bottleneck = cap if bottleneck is None else min(bottleneck, cap)
            v = u
        v = snk
        while v != src:
            u, ei = prev[v]
            graph[u][ei][1] -= bottleneck
            graph[v][graph[u][ei][2]][1] += bottleneck
            v = u
        flow += bottleneck
    return total - flow


def _solve_assignment(available: dict[str, int], locked: dict[str, int],
                      planned: list[dict]) -> dict | None:
    """精确可行性：为每份未开印的份选定一个允许批次，使各批
    “已锁定需求 + 新分配需求 ≤ 库内可用”。返回 {key: batch_id} 或 None。

    每份是不可拆分项（同一份必须用同一批），该问题一般情形是 NP-hard；
    这里用回溯 + 分数松弛剪枝，单印制中心规模下足够快。只要存在可行
    安排就一定返回其中之一（不会因此前的分配而误拒）。
    """
    caps = {b: available.get(b, 0) - locked.get(b, 0)
            for b in set(available) | set(locked)}
    if any(v < 0 for v in caps.values()):
        return None
    items = sorted((it for it in planned if it["demand"] > 0),
                   key=lambda it: (-it["demand"], len(it["allowed"]),
                                   it["key"]))
    if _fractional_deficit(caps, items) > 0:
        return None
    assignment: dict = {}

    def backtrack(i: int) -> bool:
        if i == len(items):
            return True
        it = items[i]
        for b in sorted(it["allowed"]):
            if caps.get(b, 0) >= it["demand"]:
                caps[b] = caps.get(b, 0) - it["demand"]
                assignment[it["key"]] = b
                if _fractional_deficit(caps, items[i + 1:]) == 0 \
                        and backtrack(i + 1):
                    return True
                del assignment[it["key"]]
                caps[b] += it["demand"]
        return False

    return assignment if backtrack(0) else None


def _copy_paper_usage(c, job_id: str, copy_no: int) -> list[dict]:
    """该物理份实际纸张 -> 派工 -> 领用批次的证据链。

    已交付份以只追加的 delivery_items 为准；未交付份以当前 verified
    retained_paper_id 为准。绝不使用工单允许批次或承诺/排产批次反推。
    """
    delivered = _get(
        c, "SELECT 1 FROM delivery_items WHERE job_id=? AND copy_no=? LIMIT 1",
        (job_id, copy_no))
    if delivered is not None:
        return _all(c, """
            SELECT di.page_no AS page_no, di.paper_id AS paper_id,
                   di.dispatch_id AS delivery_dispatch_id,
                   p.dispatch_id AS paper_dispatch_id,
                   COALESCE(wp.batch_id, wd.batch_id) AS batch_id,
                   COALESCE(wp.withdrawal_id, wd.withdrawal_id)
                       AS withdrawal_id
            FROM delivery_items di
            LEFT JOIN papers p ON p.paper_id = di.paper_id
            LEFT JOIN withdrawals wp ON wp.dispatch_id = p.dispatch_id
            LEFT JOIN withdrawals wd ON wd.dispatch_id = di.dispatch_id
            WHERE di.job_id=? AND di.copy_no=?
            ORDER BY di.page_no
        """, (job_id, copy_no))
    return _all(c, """
        SELECT s.page_no AS page_no,
               s.retained_paper_id AS paper_id,
               p.dispatch_id AS paper_dispatch_id,
               p.dispatch_id AS delivery_dispatch_id,
               w.batch_id AS batch_id,
               w.withdrawal_id AS withdrawal_id
        FROM page_slots s
        LEFT JOIN papers p ON p.paper_id = s.retained_paper_id
        LEFT JOIN withdrawals w ON w.dispatch_id = p.dispatch_id
        WHERE s.job_id=? AND s.copy_no=?
        ORDER BY s.page_no
    """, (job_id, copy_no))


def _recalled_usage(c, job_id: str, copy_no: int) -> list[dict]:
    """实际用纸中属于永久召回批次的记录。未追踪工单没有领用批次，返回空。"""
    if not _is_tracked(c, job_id):
        return []
    recalled = _recalled_batch_ids(c)
    if not recalled:
        return []
    return [u for u in _copy_paper_usage(c, job_id, copy_no)
            if u.get("batch_id") in recalled]


def _assert_copy_not_recalled(c, job_id: str, copy_no: int) -> list[dict]:
    bad = _recalled_usage(c, job_id, copy_no)
    if bad:
        batches = sorted({u["batch_id"] for u in bad})
        err = DomainError(
            "batch_recalled",
            f"工单 {job_id} 份 {copy_no} 的实际用纸来自已永久召回批次 "
            f"{batches}，不能交给客户或作为合格备品交出", 409)
        err.details = {"recalled_batches": batches, "papers": bad}
        raise err
    return bad


def _shortage_message(c, allowed: list[str], page_count: int,
                      copy_count: int) -> str:
    """拒单时的缺料说明：各批账面和缺口估计。"""
    arrived = _arrived_by_batch(c)
    withdrawn = _withdrawn_by_batch(c)
    committed = _demand_by_batch(c, ("planned", "locked"))
    locked = _demand_by_batch(c, ("locked",))
    parts = []
    for b in allowed:
        a, w = arrived.get(b, 0), withdrawn.get(b, 0)
        parts.append(f"批次 {b}：到货 {a}，已发出 {w}，库内可用 {a - w}，"
                     f"已承诺原印 {committed.get(b, 0)}")
    caps = {b: (arrived.get(b, 0) - withdrawn.get(b, 0) - locked.get(b, 0))
            for b in set(arrived) | set(withdrawn) | set(committed)
            | set(allowed)}
    new_items = [{"key": ("<new>", i), "demand": page_count,
                  "allowed": list(allowed)} for i in range(copy_count)]
    deficit = _fractional_deficit(caps, _planned_items(c) + new_items)
    if deficit > 0:
        tail = f"即使允许把一份拆开用多批，仍缺 {deficit} 张"
    else:
        tail = ("各批总量够用，但受“同一份必须用同一批”限制，"
                "现有批次分布容纳不下整份")
    return (f"纸料不足，整单未建：新单需求 {copy_count} 份 × {page_count} "
            f"页，允许批次 {allowed}；" + "；".join(parts) + f"。{tail}。")


# ---------------------------------------------------------------------------
# 工单与派工创建
# ---------------------------------------------------------------------------

def create_job(conn, job_id: str, title: str, page_count: int,
               copy_count: int, allowed_batches: list[str] | None = None,
               credit_account_id: str | None = None,
               price_per_copy: int | None = None) -> dict:
    if page_count <= 0 or copy_count <= 0:
        raise DomainError("invalid_job", "页数与份数必须为正整数", 400)
    # 授信参数必须成对出现；只给一个视为非法请求，不静默改走旧流程
    if (credit_account_id is None) != (price_per_copy is None):
        raise DomainError(
            "invalid_credit_job",
            "企业授信须同时给出 credit_account_id 与 price_per_copy"
            "（整数分）；两者都省略则走不使用授信的原流程", 400)
    if price_per_copy is not None and price_per_copy < 0:
        raise DomainError("invalid_credit_job",
                          "每份价格必须是非负整数分", 400)
    tracked = allowed_batches is not None
    if tracked:
        allowed_batches = sorted({b.strip() for b in allowed_batches
                                  if b and b.strip()})
        if not allowed_batches:
            raise DomainError("invalid_job",
                              "指定批次时至少给出一个批次编号", 400)
    rejection = None
    with WriteTx(conn) as c:
        if _get(c, "SELECT 1 FROM jobs WHERE job_id=?", (job_id,)):
            raise DomainError("job_exists", f"工单 {job_id} 已存在")
        now = utcnow()
        assignment: dict = {}
        if tracked:
            # 可行性 = 已锁定需求 + 未开印可重排的份 + 新单各份，
            # 存在能兑现全部承诺的安排才受理；否则整单不建。
            planned = _planned_items(c)
            new_items = [{"key": (job_id, no), "demand": page_count,
                          "allowed": list(allowed_batches)}
                         for no in range(1, copy_count + 1)]
            assignment = _solve_assignment(
                _available_by_batch(c), _demand_by_batch(c, ("locked",)),
                planned + new_items)
            if assignment is None:
                rejection = _shortage_message(c, allowed_batches,
                                              page_count, copy_count)
                log_event(c, "job_rejected_insufficient_stock", {
                    "job_id": job_id, "allowed_batches": allowed_batches,
                    "page_count": page_count, "copy_count": copy_count,
                    "reason": rejection})
            else:
                # 落实对既有未开印份的重排（不挤掉任何已接工单的原印用纸）
                for (j, no), b in assignment.items():
                    if j == job_id:
                        continue
                    cur = _get(c, "SELECT batch_id FROM copy_commitments "
                                  "WHERE job_id=? AND copy_no=?", (j, no))
                    if cur is not None and cur["batch_id"] != b:
                        c.execute(
                            "UPDATE copy_commitments SET batch_id=? "
                            "WHERE job_id=? AND copy_no=?", (b, j, no))
                        log_event(c, "copy_batch_reassigned", {
                            "job_id": j, "copy_no": no,
                            "from_batch": cur["batch_id"], "to_batch": b,
                            "reason": "accept_replan", "by_job": job_id})
        if rejection is None:
            c.execute(
                "INSERT INTO jobs(job_id,title,page_count,copy_count,"
                "created_at,credit_account_id,price_per_copy) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, title, page_count, copy_count, now,
                 credit_account_id, price_per_copy),
            )
            for copy_no in range(1, copy_count + 1):
                c.execute(
                    "INSERT INTO job_copies(job_id,copy_no,status) "
                    "VALUES (?,?,?)",
                    (job_id, copy_no, "pending"),
                )
                for page_no in range(1, page_count + 1):
                    c.execute(
                        "INSERT INTO page_slots(job_id,copy_no,page_no,"
                        "status) VALUES (?,?,?,'pending')",
                        (job_id, copy_no, page_no),
                    )
                    dispatch_id = _new_id("dsp")
                    c.execute(
                        "INSERT INTO dispatches(dispatch_id,job_id,copy_no,"
                        "page_no,attempt_no,status,created_at) "
                        "VALUES (?,?,?,?,1,'queued',?)",
                        (dispatch_id, job_id, copy_no, page_no, now),
                    )
                if tracked:
                    c.execute(
                        "INSERT INTO copy_commitments(job_id,copy_no,"
                        "status,batch_id) VALUES (?,?,'planned',?)",
                        (job_id, copy_no, assignment[(job_id, copy_no)]),
                    )
            if tracked:
                for b in allowed_batches:
                    c.execute(
                        "INSERT INTO job_batch_policies(job_id,batch_id) "
                        "VALUES (?,?)", (job_id, b))
            log_event(c, "job_created", {
                "job_id": job_id, "title": title,
                "page_count": page_count, "copy_count": copy_count,
                "allowed_batches": allowed_batches if tracked else None,
                "credit_account_id": credit_account_id,
                "price_per_copy": price_per_copy})
    if rejection is not None:
        raise DomainError("insufficient_stock", rejection, 409)
    return get_job(conn, job_id)


def get_job(conn, job_id: str) -> dict:
    job = row_to_dict(_get(conn, "SELECT * FROM jobs WHERE job_id=?",
                           (job_id,)))
    if job is None:
        raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
    allowed = _allowed_batches(conn, job_id)
    job["allowed_batches"] = allowed or None
    commitments = {r["copy_no"]: r for r in _all(
        conn, "SELECT * FROM copy_commitments WHERE job_id=?", (job_id,))}
    job["copies"] = _all(
        conn, "SELECT * FROM job_copies WHERE job_id=? ORDER BY copy_no",
        (job_id,))
    for cp in job["copies"]:
        cp["commitment"] = commitments.get(cp["copy_no"])
    job["dispatches"] = _all(
        conn, "SELECT * FROM dispatches WHERE job_id=? "
              "ORDER BY copy_no,page_no,attempt_no", (job_id,))
    return job


# ---------------------------------------------------------------------------
# 代理登记 / 心跳 / 领活
# ---------------------------------------------------------------------------

def register_agent(conn, agent_id: str) -> dict:
    with WriteTx(conn) as c:
        row = _get(c, "SELECT * FROM agents WHERE agent_id=?", (agent_id,))
        if row is None:
            c.execute(
                "INSERT INTO agents(agent_id,registered_at,last_seen_at,"
                "active) VALUES (?,?,?,1)",
                (agent_id, utcnow(), utcnow()),
            )
            log_event(c, "agent_registered", {"agent_id": agent_id})
        else:
            c.execute("UPDATE agents SET last_seen_at=?, active=1 "
                      "WHERE agent_id=?", (utcnow(), agent_id))
    return dict(_get(conn, "SELECT * FROM agents WHERE agent_id=?",
                     (agent_id,)))


def heartbeat(conn, agent_id: str) -> None:
    with WriteTx(conn) as c:
        if _get(c, "SELECT 1 FROM agents WHERE agent_id=?", (agent_id,)) \
                is None:
            raise DomainError("agent_not_found",
                              f"代理 {agent_id} 未登记", 404)
        c.execute("UPDATE agents SET last_seen_at=? WHERE agent_id=?",
                  (utcnow(), agent_id))


def _claim_decision(c, d: dict) -> tuple[str | None, str] | None:
    """该 queued 派工现在是否可领；可领时返回（批次或 None，类别）。

    返回 None 的情形：所属份已撤销（不再派工）；指定批次工单的补打
    缺料（继续等待，不挡住其他可领任务）。
    """
    if not _is_tracked(c, d["job_id"]):
        return (None, "legacy")
    cm = _get(c, "SELECT * FROM copy_commitments WHERE job_id=? AND "
                 "copy_no=?", (d["job_id"], d["copy_no"]))
    if cm is None or cm["status"] == "cancelled":
        return None
    batch_id = cm["batch_id"]
    if d["attempt_no"] == 1:
        # 原印：属已承诺用纸，接单/换批时已保证库存足够
        if _available_by_batch(c).get(batch_id, 0) < 1:
            log_event(c, "claim_blocked_invariant", {
                "dispatch_id": d["dispatch_id"], "batch_id": batch_id})
            return None
        return (batch_id, "original")
    # 补打：必须同批，且不能吃掉任何已承诺的原印用纸
    available = _available_by_batch(c).get(batch_id, 0)
    committed = _demand_by_batch(c, ("planned", "locked")).get(batch_id, 0)
    if available - committed < 1:
        return None
    return (batch_id, "reprint")


def claim_dispatch(conn, agent_id: str) -> dict | None:
    """领取一个当前可领的 queued 派工（FIFO 扫描，跳过不可领者）。

    关键：只有 queued 派工可被领取。claimed 派工在收到回执或停用确认
    之前不会再次入队，因此失联/超时不会产生重复派工。
    指定批次工单的派工：领活与从对应批次发出一张纸在同一事务内生效，
    首次领到某份的页任务时对外锁定该份批次。
    """
    with WriteTx(conn) as c:
        if _get(c, "SELECT 1 FROM agents WHERE agent_id=? AND active=1",
                (agent_id,)) is None:
            raise DomainError("agent_not_active",
                              f"代理 {agent_id} 未登记或已停用", 409)
        rows = _all(
            c, "SELECT * FROM dispatches WHERE status='queued' "
               "ORDER BY created_at, rowid")
        for d in rows:
            decision = _claim_decision(c, d)
            if decision is None:
                continue
            batch_id, kind = decision
            now = utcnow()
            c.execute(
                "UPDATE dispatches SET status='claimed', agent_id=?, "
                "claimed_at=? WHERE dispatch_id=? AND status='queued'",
                (agent_id, now, d["dispatch_id"]),
            )
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                raise DomainError("claim_lost", "派工已被其他代理领取", 409)
            withdrawal_id = None
            if batch_id is not None:
                if kind == "original":
                    # 首次领到该份页任务：对外锁定该份批次（哪怕首张
                    # 最终没印出来，锁定也不撤销）
                    c.execute(
                        "UPDATE copy_commitments SET status='locked', "
                        "locked_at=? WHERE job_id=? AND copy_no=? "
                        "AND status='planned'",
                        (now, d["job_id"], d["copy_no"]))
                    if c.execute("SELECT changes()").fetchone()[0] == 1:
                        log_event(c, "copy_batch_locked", {
                            "job_id": d["job_id"], "copy_no": d["copy_no"],
                            "batch_id": batch_id,
                            "dispatch_id": d["dispatch_id"]})
                withdrawal_id = _new_id("wd")
                c.execute(
                    "INSERT INTO withdrawals(withdrawal_id,dispatch_id,"
                    "batch_id,job_id,copy_no,page_no,attempt_no,agent_id,"
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (withdrawal_id, d["dispatch_id"], batch_id, d["job_id"],
                     d["copy_no"], d["page_no"], d["attempt_no"], agent_id,
                     now))
                log_event(c, "paper_withdrawn", {
                    "withdrawal_id": withdrawal_id,
                    "dispatch_id": d["dispatch_id"], "batch_id": batch_id,
                    "job_id": d["job_id"], "copy_no": d["copy_no"],
                    "page_no": d["page_no"], "attempt_no": d["attempt_no"],
                    "agent_id": agent_id, "kind": kind})
            c.execute("UPDATE agents SET last_seen_at=? WHERE agent_id=?",
                      (now, agent_id))
            log_event(c, "dispatch_claimed", {
                "dispatch_id": d["dispatch_id"], "agent_id": agent_id,
                "job_id": d["job_id"], "copy_no": d["copy_no"],
                "page_no": d["page_no"], "attempt_no": d["attempt_no"],
                "batch_id": batch_id,
            })
            result = _dispatch(c, d["dispatch_id"])
            if batch_id is not None:
                result["batch_id"] = batch_id
                result["withdrawal_id"] = withdrawal_id
            return result
        return None


def agent_dispatches(conn, agent_id: str) -> list[dict]:
    """代理回连后核对自己见过的派工（区分执行过/第一次见）。"""
    return _all(
        conn,
        "SELECT * FROM dispatches WHERE agent_id=? "
        "ORDER BY attempt_no, page_no", (agent_id,))


# ---------------------------------------------------------------------------
# 回执：可能重复、延迟、乱序
# ---------------------------------------------------------------------------

def _quarantine(c, paper_id: str) -> None:
    """隔离纸张作为矛盾证据，但废页结论永不被覆盖。"""
    c.execute(
        "UPDATE papers SET state='isolated' WHERE paper_id=? "
        "AND state<>'waste'", (paper_id,))


def receive_receipt(conn, dispatch_id: str, agent_id: str, paper_id: str,
                    receipt_id: str) -> dict:
    with WriteTx(conn) as c:
        # 回执幂等：同 receipt_id 直接返回既有记录（重复/重发安全）
        existing = _get(c, "SELECT * FROM receipts WHERE receipt_id=?",
                        (receipt_id,))
        if existing is not None:
            c.execute("UPDATE receipts SET is_duplicate=1 WHERE receipt_id=?",
                      (receipt_id,))
            log_event(c, "receipt_duplicate", {
                "receipt_id": receipt_id, "dispatch_id": dispatch_id,
                "paper_id": paper_id,
            })
            return {"receipt_id": receipt_id, "duplicate": True,
                    "outcome": "ignored_duplicate"}

        d = _dispatch(c, dispatch_id)
        if _is_tracked(c, d["job_id"]) and _get(
                c, "SELECT 1 FROM withdrawals WHERE dispatch_id=?",
                (dispatch_id,)) is None:
            raise DomainError(
                "no_withdrawal_record",
                f"派工 {dispatch_id} 没有领用记录：指定批次工单的成品"
                "只能凭已有领用记录的派工认定", 409)
        if d["agent_id"] not in (None, agent_id):
            raise DomainError("agent_mismatch",
                              "回执代理与派工代理不一致")
        owner = _get(c, "SELECT dispatch_id FROM papers WHERE paper_id=?",
                     (paper_id,))
        if owner is not None and owner["dispatch_id"] != dispatch_id:
            raise DomainError("paper_id_conflict",
                              f"纸张 {paper_id} 已属于其他派工", 409)
        # 同派工+同纸张的回执只接受一次
        if _get(c, "SELECT 1 FROM receipts WHERE dispatch_id=? AND paper_id=?",
                (dispatch_id, paper_id)) is not None:
            log_event(c, "receipt_duplicate", {
                "receipt_id": receipt_id, "dispatch_id": dispatch_id,
                "paper_id": paper_id, "reason": "same_dispatch_paper"})
            return {"receipt_id": receipt_id, "duplicate": True,
                    "outcome": "ignored_duplicate"}

        slot = _slot(c, d["job_id"], d["copy_no"], d["page_no"])
        latest = _latest_dispatch(c, d["job_id"], d["copy_no"], d["page_no"])
        is_latest_attempt = latest["dispatch_id"] == dispatch_id
        now = utcnow()
        outcome = "accepted"

        # 登记/取回这张纸（停用见证可能已用同一 paper_id 建过记录）
        paper = _paper(c, paper_id)
        if paper is None:
            c.execute(
                "INSERT INTO papers(paper_id,job_id,copy_no,page_no,"
                "dispatch_id,source,state,created_at) "
                "VALUES (?,?,?,?,?,'receipt','candidate',?)",
                (paper_id, d["job_id"], d["copy_no"], d["page_no"],
                 dispatch_id, now),
            )
        c.execute(
            "INSERT INTO receipts(receipt_id,dispatch_id,agent_id,paper_id,"
            "received_at) VALUES (?,?,?,?,?)",
            (receipt_id, dispatch_id, agent_id, paper_id, now),
        )
        # 迟到的回执也会终结该派工（含已停用后又收到回执的情况）
        c.execute(
            "UPDATE dispatches SET status='receipted', "
            "agent_id=COALESCE(agent_id,?), receipted_at=? "
            "WHERE dispatch_id=? AND status<>'receipted'",
            (agent_id, now, dispatch_id),
        )

        retained = slot["retained_paper_id"]
        slot_status = slot["status"]
        delivered = _get(
            c, "SELECT 1 FROM delivery_items WHERE job_id=? AND copy_no=? "
               "AND page_no=?",
            (d["job_id"], d["copy_no"], d["page_no"])) is not None

        if slot_status == "verified":
            if retained and retained != paper_id:
                if delivered:
                    # 已交付：晚回执不得替换交付用的补印页，只立追查
                    _quarantine(c, paper_id)
                    _open_investigation(
                        c, d["job_id"], d["copy_no"], d["page_no"],
                        "late_receipt_after_delivery",
                        f"交付后收到晚回执 {receipt_id}（纸张 {paper_id}），"
                        f"交付清单保留 {retained} 不变，新纸隔离")
                    outcome = "investigated_after_delivery"
                else:
                    # 未交付但已有现场保留页：矛盾，暂停等现场选页
                    _quarantine(c, paper_id)
                    c.execute(
                        "UPDATE page_slots SET status='conflict', "
                        "conflict_note=? WHERE job_id=? AND copy_no=? "
                        "AND page_no=?",
                        (f"晚回执纸张 {paper_id} 与已保留页 {retained} 矛盾",
                         d["job_id"], d["copy_no"], d["page_no"]),
                    )
                    _pause_copy(c, d["job_id"], d["copy_no"],
                                "晚回执与现场保留页矛盾，暂停交付等待选页")
                    outcome = "conflict_with_retained"
            else:
                # 同一张纸的延迟佐证，维持 verified
                c.execute("UPDATE papers SET state='verified' "
                          "WHERE paper_id=? AND state<>'waste'", (paper_id,))
                outcome = "already_verified_same_paper"

        elif is_latest_attempt:
            # 该派工此前是否已有另一张不同的纸（重复出纸检测）
            prior = _get(
                c, "SELECT paper_id FROM papers WHERE dispatch_id=? "
                   "AND paper_id<>? LIMIT 1", (dispatch_id, paper_id))
            if prior is not None:
                # 同一最新派工却报来另一张纸：疑似重复出纸，隔离待现场选
                _quarantine(c, paper_id)
                c.execute(
                    "UPDATE page_slots SET status='conflict', conflict_note=? "
                    "WHERE job_id=? AND copy_no=? AND page_no=?",
                    (f"派工 {dispatch_id} 出现两张不同纸张，疑似重复出纸",
                     d["job_id"], d["copy_no"], d["page_no"]))
                _pause_copy(c, d["job_id"], d["copy_no"],
                            "同派工多张纸，等待现场选定唯一保留页")
                outcome = "conflict_duplicate_paper"
            else:
                # 最新派工的正常回执：代表当前有效产出，旧 attempt 遗留
                # 的 no_paper/waste/conflict 结论让位于新候选；
                # 旧 attempt 的纸维持隔离/废页状态不变。
                c.execute(
                    "UPDATE papers SET state='candidate' WHERE paper_id=? "
                    "AND state NOT IN ('waste','verified')", (paper_id,))
                c.execute(
                    "UPDATE page_slots SET status='candidate', "
                    "retained_paper_id=NULL, conflict_note=NULL "
                    "WHERE job_id=? AND copy_no=? AND page_no=?",
                    (d["job_id"], d["copy_no"], d["page_no"]),
                )
                outcome = ("accepted_reprint_candidate"
                           if slot_status in ("waste", "no_paper", "conflict")
                           else "accepted")

        elif slot_status == "conflict":
            # 旧 attempt 回执撞入尚未解决的矛盾槽位：再隔离，等现场 resolve
            _quarantine(c, paper_id)
            outcome = "added_to_conflict"

        else:
            # 旧 attempt 的晚回执：更新派工已存在。旧纸只能隔离保留
            # 证据，绝不回退槽位当前结论（哪怕当前是 no_paper/waste）。
            _quarantine(c, paper_id)
            if slot_status in ("verified", "candidate"):
                # 更新派工已产出：槽位保持可用，只记追查
                _open_investigation(
                    c, d["job_id"], d["copy_no"], d["page_no"],
                    "late_receipt_stale_attempt",
                    f"旧派工 {dispatch_id} 晚回执 {receipt_id}（纸张 "
                    f"{paper_id}）晚于更新派工（attempt "
                    f"{latest['attempt_no']}）到达；保留当前槽位结论，"
                    "旧纸隔离")
                outcome = "stale_receipt_quarantined"
            else:
                # 槽位仍是 no_paper/waste/conflict/pending：暂停该份，
                # 交现场选定唯一保留页
                _pause_copy(c, d["job_id"], d["copy_no"],
                            "旧派工晚回执与现场结论并存，暂停交付等待选页")
                c.execute(
                    "UPDATE page_slots SET status='conflict', conflict_note=? "
                    "WHERE job_id=? AND copy_no=? AND page_no=? "
                    "AND status NOT IN ('verified')",
                    (f"旧派工晚回执 {receipt_id}（{paper_id}）与现场结论 "
                     f"{slot_status} 矛盾",
                     d["job_id"], d["copy_no"], d["page_no"]))
                outcome = "conflict_stale_against_onsite"

        log_event(c, "receipt_received", {
            "receipt_id": receipt_id, "dispatch_id": dispatch_id,
            "agent_id": agent_id, "paper_id": paper_id,
            "slot_status_before": slot_status,
            "is_latest_attempt": is_latest_attempt, "outcome": outcome,
        })
        _refresh_copy_status(c, d["job_id"], d["copy_no"])
        return {"receipt_id": receipt_id, "duplicate": False,
                "outcome": outcome,
                "paper": _paper(c, paper_id)}


# ---------------------------------------------------------------------------
# 停用确认：只承诺未来不再执行，不替代出纸检查
# ---------------------------------------------------------------------------

def deactivate_dispatch(conn, dispatch_id: str, agent_id: str,
                        executed_local: bool,
                        evidence_paper_id: str | None = None) -> dict:
    with WriteTx(conn) as c:
        d = _dispatch(c, dispatch_id)
        existing = _get(c, "SELECT 1 FROM deactivations WHERE dispatch_id=?",
                        (dispatch_id,))
        if existing is not None:
            return {"dispatch_id": dispatch_id, "idempotent": True}
        if d["status"] == "receipted":
            raise DomainError(
                "already_receipted",
                "派工已有回执，无需停用；若代理本地另有记录请走核对流程")
        if d["agent_id"] != agent_id:
            raise DomainError(
                "agent_mismatch",
                "只有领取该派工的原代理才能停用它")
        if d["status"] != "claimed":
            raise DomainError("dispatch_not_claimed",
                              f"派工状态为 {d['status']}，不可停用")

        now = utcnow()
        c.execute(
            "UPDATE dispatches SET status='deactivated', deactivated_at=? "
            "WHERE dispatch_id=?", (now, dispatch_id),
        )
        c.execute(
            "INSERT INTO deactivations(dispatch_id,agent_id,"
            "executed_local,evidence_paper_id,confirmed_at) "
            "VALUES (?,?,?,?,?)",
            (dispatch_id, agent_id, int(executed_local),
             evidence_paper_id, now),
        )

        slot = _slot(c, d["job_id"], d["copy_no"], d["page_no"])
        attest_paper = None
        if executed_local and evidence_paper_id:
            # 见证纸：只是"代理本地说执行过"，必须等现场核对
            if _paper(c, evidence_paper_id) is None:
                c.execute(
                    "INSERT INTO papers(paper_id,job_id,copy_no,page_no,"
                    "dispatch_id,source,state,created_at) "
                    "VALUES (?,?,?,?,?,'attest','attest',?)",
                    (evidence_paper_id, d["job_id"], d["copy_no"],
                     d["page_no"], dispatch_id, now),
                )
            attest_paper = _paper(c, evidence_paper_id)
            if slot["status"] == "pending":
                c.execute(
                    "UPDATE page_slots SET status='candidate' "
                    "WHERE job_id=? AND copy_no=? AND page_no=?",
                    (d["job_id"], d["copy_no"], d["page_no"]),
                )
        log_event(c, "dispatch_deactivated", {
            "dispatch_id": dispatch_id, "agent_id": agent_id,
            "executed_local": executed_local,
            "evidence_paper_id": evidence_paper_id,
        })
        _refresh_copy_status(c, d["job_id"], d["copy_no"])
        return {"dispatch_id": dispatch_id, "idempotent": False,
                "executed_local": executed_local,
                "attest_paper": attest_paper}


# ---------------------------------------------------------------------------
# 现场核对
# ---------------------------------------------------------------------------

def _require_deactivation(c, dispatch: dict) -> None:
    """宣告未出纸前，必须有原代理停用该派工的确认。"""
    if dispatch["status"] == "deactivated":
        return
    if _get(c, "SELECT 1 FROM deactivations WHERE dispatch_id=?",
            (dispatch["dispatch_id"],)) is not None:
        return
    raise DomainError(
        "deactivation_required",
        f"派工 {dispatch['dispatch_id']} 尚未停用：失联期间不能凭超时"
        "宣告未出纸，必须先取得原代理的停用确认")


def onsite_no_paper(conn, job_id: str, copy_no: int, page_no: int,
                    note: str | None = None) -> dict:
    with WriteTx(conn) as c:
        slot = _slot(c, job_id, copy_no, page_no)
        d = _latest_dispatch(c, job_id, copy_no, page_no)
        _require_deactivation(c, d)

        # 若已有回执纸/见证纸却宣告未出纸 -> 矛盾证据链
        papers = _all(
            c, "SELECT * FROM papers WHERE job_id=? AND copy_no=? "
               "AND page_no=? AND state IN ('candidate','attest')",
            (job_id, copy_no, page_no))
        if papers:
            for p in papers:
                c.execute(
                    "UPDATE papers SET state='isolated' WHERE paper_id=?",
                    (p["paper_id"],))
            c.execute(
                "UPDATE page_slots SET status='conflict', conflict_note=? "
                "WHERE job_id=? AND copy_no=? AND page_no=?",
                ("现场未出纸 vs 回执/见证声称已出纸",
                 job_id, copy_no, page_no),
            )
            _pause_copy(c, job_id, copy_no,
                        "现场未出纸与代理产出声明矛盾，暂停交付")
            _record_verification(c, job_id, copy_no, page_no, "no_paper",
                                 dispatch_id=d["dispatch_id"], note=note)
            log_event(c, "onsite_no_paper_conflict", {
                "job_id": job_id, "copy_no": copy_no, "page_no": page_no,
                "papers": [p["paper_id"] for p in papers]})
            _refresh_copy_status(c, job_id, copy_no)
            return {"outcome": "conflict",
                    "isolated_papers": [p["paper_id"] for p in papers]}

        c.execute(
            "UPDATE page_slots SET status='no_paper', retained_paper_id=NULL,"
            " conflict_note=NULL WHERE job_id=? AND copy_no=? AND page_no=?",
            (job_id, copy_no, page_no),
        )
        _record_verification(c, job_id, copy_no, page_no, "no_paper",
                             dispatch_id=d["dispatch_id"], note=note)
        log_event(c, "onsite_no_paper", {
            "job_id": job_id, "copy_no": copy_no, "page_no": page_no})
        _refresh_copy_status(c, job_id, copy_no)
        return {"outcome": "no_paper_confirmed"}


def onsite_retrieve(conn, job_id: str, copy_no: int, page_no: int,
                    paper_id: str, dispatch_id: str | None = None,
                    note: str | None = None) -> dict:
    """现场找回带唯一编号的成品并确认有效。"""
    with WriteTx(conn) as c:
        slot = _slot(c, job_id, copy_no, page_no)
        d = _latest_dispatch(c, job_id, copy_no, page_no)
        if _is_tracked(c, job_id):
            eff_dispatch = dispatch_id or d["dispatch_id"]
            if _get(c, "SELECT 1 FROM withdrawals WHERE dispatch_id=?",
                    (eff_dispatch,)) is None:
                raise DomainError(
                    "no_withdrawal_record",
                    f"派工 {eff_dispatch} 没有领用记录，不能据此认定成品；"
                    "若实物来自更早已领用的派工，请显式指定 dispatch_id",
                    409)
        if slot["status"] in ("verified",):
            # 允许对同一保留页重复确认；换纸需走 resolve_conflict
            if slot["retained_paper_id"] == paper_id:
                return {"outcome": "already_verified", "paper_id": paper_id}
            raise DomainError(
                "slot_has_retained_paper",
                f"槽位已保留 {slot['retained_paper_id']}，"
                "更换保留页请使用矛盾处理流程")

        paper = _paper(c, paper_id)
        if paper is None:
            # 现场从实物读到纸张号，服务端首次知晓
            c.execute(
                "INSERT INTO papers(paper_id,job_id,copy_no,page_no,"
                "dispatch_id,source,state,created_at) "
                "VALUES (?,?,?,?,?,'onsite','verified',?)",
                (paper_id, job_id, copy_no, page_no,
                 dispatch_id or d["dispatch_id"], utcnow()),
            )
            paper = _paper(c, paper_id)
        else:
            if paper["state"] == "waste":
                raise DomainError("paper_is_waste",
                                  "废页不能用于交付")
            if paper["state"] == "isolated":
                raise DomainError(
                    "paper_isolated",
                    "该纸处于矛盾隔离状态，须经矛盾处理选定后才能保留")

        # 旧 attempt 的废页/未出纸结论不能阻挡最新派工的成品被现场确认
        from_latest = paper["dispatch_id"] == d["dispatch_id"]
        if slot["status"] in ("waste", "no_paper") and not from_latest:
            raise DomainError(
                "slot_closed_needs_reprint",
                f"槽位结论为 {slot['status']} 且该纸不属于最新派工，"
                "请通过补打或矛盾处理流程操作")
        if slot["status"] == "conflict":
            # 矛盾槽位不允许普通 retrieve 直接定版，必须走 resolve
            raise DomainError(
                "slot_in_conflict",
                "槽位处于矛盾状态，请使用 resolve-conflict 选定唯一保留页")
        if paper["state"] != "verified":
            c.execute(
                "UPDATE papers SET state='verified' WHERE paper_id=?",
                (paper_id,))

        # 同槽位其他候选纸降级隔离，保证唯一保留
        others = _all(
            c, "SELECT paper_id FROM papers WHERE job_id=? AND copy_no=? "
               "AND page_no=? AND paper_id<>? AND state IN "
               "('candidate','attest')",
            (job_id, copy_no, page_no, paper_id))
        for o in others:
            c.execute("UPDATE papers SET state='isolated' WHERE paper_id=?",
                      (o["paper_id"],))

        c.execute(
            "UPDATE page_slots SET status='verified', retained_paper_id=?, "
            "conflict_note=NULL WHERE job_id=? AND copy_no=? AND page_no=?",
            (paper_id, job_id, copy_no, page_no),
        )
        _record_verification(c, job_id, copy_no, page_no, "retrieve",
                             paper_id=paper_id,
                             dispatch_id=dispatch_id or d["dispatch_id"],
                             note=note)
        log_event(c, "onsite_retrieve", {
            "job_id": job_id, "copy_no": copy_no, "page_no": page_no,
            "paper_id": paper_id,
            "isolated_others": [o["paper_id"] for o in others]})
        new_status = _refresh_copy_status(c, job_id, copy_no)
        return {"outcome": "verified", "paper_id": paper_id,
                "copy_status": new_status}


def onsite_waste(conn, job_id: str, copy_no: int, page_no: int,
                 paper_id: str, note: str | None = None) -> dict:
    """确认某张已出纸为废页。废页永久不可交付。"""
    with WriteTx(conn) as c:
        slot = _slot(c, job_id, copy_no, page_no)
        paper = _paper(c, paper_id)
        if paper is None:
            raise DomainError("paper_unknown",
                              "服务端未知该纸张，无法判废；请先核对身份",
                              404)
        if paper["job_id"] != job_id or paper["copy_no"] != copy_no or \
                paper["page_no"] != page_no:
            raise DomainError("paper_slot_mismatch",
                              "纸张不属于该（份，页）槽位")
        c.execute("UPDATE papers SET state='waste' WHERE paper_id=?",
                  (paper_id,))
        if slot["retained_paper_id"] == paper_id:
            c.execute(
                "UPDATE page_slots SET status='waste', "
                "retained_paper_id=NULL WHERE job_id=? AND copy_no=? "
                "AND page_no=?", (job_id, copy_no, page_no))
        elif slot["status"] != "conflict":
            c.execute(
                "UPDATE page_slots SET status='waste' WHERE job_id=? "
                "AND copy_no=? AND page_no=?",
                (job_id, copy_no, page_no))
        _record_verification(c, job_id, copy_no, page_no, "waste",
                             paper_id=paper_id,
                             dispatch_id=paper["dispatch_id"], note=note)
        log_event(c, "onsite_waste", {
            "job_id": job_id, "copy_no": copy_no, "page_no": page_no,
            "paper_id": paper_id})
        _refresh_copy_status(c, job_id, copy_no)
        return {"outcome": "waste_recorded", "paper_id": paper_id}


def resolve_conflict(conn, job_id: str, copy_no: int, page_no: int,
                     keep_paper_id: str | None,
                     note: str | None = None) -> dict:
    """现场在矛盾槽位选定唯一保留页，其余纸张隔离。

    keep_paper_id=None 表示现场判定没有任何有效候选（全部隔离），
    槽位置 no_paper，仍需补打。
    """
    with WriteTx(conn) as c:
        slot = _slot(c, job_id, copy_no, page_no)
        if slot["status"] != "conflict":
            raise DomainError("not_in_conflict",
                              f"槽位状态为 {slot['status']}，无需矛盾处理")
        papers = _all(
            c, "SELECT * FROM papers WHERE job_id=? AND copy_no=? "
               "AND page_no=?", (job_id, copy_no, page_no))
        candidates = [p for p in papers if p["state"] != "waste"]

        if keep_paper_id is None:
            for p in candidates:
                c.execute(
                    "UPDATE papers SET state='isolated' WHERE paper_id=?",
                    (p["paper_id"],))
            c.execute(
                "UPDATE page_slots SET status='no_paper', "
                "retained_paper_id=NULL, conflict_note=NULL "
                "WHERE job_id=? AND copy_no=? AND page_no=?",
                (job_id, copy_no, page_no))
            outcome = "no_candidate_needs_reprint"
        else:
            keep = next((p for p in candidates
                         if p["paper_id"] == keep_paper_id), None)
            if keep is None:
                raise DomainError("keep_paper_not_candidate",
                                  "保留纸不存在或为废页", 404)
            for p in candidates:
                new_state = "verified" if p["paper_id"] == keep_paper_id \
                    else "isolated"
                c.execute("UPDATE papers SET state=? WHERE paper_id=?",
                          (new_state, p["paper_id"]))
            c.execute(
                "UPDATE page_slots SET status='verified', "
                "retained_paper_id=?, conflict_note=NULL "
                "WHERE job_id=? AND copy_no=? AND page_no=?",
                (keep_paper_id, job_id, copy_no, page_no))
            outcome = "kept_unique_paper"

        _record_verification(c, job_id, copy_no, page_no,
                             "resolve_conflict", paper_id=keep_paper_id,
                             note=note)
        # 关联的追查项（含交付后场景）随现场结论关闭，但交付清单不变
        c.execute(
            "UPDATE investigations SET resolved=1 WHERE job_id=? AND "
            "copy_no=? AND page_no=? AND resolved=0",
            (job_id, copy_no, page_no))
        log_event(c, "conflict_resolved", {
            "job_id": job_id, "copy_no": copy_no, "page_no": page_no,
            "keep_paper_id": keep_paper_id, "outcome": outcome})
        new_status = _refresh_copy_status(c, job_id, copy_no)
        return {"outcome": outcome, "copy_status": new_status}


# ---------------------------------------------------------------------------
# 补打
# ---------------------------------------------------------------------------

def reprint(conn, job_id: str, copy_no: int, page_no: int) -> dict:
    with WriteTx(conn) as c:
        slot = _slot(c, job_id, copy_no, page_no)
        if slot["status"] not in ("no_paper", "waste"):
            raise DomainError(
                "reprint_not_allowed",
                f"槽位状态为 {slot['status']}：只有现场确认未出纸或记录"
                "废页，且原派工已停用后才能补打")
        latest = _latest_dispatch(c, job_id, copy_no, page_no)
        if latest["status"] not in ("deactivated", "receipted"):
            # receipted + waste：纸已出但判废，原派工生命周期已结束
            raise DomainError(
                "deactivation_required",
                "原派工尚未结束（无停用确认），不能补打")
        if latest["status"] == "receipted" and slot["status"] != "waste":
            raise DomainError(
                "reprint_not_allowed",
                "有回执的槽位只有在纸张被判废后才能补打")
        attempt = latest["attempt_no"] + 1
        dispatch_id = _new_id("dsp")
        c.execute(
            "INSERT INTO dispatches(dispatch_id,job_id,copy_no,page_no,"
            "attempt_no,status,created_at) VALUES (?,?,?,?,?,'queued',?)",
            (dispatch_id, job_id, copy_no, page_no, attempt, utcnow()),
        )
        log_event(c, "reprint_created", {
            "dispatch_id": dispatch_id, "job_id": job_id,
            "copy_no": copy_no, "page_no": page_no, "attempt_no": attempt,
            "reason": slot["status"]})
        return _dispatch(c, dispatch_id)


# ---------------------------------------------------------------------------
# 交付
# ---------------------------------------------------------------------------

def delivery_readiness(conn, job_id: str) -> list[dict]:
    copies = _all(
        conn, "SELECT * FROM job_copies WHERE job_id=? ORDER BY copy_no",
        (job_id,))
    result = []
    for cp in copies:
        if cp["status"] == "cancelled":
            result.append({
                "copy_no": cp["copy_no"], "copy_kind": cp.get("copy_kind"),
                "status": "cancelled",
                "paused_reason": cp["paused_reason"],
                "ready": False, "blocking_slots": []})
            continue
        slots = _all(
            conn, "SELECT * FROM page_slots WHERE job_id=? AND copy_no=? "
                  "ORDER BY page_no", (job_id, cp["copy_no"]))
        blocking = [
            {"page_no": s["page_no"], "status": s["status"],
             "note": s["conflict_note"]}
            for s in slots if s["status"] != "verified"]
        recalled_usage = _recalled_usage(conn, job_id, cp["copy_no"])
        recalled_batches = sorted(
            {u["batch_id"] for u in recalled_usage if u.get("batch_id")})
        if recalled_batches:
            blocking.append({
                "scope": "batch_recall", "status": "batch_recalled",
                "batches": recalled_batches,
                "note": "实际用纸含永久召回批次，不能交付"})
        is_spare = cp.get("copy_kind") == "spare"
        if is_spare:
            blocking.append({
                "scope": "delivery_channel",
                "status": "spare_exchange_only",
                "note": "备品只能经柜台换货交给原工单客户"})
        slot_ready = all(s["status"] == "verified" for s in slots)
        result.append({
            "copy_no": cp["copy_no"], "copy_kind": cp.get("copy_kind"),
            "status": cp["status"],
            "paused_reason": cp["paused_reason"],
            "ready": not blocking,
            "slot_ready": slot_ready,
            "ordinary_delivery_allowed": not is_spare
            and not recalled_batches,
            "exchange_ready": is_spare and slot_ready
            and not recalled_batches,
            "recalled_batches": recalled_batches,
            "blocking_slots": blocking})
    return result


def _check_deliverable(c, job: dict, copy_no: int, *,
                       allow_spare: bool = False) -> tuple[dict, list[dict]]:
    """原有交付条件（不变）：份存在、未撤销/未交付，整份每页 verified
    且都有唯一保留纸。返回（份行，槽位列表）。

    allow_spare=False 时备品份不能走普通/授信取件；换货校验显式传 True。
    纸料追踪工单还必须满足：实际保留纸的领用批次均未被永久召回。
    """
    cp = row_to_dict(_get(
        c, "SELECT * FROM job_copies WHERE job_id=? AND copy_no=?",
        (job["job_id"], copy_no)))
    if cp is None:
        raise DomainError("copy_not_found", "份不存在", 404)
    if cp.get("copy_kind") == "spare" and not allow_spare:
        raise DomainError(
            "spare_exchange_only",
            f"工单 {job['job_id']} 份 {copy_no} 是补制备品，只能经柜台"
            "换货交给原工单客户，不能走普通或授信取件", 409)
    if cp["status"] == "cancelled":
        raise DomainError("copy_cancelled", "该份已撤销，不再派工或交付")
    if cp["status"] == "delivered":
        raise DomainError("already_delivered", "该份已交付")
    slots = _all(
        c, "SELECT * FROM page_slots WHERE job_id=? AND copy_no=? "
          "ORDER BY page_no", (job["job_id"], copy_no))
    if len(slots) != job["page_count"]:
        raise DomainError("slot_count_mismatch", "槽位不完整")
    bad = [s for s in slots if s["status"] != "verified"
           or not s["retained_paper_id"]]
    if bad:
        raise DomainError(
            "not_deliverable",
            "只有整份每页都明确且有效（verified）才可交付；"
            f"未满足页：{[s['page_no'] for s in bad]}")
    _assert_copy_not_recalled(c, job["job_id"], copy_no)
    return cp, slots


def _record_delivery(c, job_id: str, copy_no: int, slots: list[dict],
                     request_id: str | None = None,
                     exchange_id: str | None = None) -> dict:
    """在当前事务内固化交付清单（只追加）并推进份/工单状态。"""
    now = utcnow()
    items = []
    for s in slots:
        paper = _paper(c, s["retained_paper_id"])
        c.execute(
            "INSERT INTO delivery_items(job_id,copy_no,page_no,"
            "paper_id,dispatch_id,delivered_at,request_id,exchange_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, copy_no, s["page_no"], s["retained_paper_id"],
             paper["dispatch_id"], now, request_id, exchange_id))
        items.append({"copy_no": copy_no, "page_no": s["page_no"],
                      "paper_id": s["retained_paper_id"],
                      "dispatch_id": paper["dispatch_id"]})
    c.execute(
        "UPDATE job_copies SET status='delivered', paused_reason=NULL "
        "WHERE job_id=? AND copy_no=?", (job_id, copy_no))
    # 每次真实交付都建立物理份持有关系。换货事务随后会把备品 custody 的
    # logical_* 改指向原交付份，并把旧物理份置为 returned。
    c.execute(
        "INSERT INTO copy_custody(physical_job_id,physical_copy_no,"
        "logical_job_id,logical_copy_no,status,source_request_id,"
        "out_exchange_id,delivered_at) VALUES (?,?,?,?, 'with_customer',"
        "?,?,?)",
        (job_id, copy_no, job_id, copy_no, request_id, exchange_id, now))
    if not _get(c, "SELECT 1 FROM job_copies WHERE job_id=? AND "
                   "status NOT IN ('delivered','cancelled') "
                   "AND COALESCE(copy_kind,'original')='original'",
                (job_id,)):
        c.execute("UPDATE jobs SET status='delivered' WHERE job_id=?",
                  (job_id,))
    log_event(c, "delivery_confirmed", {
        "job_id": job_id, "copy_no": copy_no, "items": items,
        "request_id": request_id})
    return {"job_id": job_id, "copy_no": copy_no,
            "delivered_at": now, "items": items,
            "request_id": request_id}


def confirm_delivery(conn, job_id: str, copy_no: int) -> dict:
    """原交付入口：不使用授信的工单仍走此流程。

    授信工单在此被明确拒绝——不能绕过授信资金保障直接交付。
    """
    with WriteTx(conn) as c:
        job = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)))
        if job is None:
            raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
        if job["credit_account_id"] is not None:
            raise DomainError(
                "credit_delivery_required",
                f"工单 {job_id} 使用企业授信（账户 "
                f"{job['credit_account_id']}），必须携带取件请求号走授信"
                "取件入口；原交付入口不能绕过授信", 409)
        _, slots = _check_deliverable(c, job, copy_no)
        return _record_delivery(c, job_id, copy_no, slots)


# ---------------------------------------------------------------------------
# 企业授信取件：请求号幂等 + 跨服务资金状态机
# ---------------------------------------------------------------------------

def _credit_job(c, job_id: str) -> dict:
    job = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                           (job_id,)))
    if job is None:
        raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
    if job["credit_account_id"] is None:
        raise DomainError(
            "not_credit_job",
            f"工单 {job_id} 未使用企业授信，请走原交付入口", 409)
    return job


def _pickup_row(c, request_id: str):
    return row_to_dict(_get(
        c, "SELECT * FROM delivery_requests WHERE request_id=?",
        (request_id,)))


def _pickup_view(conn, row: dict) -> dict:
    """取件结果视图：明确区分待处理/待清算、确已扣费、未产生净费用，
    并在已交付时附上冻结的交付清单。"""
    status = row["status"]
    if status == "delivered":
        fund_status, fund_text = "charged", "确已扣费"
    elif status in ("rejected", "released"):
        fund_status, fund_text = "no_net_charge", "未产生净费用"
    elif status == "held":
        fund_status, fund_text = "held", "待清算：额度已锁定，交付/清算处理中"
    else:
        fund_status, fund_text = "pending", "待处理：财务结果未定，请勿重复下单"
    out = dict(row)
    out["fund_status"] = fund_status
    out["fund_status_text"] = fund_text
    out["delivery_list"] = (
        delivery_list(conn, row["job_id"], row["copy_no"])["items"]
        if status == "delivered" else [])
    return out


def request_credit_pickup(conn, job_id: str, copy_no: int,
                          request_id: str) -> dict:
    """柜员提交授信取件：工单号 + 份号 + 取件请求号。

    幂等/冲突
    ---------
    * 同 request_id 重送：返回同一请求的收敛结果，不多扣费、不重复交付；
    * request_id 已用于其他（工单/份）取件：拒绝；
    * 同一份尚有未终结请求时换新号：拒绝（换号不能形成第二次交付）。
    终态（rejected/released）后允许用新请求号重新发起。
    """
    with WriteTx(conn) as c:
        job = _credit_job(c, job_id)
        existing = _pickup_row(c, request_id)
        if existing is not None:
            if (existing["job_id"], existing["copy_no"]) != \
                    (job_id, copy_no):
                raise DomainError(
                    "request_id_conflict",
                    f"取件请求号 {request_id} 已用于工单 "
                    f"{existing['job_id']} 份 {existing['copy_no']} 的取件，"
                    "不能复用于其他取件", 409)
            snapshot = dict(existing)
        else:
            cp = row_to_dict(_get(
                c, "SELECT * FROM job_copies WHERE job_id=? AND copy_no=?",
                (job_id, copy_no)))
            if cp is None:
                raise DomainError("copy_not_found", "份不存在", 404)
            if cp["status"] == "cancelled":
                raise DomainError("copy_cancelled",
                                  "该份已撤销，不再派工或交付")
            if cp.get("copy_kind") == "spare":
                raise DomainError(
                    "spare_exchange_only",
                    f"工单 {job_id} 份 {copy_no} 是备品，只能经柜台换货"
                    "交给原工单客户，不能走授信取件", 409)
            if cp["status"] == "delivered":
                # 同一份换号也不能形成第二次交付或扣费
                prior = _get(
                    c, "SELECT request_id FROM delivery_requests "
                       "WHERE job_id=? AND copy_no=? AND status='delivered' "
                       "LIMIT 1", (job_id, copy_no))
                raise DomainError(
                    "already_delivered",
                    f"工单 {job_id} 份 {copy_no} 已凭取件请求 "
                    f"{prior['request_id'] if prior else ''} 交付；"
                    "同一份换号不能形成第二次交付或扣费")
            active = _get(
                c, "SELECT request_id FROM delivery_requests "
                   "WHERE job_id=? AND copy_no=? AND status IN "
                   "('pending','held') LIMIT 1", (job_id, copy_no))
            if active is not None:
                raise DomainError(
                    "pickup_already_in_progress",
                    f"工单 {job_id} 份 {copy_no} 已有未终结取件请求 "
                    f"{active['request_id']}：请用原请求号重送或等待其"
                    "终结；同一份换号不能形成第二次交付或扣费", 409)
            now = utcnow()
            c.execute(
                "INSERT INTO delivery_requests(request_id,job_id,copy_no,"
                "account_id,amount,status,release_pending,created_at,"
                "updated_at) VALUES (?,?,?,?,?, 'pending', 0, ?,?)",
                (request_id, job_id, copy_no, job["credit_account_id"],
                 job["price_per_copy"], now, now))
            log_event(c, "pickup_requested", {
                "request_id": request_id, "job_id": job_id,
                "copy_no": copy_no,
                "account_id": job["credit_account_id"],
                "amount": job["price_per_copy"]})
            snapshot = dict(_pickup_row(c, request_id))

    if snapshot["status"] in ("delivered", "rejected", "released"):
        return _pickup_view(conn, snapshot)   # 终态重送：幂等回放
    return _drive_pickup(conn, snapshot)


def _drive_pickup(conn, row: dict) -> dict:
    """按请求当前状态推进一步收敛；所有外部调用都带幂等键，可安全重入。"""
    if row["status"] == "pending":
        return _pickup_hold_stage(conn, row["request_id"])
    if row["status"] == "held":
        return _pickup_held_stage(conn, row["request_id"])
    return _pickup_view(conn, row)


def _pickup_hold_stage(conn, request_id: str) -> dict:
    """pending：向财务锁定额度。"""
    with WriteTx(conn) as c:
        snapshot = dict(_pickup_row(c, request_id))
    if snapshot["status"] != "pending":
        return _drive_pickup(conn, snapshot)
    try:
        fin = finance_client.hold(
            request_id, snapshot["account_id"], snapshot["amount"],
            reference=f"{snapshot['job_id']}#copy{snapshot['copy_no']}")
    except finance_client.FinanceRejected as exc:
        # 额度不足/请求号冲突等明确拒绝：不交付、无净费用，请求终结
        with WriteTx(conn) as c:
            c.execute(
                "UPDATE delivery_requests SET status='rejected', "
                "last_error=?, updated_at=? WHERE request_id=? "
                "AND status='pending'",
                (f"{exc.code}: {exc.message}", utcnow(), request_id))
            log_event(c, "pickup_rejected", {
                "request_id": request_id, "error_code": exc.code,
                "message": exc.message})
        return _pickup_view(conn, _pickup_row(conn, request_id))
    except finance_client.FinanceUnavailable as exc:
        # 结果未知：保持待处理，绝不凭超时报告成功或认定没扣款
        with WriteTx(conn) as c:
            c.execute(
                "UPDATE delivery_requests SET last_error=?, updated_at=? "
                "WHERE request_id=? AND status='pending'",
                (f"finance_unavailable: {exc}", utcnow(), request_id))
            log_event(c, "pickup_finance_unavailable",
                      {"request_id": request_id, "stage": "hold"})
        return _pickup_view(conn, _pickup_row(conn, request_id))

    if fin.get("status") == "voided":
        # 防御分支：同号资金已被释放（本服务正常流程下不会出现）
        with WriteTx(conn) as c:
            c.execute(
                "UPDATE delivery_requests SET status='rejected', "
                "last_error=?, updated_at=? WHERE request_id=? "
                "AND status='pending'",
                ("finance_request_voided: 同号资金已释放，交付不成立",
                 utcnow(), request_id))
            log_event(c, "pickup_rejected", {
                "request_id": request_id,
                "error_code": "finance_request_voided"})
        return _pickup_view(conn, _pickup_row(conn, request_id))

    with WriteTx(conn) as c:
        c.execute(
            "UPDATE delivery_requests SET status='held', last_error=NULL, "
            "updated_at=? WHERE request_id=? AND status='pending'",
            (utcnow(), request_id))
        log_event(c, "pickup_funds_held", {
            "request_id": request_id,
            "finance_status": fin.get("status")})
    return _pickup_held_stage(conn, request_id)


def _pickup_held_stage(conn, request_id: str) -> dict:
    """held：额度已有确定保障。确认交付条件仍满足 -> 固化交付并清算；
    条件失效或被标记放弃 -> 释放锁定，交付不成立。"""
    with WriteTx(conn) as c:
        row = dict(_pickup_row(c, request_id))
        job = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                               (row["job_id"],)))
        cp = row_to_dict(_get(
            c, "SELECT * FROM job_copies WHERE job_id=? AND copy_no=?",
            (row["job_id"], row["copy_no"])))

    # 上一轮已固化交付但清算应答丢失：只需补做幂等清算
    if cp is not None and cp["status"] == "delivered":
        return _pickup_settle(conn, request_id)

    if row["release_pending"]:
        return _pickup_release(conn, request_id,
                               row.get("last_error") or "交付条件失效")

    with WriteTx(conn) as c:
        job2 = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                                (row["job_id"],)))
        try:
            _, slots = _check_deliverable(c, job2, row["copy_no"])
        except DomainError as exc:
            # 原交付条件在确认前失效：本次交付不得成立，额度必须恢复可用
            c.execute(
                "UPDATE delivery_requests SET release_pending=1, "
                "last_error=?, updated_at=? WHERE request_id=? "
                "AND status='held'",
                (f"{exc.code}: {exc.message}", utcnow(), request_id))
            log_event(c, "pickup_conditions_failed", {
                "request_id": request_id, "error_code": exc.code,
                "message": exc.message})
            failure = exc
        else:
            failure = None
    if failure is not None:
        return _pickup_release(conn, request_id, str(failure))

    # 条件满足：先在本地事务固化交付（清单照旧冻结），再做幂等清算。
    # 此刻额度处于 held，资金保障确定；清算只需把同一笔 hold 转 settle。
    with WriteTx(conn) as c:
        job3 = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                                (row["job_id"],)))
        _, slots = _check_deliverable(c, job3, row["copy_no"])
        _record_delivery(c, row["job_id"], row["copy_no"], slots,
                         request_id=request_id)
        log_event(c, "pickup_delivery_recorded",
                  {"request_id": request_id})
    return _pickup_settle(conn, request_id)


def _pickup_settle(conn, request_id: str) -> dict:
    with WriteTx(conn) as c:
        row = dict(_pickup_row(c, request_id))
    try:
        fin = finance_client.settle(request_id, row["amount"])
    except finance_client.FinanceUnavailable as exc:
        # 交付已成立但清算应答未知：钱仍在 hold 中（他人动不了），
        # 保持待清算状态，重送/对账后自动完成，无需人工改账
        with WriteTx(conn) as c:
            c.execute(
                "UPDATE delivery_requests SET last_error=?, updated_at=? "
                "WHERE request_id=? AND status='held'",
                (f"finance_unavailable: {exc}", utcnow(), request_id))
            log_event(c, "pickup_finance_unavailable",
                      {"request_id": request_id, "stage": "settle"})
        return _pickup_view(conn, _pickup_row(conn, request_id))
    except finance_client.FinanceRejected as exc:
        # 防御：先查财务真实状态再决定，绝不臆断
        try:
            fin = finance_client.get_request(request_id)
        except finance_client.FinanceUnavailable:
            with WriteTx(conn) as c:
                c.execute(
                    "UPDATE delivery_requests SET last_error=?, updated_at=? "
                    "WHERE request_id=? AND status='held'",
                    (f"settle_rejected_then_unreachable: {exc}",
                     utcnow(), request_id))
            return _pickup_view(conn, _pickup_row(conn, request_id))
        if fin.get("status") != "settled":
            raise
    with WriteTx(conn) as c:
        now = utcnow()
        c.execute(
            "UPDATE delivery_requests SET status='delivered', "
            "release_pending=0, last_error=NULL, delivered_at=?, "
            "updated_at=? WHERE request_id=? AND status='held'",
            (now, now, request_id))
        log_event(c, "pickup_settled_delivered", {
            "request_id": request_id,
            "finance_status": fin.get("status")})
    return _pickup_view(conn, _pickup_row(conn, request_id))


def _pickup_release(conn, request_id: str, reason: str) -> dict:
    try:
        fin = finance_client.release(request_id, reason)
    except finance_client.FinanceUnavailable as exc:
        # 释放尚未确认：维持 held + release_pending，仍按待处理展示，
        # 不阻塞其他任何流程；恢复后重送/对账自动完成释放
        with WriteTx(conn) as c:
            c.execute(
                "UPDATE delivery_requests SET release_pending=1, "
                "last_error=?, updated_at=? WHERE request_id=? "
                "AND status='held'",
                (f"release_pending_finance_unavailable: {exc}",
                 utcnow(), request_id))
            log_event(c, "pickup_finance_unavailable",
                      {"request_id": request_id, "stage": "release"})
        return _pickup_view(conn, _pickup_row(conn, request_id))
    with WriteTx(conn) as c:
        c.execute(
            "UPDATE delivery_requests SET status='released', "
            "release_pending=0, updated_at=? "
            "WHERE request_id=? AND status='held'",
            (utcnow(), request_id))
        log_event(c, "pickup_released", {
            "request_id": request_id, "reason": reason,
            "finance_status": fin.get("status")})
    return _pickup_view(conn, _pickup_row(conn, request_id))


def reconcile_pickup(conn, request_id: str) -> dict:
    """对结果未定的取件请求主动对账收敛（重送同一请求号等价）。"""
    with WriteTx(conn) as c:
        row = _pickup_row(c, request_id)
        if row is None:
            raise DomainError(
                "pickup_not_found", f"取件请求 {request_id} 不存在", 404)
        snapshot = dict(row)
    if snapshot["status"] in ("delivered", "rejected", "released"):
        return _pickup_view(conn, snapshot)
    return _drive_pickup(conn, snapshot)


def get_pickup(conn, request_id: str) -> dict:
    with WriteTx(conn) as c:
        row = _pickup_row(c, request_id)
        if row is None:
            raise DomainError(
                "pickup_not_found", f"取件请求 {request_id} 不存在", 404)
        snapshot = dict(row)
    return _pickup_view(conn, snapshot)


def list_pickups(conn, job_id: str | None = None) -> list[dict]:
    with WriteTx(conn) as c:
        if job_id is not None:
            rows = _all(
                c, "SELECT * FROM delivery_requests WHERE job_id=? "
                   "ORDER BY rowid", (job_id,))
        else:
            rows = _all(c, "SELECT * FROM delivery_requests ORDER BY rowid")
    return [_pickup_view(conn, r) for r in rows]


def delivery_list(conn, job_id: str, copy_no: int | None = None) -> dict:
    where = "WHERE job_id=?"
    params: list = [job_id]
    if copy_no is not None:
        where += " AND copy_no=?"
        params.append(copy_no)
    items = _all(
        conn, f"SELECT * FROM delivery_items {where} "
              f"ORDER BY copy_no,page_no", params)
    return {"job_id": job_id, "items": items}


# ---------------------------------------------------------------------------
# 纸料承诺：到货登记、换批、撤销、查询
# ---------------------------------------------------------------------------

def register_arrival(conn, batch_id: str, sheets: int,
                     note: str | None = None) -> dict:
    """库管按批次登记到货张数；同一批可再入库（多次登记累加）。"""
    batch_id = (batch_id or "").strip()
    if not batch_id:
        raise DomainError("invalid_arrival", "批次编号不能为空", 400)
    if sheets <= 0:
        raise DomainError("invalid_arrival", "到货张数必须为正整数", 400)
    with WriteTx(conn) as c:
        c.execute(
            "INSERT INTO stock_arrivals(batch_id,sheets,note,created_at) "
            "VALUES (?,?,?,?)",
            (batch_id, sheets, note, utcnow()))
        log_event(c, "stock_arrived", {
            "batch_id": batch_id, "sheets": sheets, "note": note})
    return batch_view(conn, batch_id)


def batch_view(conn, batch_id: str) -> dict:
    """单批账面：尚未发出张数 = 到货合计 − 已发出合计。"""
    arrived = _arrived_by_batch(conn).get(batch_id, 0)
    withdrawn = _withdrawn_by_batch(conn).get(batch_id, 0)
    committed = _demand_by_batch(conn, ("planned", "locked")).get(batch_id, 0)
    available = arrived - withdrawn
    recall = _get(conn, "SELECT recalled_at,note FROM paper_batch_recalls "
                        "WHERE batch_id=?", (batch_id,))
    return {"batch_id": batch_id, "arrived": arrived, "withdrawn": withdrawn,
            "available": available, "committed_original": committed,
            "slack": available - committed,
            "recalled": recall is not None,
            "permanently_recalled": recall is not None,
            "recalled_at": recall["recalled_at"] if recall else None,
            "recall_note": recall["note"] if recall else None}


def list_batches(conn) -> list[dict]:
    return [batch_view(conn, b) for b in sorted(_all_batch_ids(conn))]


def job_commitments(conn, job_id: str) -> dict:
    """各份的承诺状态及已锁定批次。"""
    if row_to_dict(_get(conn, "SELECT 1 FROM jobs WHERE job_id=?",
                        (job_id,))) is None:
        raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
    allowed = _allowed_batches(conn, job_id)
    copies = _all(
        conn, "SELECT * FROM job_copies WHERE job_id=? ORDER BY copy_no",
        (job_id,))
    commitments = {r["copy_no"]: r for r in _all(
        conn, "SELECT * FROM copy_commitments WHERE job_id=?", (job_id,))}
    out = []
    for cp in copies:
        cm = commitments.get(cp["copy_no"])
        entry = {"copy_no": cp["copy_no"], "copy_status": cp["status"],
                 "commitment": None}
        if cm is not None:
            remaining = _get(
                conn, "SELECT COUNT(*) AS n FROM dispatches "
                      "WHERE job_id=? AND copy_no=? AND attempt_no=1 "
                      "AND status='queued'", (job_id, cp["copy_no"]))["n"]
            entry["commitment"] = {
                "status": cm["status"], "batch_id": cm["batch_id"],
                "locked_at": cm["locked_at"],
                "cancelled_at": cm["cancelled_at"],
                "remaining_original": remaining}
        out.append(entry)
    return {"job_id": job_id, "allowed_batches": allowed or None,
            "copies": out}


def dispatch_withdrawal(conn, dispatch_id: str) -> dict:
    """从派工追溯领用批次。"""
    d = _dispatch(conn, dispatch_id)
    w = row_to_dict(_get(conn, "SELECT * FROM withdrawals "
                               "WHERE dispatch_id=?", (dispatch_id,)))
    return {"dispatch_id": dispatch_id, "job_id": d["job_id"],
            "copy_no": d["copy_no"], "page_no": d["page_no"],
            "attempt_no": d["attempt_no"], "status": d["status"],
            "withdrawal": w}


def _commitment_or_error(c, job_id: str, copy_no: int) -> dict:
    cm = row_to_dict(_get(
        c, "SELECT * FROM copy_commitments WHERE job_id=? AND copy_no=?",
        (job_id, copy_no)))
    if cm is not None:
        return cm
    if row_to_dict(_get(c, "SELECT 1 FROM jobs WHERE job_id=?",
                        (job_id,))) is None:
        raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
    if not _is_tracked(c, job_id):
        raise DomainError("not_batch_tracked",
                          "该工单未指定批次，不参与纸料账", 409)
    raise DomainError("copy_not_found", f"份 {copy_no} 不存在", 404)


def reassign_copy_batch(conn, job_id: str, copy_no: int,
                        batch_id: str) -> dict:
    """把未开印的份换到其他允许批次；换入批须容得下该份全部原印。"""
    with WriteTx(conn) as c:
        cm = _commitment_or_error(c, job_id, copy_no)
        if cm["status"] == "cancelled":
            raise DomainError("copy_cancelled", "该份已撤销，不能换批")
        if cm["status"] != "planned":
            raise DomainError(
                "copy_already_locked",
                f"该份已开印并锁定批次 {cm['batch_id']}，不能换批")
        allowed = _allowed_batches(c, job_id)
        if batch_id not in allowed:
            raise DomainError(
                "batch_not_allowed",
                f"批次 {batch_id} 不在工单允许批次 {allowed} 内", 409)
        if batch_id == cm["batch_id"]:
            return job_commitments(conn, job_id)
        demand = _get(c, "SELECT COUNT(*) AS n FROM dispatches "
                         "WHERE job_id=? AND copy_no=? AND attempt_no=1 "
                         "AND status='queued'", (job_id, copy_no))["n"]
        available = _available_by_batch(c).get(batch_id, 0)
        committed = _demand_by_batch(c, ("planned", "locked")).get(
            batch_id, 0)
        if committed + demand > available:
            raise DomainError(
                "insufficient_stock",
                f"批次 {batch_id} 库内可用 {available} 张，已承诺原印 "
                f"{committed} 张，换入该份还需 {demand} 张，余量不足", 409)
        c.execute(
            "UPDATE copy_commitments SET batch_id=? "
            "WHERE job_id=? AND copy_no=?", (batch_id, job_id, copy_no))
        log_event(c, "copy_batch_reassigned", {
            "job_id": job_id, "copy_no": copy_no,
            "from_batch": cm["batch_id"], "to_batch": batch_id,
            "reason": "manual"})
    return job_commitments(conn, job_id)


def cancel_copy(conn, job_id: str, copy_no: int) -> dict:
    """整份撤销（仅限未开印的份）：不再派工或交付，承诺用纸释放。"""
    with WriteTx(conn) as c:
        cm = _commitment_or_error(c, job_id, copy_no)
        if cm["status"] == "cancelled":
            return job_commitments(conn, job_id)  # 幂等
        if cm["status"] != "planned":
            raise DomainError(
                "copy_already_locked",
                f"该份已开印并锁定批次 {cm['batch_id']}，不能撤销")
        now = utcnow()
        c.execute(
            "UPDATE copy_commitments SET status='cancelled', cancelled_at=? "
            "WHERE job_id=? AND copy_no=?", (now, job_id, copy_no))
        c.execute(
            "UPDATE job_copies SET status='cancelled', paused_reason=? "
            "WHERE job_id=? AND copy_no=?",
            ("已撤销，不再派工或交付", job_id, copy_no))
        c.execute(
            "UPDATE dispatches SET status='cancelled' "
            "WHERE job_id=? AND copy_no=? AND status='queued'",
            (job_id, copy_no))
        cancelled_n = c.execute("SELECT changes()").fetchone()[0]
        log_event(c, "copy_cancelled", {
            "job_id": job_id, "copy_no": copy_no,
            "freed_batch": cm["batch_id"],
            "cancelled_dispatches": cancelled_n})
    return job_commitments(conn, job_id)


# ---------------------------------------------------------------------------
# 批次召回、备品补制、柜台换货
# ---------------------------------------------------------------------------

def recall_batch(conn, batch_id: str, note: str | None = None,
                 **kwargs) -> dict:
    """库管把已登记纸批标记为永久召回。重复标记幂等返回。"""
    if note is None:
        note = kwargs.get("reason")
    # 永久召回是当前唯一支持的召回类型；接受 permanent=True 仅为调用兼容。
    kwargs.get("permanent", True)
    batch_id = (batch_id or "").strip()
    if not batch_id:
        raise DomainError("invalid_recall", "批次编号不能为空", 400)
    with WriteTx(conn) as c:
        existing = _get(c, "SELECT * FROM paper_batch_recalls "
                           "WHERE batch_id=?", (batch_id,))
        if existing is not None:
            row = dict(existing)
        else:
            registered = _get(
                c, "SELECT 1 FROM stock_arrivals WHERE batch_id=? LIMIT 1",
                (batch_id,))
            if registered is None:
                raise DomainError(
                    "batch_not_registered",
                    f"纸批 {batch_id} 尚未登记到货，不能标记召回；"
                    "请先由库管登记该纸批", 404)
            now = utcnow()
            c.execute(
                "INSERT INTO paper_batch_recalls(batch_id,status,note,"
                "recalled_at) VALUES (?,'recalled',?,?)",
                (batch_id, note, now))
            log_event(c, "paper_batch_recalled", {
                "batch_id": batch_id, "note": note, "permanent": True})
            row = dict(_get(c, "SELECT * FROM paper_batch_recalls "
                               "WHERE batch_id=?", (batch_id,)))
    return _recall_view(conn, row)


# 常见命名别名，便于不同调用方/测试意图直接命中。
mark_batch_recalled = recall_batch
recall_paper_batch = recall_batch


def _recall_view(conn, row: dict) -> dict:
    out = dict(row)
    out["recalled"] = True
    out["permanently_recalled"] = True
    out["permanent"] = True
    out["pending_copies"] = list_recall_pending(
        conn, batch_id=row["batch_id"])
    out["pending_count"] = len(out["pending_copies"])
    return out


def get_recall(conn, batch_id: str) -> dict:
    row = row_to_dict(_get(
        conn, "SELECT * FROM paper_batch_recalls WHERE batch_id=?",
        (batch_id,)))
    if row is None:
        raise DomainError("recall_not_found",
                          f"纸批 {batch_id} 没有召回记录", 404)
    return _recall_view(conn, row)


def list_recalls(conn) -> list[dict]:
    rows = _all(conn, "SELECT * FROM paper_batch_recalls "
                      "ORDER BY recalled_at, batch_id")
    return [_recall_view(conn, r) for r in rows]


def recall_dashboard(conn, job_id: str | None = None) -> dict:
    """一个柜台/库管总览：召回批、待收回名单、当前持有和换货记录。"""
    return {
        "recalls": list_recalls(conn),
        "pending": list_recall_pending(conn, job_id=job_id),
        "holdings": list_holdings(conn, job_id)["holdings"],
        "exchanges": list_copy_exchanges(conn, job_id),
        "spares": list_spare_copies(conn, job_id),
    }


def _normalize_batch_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, (list, tuple, set)):
        value = [value]
    return sorted({str(b).strip() for b in value if str(b).strip()})


def create_spare_copies(conn, *args, **kwargs) -> dict:
    """按原工单补制若干份共用备品并指定可用纸批。

    支持常见位置形式 ``(conn, job_id, count, [batches])`` 以及批次/份数
    参数顺序互换。备品仍创建普通 page_slots/dispatches/copy_commitments，
    因此照常领纸、派工、核对、补打和受库存承诺约束；但
    copy_kind='spare'，普通/授信入口不能交付，只能由换货事务交给原工单客户。
    """
    job_id = kwargs.pop("job_id", None) or kwargs.pop("job", None)
    pos = list(args)
    if pos and isinstance(pos[0], str) and job_id is None:
        job_id = pos.pop(0)

    copy_count = (kwargs.pop("copy_count", None)
                  or kwargs.pop("spare_count", None)
                  or kwargs.pop("copies_count", None)
                  or kwargs.pop("number_of_copies", None)
                  or kwargs.pop("quantity", None)
                  or kwargs.pop("copies", None)
                  or kwargs.pop("n", None)
                  or kwargs.pop("count", None))
    page_count = kwargs.pop("page_count", None) or kwargs.pop("pages", None)
    note = kwargs.pop("note", None)
    reason = kwargs.pop("reason", None)
    allowed_batches = kwargs.pop("allowed_batches", None)
    batch_id = kwargs.pop("batch_id", None)
    batch_ids = kwargs.pop("batch_ids", None)
    batches = kwargs.pop("batches", None)

    ints: list[int] = []
    for a in pos:
        if isinstance(a, int) and not isinstance(a, bool):
            ints.append(a)
        elif isinstance(a, str):
            # 单个字符串既可能是 job（已处理），也可能是单个批次。
            allowed_batches = [a] if allowed_batches is None \
                else _normalize_batch_list(allowed_batches) + [a]
        elif isinstance(a, (list, tuple, set)):
            allowed_batches = a if allowed_batches is None \
                else _normalize_batch_list(allowed_batches) \
                + _normalize_batch_list(a)
    if copy_count is None and ints:
        copy_count = ints[0]
    if page_count is None and len(ints) >= 2:
        page_count = ints[1]
    if copy_count is None:
        copy_count = 1
    try:
        copy_count = int(copy_count)
    except (TypeError, ValueError) as exc:
        raise DomainError("invalid_spare_count",
                          "备品份数必须是正整数", 400) from exc
    if copy_count <= 0:
        raise DomainError("invalid_spare_count",
                          "备品份数必须是正整数", 400)

    requested_batches = _normalize_batch_list(allowed_batches)
    for v in (batch_ids, batches):
        requested_batches += [b for b in _normalize_batch_list(v)
                              if b not in requested_batches]
    if batch_id:
        b = batch_id.strip() if isinstance(batch_id, str) else batch_id
        if b and b not in requested_batches:
            requested_batches.append(b)

    with WriteTx(conn) as c:
        job = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)))
        if job is None:
            raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
        if page_count is not None and int(page_count) != job["page_count"]:
            raise DomainError(
                "page_count_mismatch",
                f"备品必须与原工单同页数：原工单 {job['page_count']} 页，"
                f"请求 {page_count} 页", 400)
        if not _is_tracked(c, job_id):
            raise DomainError(
                "not_batch_tracked",
                f"工单 {job_id} 未纳入纸料追踪，不能按纸批补制召回备品；"
                "旧工单仍按原流程办理", 409)
        if not requested_batches:
            requested_batches = _allowed_batches(c, job_id)
        requested_batches = sorted(set(requested_batches))
        if not requested_batches:
            raise DomainError(
                "batches_required",
                "补制备品必须指定至少一个可用纸批", 400)
        recalled = _recalled_batch_ids(c)
        forbidden = [b for b in requested_batches if b in recalled]
        if forbidden:
            raise DomainError(
                "batch_recalled",
                f"不能指定已永久召回批次 {forbidden} 制作品格备品", 409)

        next_no = _get(
            c, "SELECT COALESCE(MAX(copy_no),0)+1 AS n FROM job_copies "
               "WHERE job_id=?", (job_id,))["n"]
        copy_nos = list(range(next_no, next_no + copy_count))

        # 与接单相同的整体可行性求解：不挤掉已接工单/已有份的原印承诺，
        # 并允许重排未开印份。
        planned = _planned_items(c)
        new_items = [{"key": (job_id, no),
                      "demand": job["page_count"],
                      "allowed": list(requested_batches)}
                     for no in copy_nos]
        assignment = _solve_assignment(
            _available_by_batch(c),
            _demand_by_batch(c, ("locked",)),
            planned + new_items)
        if assignment is None:
            raise DomainError(
                "insufficient_stock",
                f"备品 {copy_count} 份 × {job['page_count']} 页在指定批次 "
                f"{requested_batches} 中没有可行的整份安排", 409)

        now = utcnow()
        # 落实求解器对既有未开印份的重排。
        for (j, no), b in assignment.items():
            if (j, no) in {(job_id, n) for n in copy_nos}:
                continue
            cur = _get(c, "SELECT batch_id FROM copy_commitments "
                          "WHERE job_id=? AND copy_no=?", (j, no))
            if cur is not None and cur["batch_id"] != b:
                c.execute(
                    "UPDATE copy_commitments SET batch_id=? "
                    "WHERE job_id=? AND copy_no=?", (b, j, no))
                log_event(c, "copy_batch_reassigned", {
                    "job_id": j, "copy_no": no,
                    "from_batch": cur["batch_id"], "to_batch": b,
                    "reason": "spare_accept_replan", "by_job": job_id})

        for no in copy_nos:
            c.execute(
                "INSERT INTO job_copies(job_id,copy_no,copy_kind,status) "
                "VALUES (?,?,'spare','pending')", (job_id, no))
            for page_no in range(1, job["page_count"] + 1):
                c.execute(
                    "INSERT INTO page_slots(job_id,copy_no,page_no,status) "
                    "VALUES (?,?,?,'pending')",
                    (job_id, no, page_no))
                c.execute(
                    "INSERT INTO dispatches(dispatch_id,job_id,copy_no,"
                    "page_no,attempt_no,status,created_at) "
                    "VALUES (?,?,?,?,1,'queued',?)",
                    (_new_id("dsp"), job_id, no, page_no, now))
            for b in requested_batches:
                c.execute(
                    "INSERT OR IGNORE INTO copy_allowed_batches("
                    "job_id,copy_no,batch_id) VALUES (?,?,?)",
                    (job_id, no, b))
            batch_id = assignment[(job_id, no)]
            c.execute(
                "INSERT INTO copy_commitments(job_id,copy_no,status,"
                "batch_id) VALUES (?,?,'planned',?)",
                (job_id, no, batch_id))

        log_event(c, "spare_copies_created", {
            "job_id": job_id, "copy_nos": copy_nos,
            "copy_count": copy_count, "page_count": job["page_count"],
            "allowed_batches": requested_batches,
            "assignment": {str(no): assignment[(job_id, no)]
                           for no in copy_nos},
            "note": note or reason})

    spare_view = list_spare_copies(conn, job_id, copy_nos=copy_nos)
    rows = spare_view["spares"]
    return {"job_id": job_id, "copy_count": copy_count,
            "copy_nos": copy_nos,
            "spare_ids": [f"SPARE-{job_id}-{n}" for n in copy_nos],
            "spare_copies": rows,
            "spares": rows,
            "assignment": {str(no): assignment[(job_id, no)]
                           for no in copy_nos}}


# 常见命名别名。
create_spares = create_spare_copies
create_spare_copy = create_spare_copies
create_spares_for_job = create_spare_copies
produce_spare_copies = create_spare_copies
add_spare_copies = create_spare_copies


def list_spare_copies(conn, job_id: str | None = None, *,
                      copy_nos: list[int] | None = None,
                      only_available: bool = False) -> dict | list[dict]:
    sql = ("SELECT j.job_id, j.title, j.page_count, jc.copy_no, "
           "jc.status AS copy_status, jc.copy_kind, jc.paused_reason, "
           "cc.status AS commitment_status, cc.batch_id, "
           "cu.status AS custody_status, ce.request_id AS exchange_id "
           "FROM job_copies jc JOIN jobs j ON j.job_id=jc.job_id "
           "LEFT JOIN copy_commitments cc ON cc.job_id=jc.job_id "
           "AND cc.copy_no=jc.copy_no "
           "LEFT JOIN copy_custody cu ON cu.physical_job_id=jc.job_id "
           "AND cu.physical_copy_no=jc.copy_no "
           "LEFT JOIN copy_exchanges ce ON ce.spare_job_id=jc.job_id "
           "AND ce.spare_copy_no=jc.copy_no "
           "WHERE COALESCE(jc.copy_kind,'original')='spare'")
    params: list = []
    if job_id is not None:
        sql += " AND jc.job_id=?"
        params.append(job_id)
    if copy_nos:
        marks = ",".join("?" for _ in copy_nos)
        sql += f" AND jc.copy_no IN ({marks})"
        params.extend(copy_nos)
    if only_available:
        sql += " AND jc.status NOT IN ('delivered','cancelled') " \
               "AND ce.request_id IS NULL"
    sql += " ORDER BY j.job_id,jc.copy_no"
    rows = _all(conn, sql, params)
    for r in rows:
        r["spare_id"] = f"SPARE-{r['job_id']}-{r['copy_no']}"
        r["allowed_batches"] = _allowed_batches(
            conn, r["job_id"], r["copy_no"])
        try:
            _, slots = _check_deliverable(
                conn, {"job_id": r["job_id"],
                       "page_count": r["page_count"]},
                r["copy_no"], allow_spare=True)
            r["exchange_ready"] = True
            r["blocking_slots"] = []
        except DomainError as exc:
            r["exchange_ready"] = False
            r["readiness_error"] = exc.code
            slots = _all(
                conn, "SELECT page_no,status FROM page_slots "
                      "WHERE job_id=? AND copy_no=? AND status<>'verified'",
                (r["job_id"], r["copy_no"]))
            r["blocking_slots"] = slots
    if job_id is not None:
        return {"job_id": job_id, "spares": rows}
    return rows


def _current_custody(c, job_id: str, copy_no: int) -> dict | None:
    """按逻辑原交付份号或当前物理份号找唯一在手持有关系。"""
    row = _get(
        c,
        "SELECT * FROM copy_custody WHERE logical_job_id=? "
        "AND logical_copy_no=? AND status='with_customer'",
        (job_id, copy_no))
    if row is not None:
        return dict(row)
    row = _get(
        c,
        "SELECT * FROM copy_custody WHERE physical_job_id=? "
        "AND physical_copy_no=? AND status='with_customer'",
        (job_id, copy_no))
    if row is not None:
        return dict(row)
    # 传入的是已退回的旧物理份时，沿其 logical 指针找到当前在手份。
    old = _get(
        c,
        "SELECT * FROM copy_custody WHERE physical_job_id=? "
        "AND physical_copy_no=?", (job_id, copy_no))
    if old is not None:
        row = _get(
            c,
            "SELECT * FROM copy_custody WHERE logical_job_id=? "
            "AND logical_copy_no=? AND status='with_customer'",
            (old["logical_job_id"], old["logical_copy_no"]))
        if row is not None:
            return dict(row)
    return None


def list_holdings(conn, job_id: str | None = None, *,
                  include_returned: bool = False) -> dict:
    where = []
    params: list = []
    if not include_returned:
        where.append("cu.status='with_customer'")
    if job_id is not None:
        where.append("(cu.logical_job_id=? OR cu.physical_job_id=?)")
        params.extend([job_id, job_id])
    sql = ("SELECT cu.*, j.title, j.page_count, j.credit_account_id, "
           "j.price_per_copy FROM copy_custody cu "
           "JOIN jobs j ON j.job_id=cu.logical_job_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY cu.logical_job_id,cu.logical_copy_no," \
           "cu.physical_copy_no"
    rows = _all(conn, sql, params)
    for row in rows:
        ret_bad = _recalled_usage(
            conn, row["physical_job_id"], row["physical_copy_no"])
        row["current_paper_usage"] = _copy_paper_usage(
            conn, row["physical_job_id"], row["physical_copy_no"])
        row["recalled_usage"] = ret_bad
        row["recalled_batches"] = sorted(
            {u["batch_id"] for u in ret_bad if u.get("batch_id")})
        row["pending_recall"] = bool(ret_bad)
        row["exchange_count"] = _get(
            conn, "SELECT COUNT(*) AS n FROM copy_exchanges "
                  "WHERE job_id=? AND original_copy_no=?",
            (row["logical_job_id"], row["logical_copy_no"]))["n"]
    return {"holdings": rows}


def list_recall_pending(conn, batch_id: str | None = None,
                        job_id: str | None = None) -> list[dict]:
    """当前在手且实际用纸含永久召回批次的逻辑原交付份。

    换出合格备品后，当前 custody 指向新物理份；旧批次自然不再命中。备品
    日后被召回时，同一 logical 原交付份会用上次换出的纸再次出现。
    """
    holdings = list_holdings(conn, job_id)["holdings"]
    out = []
    for h in holdings:
        bad = h["recalled_usage"]
        if not bad:
            continue
        if batch_id is not None and not any(
                u.get("batch_id") == batch_id for u in bad):
            continue
        usage = h["current_paper_usage"]
        batches: dict[str, dict] = {}
        for u in bad:
            b = u["batch_id"]
            item = batches.setdefault(b, {
                "batch_id": b,
                "recalled_paper_ids": [],
                "recalled_page_numbers": []})
            item["recalled_paper_ids"].append(u["paper_id"])
            item["recalled_page_numbers"].append(u["page_no"])
        for b in batches.values():
            recall = _get(conn, "SELECT recalled_at,note FROM "
                               "paper_batch_recalls WHERE batch_id=?",
                          (b["batch_id"],))
            b["recalled_at"] = recall["recalled_at"] if recall else None
            b["note"] = recall["note"] if recall else None
        paper_ids = [u["paper_id"] for u in usage]
        out.append({
            "job_id": h["logical_job_id"],
            "title": h.get("title"),
            "credit_account_id": h.get("credit_account_id"),
            "original_copy_no": h["logical_copy_no"],
            "logical_job_id": h["logical_job_id"],
            "logical_copy_no": h["logical_copy_no"],
            "current_job_id": h["physical_job_id"],
            "current_copy_no": h["physical_copy_no"],
            "held_copy_no": h["physical_copy_no"],
            "physical_job_id": h["physical_job_id"],
            "physical_copy_no": h["physical_copy_no"],
            "status": "pending_return",
            "pending_reason": (
                "客户当前在手份使用了永久召回纸批："
                + ",".join(sorted({u['batch_id'] for u in bad
                                    if u.get('batch_id')}))),
            "recalled_batches": list(batches.values()),
            "recalled_paper_ids": [u["paper_id"] for u in bad],
            "delivery_items": usage,
            "paper_ids": paper_ids,
            "expected_recovered_paper_ids": paper_ids,
            "exchange_count": h["exchange_count"],
        })
    return out


# 常见查询别名。
recall_pending_copies = list_recall_pending
pending_recalls = list_recall_pending
list_pending_returns = list_recall_pending


def get_copy_recall_view(conn, job_id: str, copy_no: int) -> dict:
    """区分当前在手物理份、召回原因、交付清单和历次换货。"""
    job = row_to_dict(_get(conn, "SELECT * FROM jobs WHERE job_id=?",
                           (job_id,)))
    if job is None:
        raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
    with WriteTx(conn) as c:
        custody = _current_custody(c, job_id, copy_no)
    if custody is None:
        # 可能是从未交付的份/备品：返回状态而不是 404，便于柜台查询。
        cp = row_to_dict(_get(
            conn, "SELECT * FROM job_copies WHERE job_id=? AND copy_no=?",
            (job_id, copy_no)))
        if cp is None:
            raise DomainError("copy_not_found", "份不存在", 404)
        return {"job_id": job_id, "query_copy_no": copy_no,
                "in_hand": False, "current_holding": None,
                "pending_recall": False, "recall_reasons": [],
                "exchanges": list_copy_exchanges(conn, job_id, copy_no)}
    logical_job = custody["logical_job_id"]
    logical_no = custody["logical_copy_no"]
    pending = [p for p in list_recall_pending(conn, job_id=logical_job)
               if p["logical_copy_no"] == logical_no]
    exchanges = list_copy_exchanges(conn, logical_job, logical_no)
    current_list = delivery_list(
        conn, custody["physical_job_id"], custody["physical_copy_no"])
    original_list = delivery_list(conn, logical_job, logical_no)
    return {
        "job_id": logical_job,
        "original_copy_no": logical_no,
        "query_copy_no": copy_no,
        "in_hand": custody["status"] == "with_customer",
        "current_holding": custody,
        "current_delivery_list": current_list["items"],
        "original_delivery_list": original_list["items"],
        "pending_recall": bool(pending),
        "recall_reasons": pending,
        "exchanges": exchanges,
        "exchange_count": len(exchanges),
    }


copy_recall_status = get_copy_recall_view
copy_history = get_copy_recall_view


def current_delivery_list(conn, job_id: str, copy_no: int) -> dict:
    """按逻辑原交付份号或物理份号返回客户当前持有份的冻结清单。"""
    custody = _current_custody(conn, job_id, copy_no)
    if custody is None:
        # 兼容尚未发生换货、但 custody 回填缺失的极端旧库。
        items = delivery_list(conn, job_id, copy_no)
        if items["items"]:
            return {**items, "current_job_id": job_id,
                    "current_copy_no": copy_no}
        raise DomainError("holding_not_found",
                          "该份当前不在客户手中", 404)
    result = delivery_list(
        conn, custody["physical_job_id"], custody["physical_copy_no"])
    result.update({
        "logical_job_id": custody["logical_job_id"],
        "logical_copy_no": custody["logical_copy_no"],
        "current_job_id": custody["physical_job_id"],
        "current_copy_no": custody["physical_copy_no"],
        "custody": custody})
    return result


def _exchange_payload_kwargs(args, kwargs) -> dict:
    """把多种符合业务语义的位置/关键字调用归一化。"""
    kw = dict(kwargs)
    out = {
        "request_id": kw.pop("request_id", None)
        or kw.pop("exchange_request_id", None)
        or kw.pop("request_no", None)
        or kw.pop("exchange_request_no", None)
        or kw.pop("idempotency_key", None)
        or kw.pop("exchange_no", None)
        or kw.pop("exchange_id", None),
        "job_id": kw.pop("job_id", None) or kw.pop("job", None),
        "original_copy_no": kw.pop("original_copy_no", None)
        or kw.pop("copy_no", None) or kw.pop("original_copy", None)
        or kw.pop("original_copy_id", None) or kw.pop("copy_id", None)
        or kw.pop("copy", None),
        "returned_copy_no": kw.pop("returned_copy_no", None)
        or kw.pop("held_copy_no", None)
        or kw.pop("current_copy_no", None)
        or kw.pop("from_copy_no", None),
        "spare_copy_no": kw.pop("spare_copy_no", None)
        or kw.pop("spare_no", None) or kw.pop("spare_copy", None)
        or kw.pop("spare_id", None) or kw.pop("spare_copy_id", None)
        or kw.pop("spare", None),
        "recovered_paper_ids": kw.pop("recovered_paper_ids", None)
        or kw.pop("returned_paper_ids", None)
        or kw.pop("recovered_papers", None)
        or kw.pop("papers", None)
        or kw.pop("paper_ids", None)
        or kw.pop("returned_papers", None)
        or kw.pop("collected_paper_ids", None),
        "spare_job_id": kw.pop("spare_job_id", None),
    }
    args = list(args)
    ints: list[int] = []
    strings: list[str] = []
    papers = None
    for a in args:
        if isinstance(a, (list, tuple, set)):
            papers = list(a)
        elif isinstance(a, int) and not isinstance(a, bool):
            ints.append(a)
        elif isinstance(a, str):
            strings.append(a)
    if out["recovered_paper_ids"] is None and papers is not None:
        out["recovered_paper_ids"] = papers
    # 常见位置形式：
    # (job_id, original_copy_no, [papers], spare_copy_no, request_id)
    # (request_id, job_id, original_copy_no, spare_copy_no, [papers])
    if out["job_id"] is None and strings:
        # 无法仅靠字符串区分 job/request；若第一个位置参数是字符串且第二
        # 个也是字符串，按 request_id,job_id 处理，否则第一个是 job_id。
        if args and isinstance(args[0], str) and len(args) >= 2 \
                and isinstance(args[1], str):
            out["request_id"] = out["request_id"] or args[0]
            out["job_id"] = args[1]
        else:
            out["job_id"] = strings[0]
            if out["request_id"] is None and len(strings) >= 2:
                out["request_id"] = strings[-1]
    elif out["request_id"] is None and strings:
        out["request_id"] = strings[0]
    if out["original_copy_no"] is None and ints:
        out["original_copy_no"] = ints[0]
    if out["spare_copy_no"] is None and len(ints) >= 2:
        out["spare_copy_no"] = ints[1]
    return out


def exchange_copy(conn, *args, **kwargs) -> dict:
    """柜台原子换货：收回当前待召回整份 + 交出合格备品，同请求号幂等。"""
    p = _exchange_payload_kwargs(args, kwargs)
    request_id = (p["request_id"] or "").strip() \
        if isinstance(p["request_id"], str) else p["request_id"]
    job_id = (p["job_id"] or "").strip() \
        if isinstance(p["job_id"], str) else p["job_id"]
    original_input = p["original_copy_no"]
    spare_copy_no = p["spare_copy_no"]
    returned_copy_no = p["returned_copy_no"]
    spare_job_id = p["spare_job_id"] or job_id
    raw_papers = p["recovered_paper_ids"]

    if not request_id or not isinstance(request_id, str):
        raise DomainError("request_id_required",
                          "换货必须提交换货请求号", 400)
    if not job_id or not isinstance(job_id, str):
        raise DomainError("job_id_required", "换货必须指定原工单", 400)
    try:
        original_input = _coerce_copy_no(original_input)
        spare_copy_no = _coerce_copy_no(spare_copy_no)
    except (TypeError, ValueError) as exc:
        raise DomainError("invalid_copy_no",
                          "原交付份号与备品份号必须是整数", 400) from exc
    if returned_copy_no is not None:
        returned_copy_no = _coerce_copy_no(returned_copy_no)
    if spare_job_id != job_id:
        raise DomainError(
            "spare_job_mismatch",
            "当前实现的备品均补制在原工单下；不能选择其他工单的备品", 409)
    if not isinstance(raw_papers, (list, tuple, set)) or not raw_papers:
        raise DomainError(
            "recovered_papers_required",
            "必须提交现场收回的整份纸张编号（非空数组）", 400)
    def _paper_id_value(item):
        if isinstance(item, dict):
            return item.get("paper_id") or item.get("id") \
                or item.get("paperId")
        return item

    recovered_ids = [str(_paper_id_value(x)).strip()
                     for x in raw_papers
                     if str(_paper_id_value(x) or "").strip()]
    if len(recovered_ids) != len(set(recovered_ids)):
        raise DomainError("duplicate_paper_id",
                          "收回纸张编号不能重复", 400)

    paper_set = sorted(set(recovered_ids))
    now = utcnow()

    with WriteTx(conn) as c:
        old = row_to_dict(_get(
            c, "SELECT * FROM copy_exchanges WHERE request_id=?",
            (request_id,)))
        if old is not None:
            old_papers = json.loads(old["recovered_paper_ids"])
            same_core = (
                old["job_id"] == job_id
                and (old["original_copy_no"] == original_input
                     # 也允许重送时传当前物理份号而不是逻辑原份号。
                     or old["returned_copy_no"] == original_input)
                and old["spare_job_id"] == spare_job_id
                and old["spare_copy_no"] == spare_copy_no
                and old_papers == paper_set
                and (returned_copy_no is None
                     or returned_copy_no == old["returned_copy_no"])
            )
            if not same_core:
                raise DomainError(
                    "request_id_conflict",
                    f"换货请求号 {request_id} 已用于不同的换货内容；"
                    "同号不能改换工单、份号、纸号或备品", 409)
            snapshot = json.loads(old["result_json"])
            snapshot["idempotent_replay"] = True
            snapshot["replayed"] = True
            return snapshot

        job = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)))
        if job is None:
            raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
        if not _is_tracked(c, job_id):
            raise DomainError(
                "not_batch_tracked",
                f"工单 {job_id} 未纳入纸料追踪，不存在本功能的批次召回"
                "待收回换货；旧工单照旧办理", 409)

        custody = _current_custody(c, job_id, original_input)
        if custody is None:
            raise DomainError(
                "copy_not_in_hand",
                f"工单 {job_id} 份 {original_input} 没有当前在客户手中的"
                "交付记录，不能换货收回", 409)
        logical_job = custody["logical_job_id"]
        logical_copy_no = custody["logical_copy_no"]
        ret_job_id = custody["physical_job_id"]
        ret_copy_no = custody["physical_copy_no"]
        if logical_job != job_id:
            raise DomainError("job_mismatch", "当前持有份不属于该工单", 409)
        if returned_copy_no is not None \
                and returned_copy_no != ret_copy_no:
            raise DomainError(
                "returned_copy_mismatch",
                f"请求收回份 {returned_copy_no} 不是客户当前在手份 "
                f"{ret_copy_no}", 409)

        recalled_bad = _recalled_usage(c, ret_job_id, ret_copy_no)
        if not recalled_bad:
            raise DomainError(
                "copy_not_pending_recall",
                f"工单 {job_id} 当前在手物理份 {ret_copy_no} 没有使用任何"
                "已召回批次，不在待收回名单中，不能走召回换货", 409)

        old_items = _all(
            c, "SELECT * FROM delivery_items WHERE job_id=? AND copy_no=? "
               "ORDER BY page_no", (ret_job_id, ret_copy_no))
        if not old_items:
            raise DomainError(
                "delivery_list_not_found",
                "当前在手份没有冻结的交付清单，无法核对收回纸号", 409)
        expected_ids = sorted(i["paper_id"] for i in old_items)
        if paper_set != expected_ids or len(recovered_ids) != len(old_items):
            raise DomainError(
                "recovered_paper_mismatch",
                "必须按交付清单收回客户当前持有的一整份；"
                f"应交回纸号 {expected_ids}，实际提交 {paper_set}", 409)

        spare_cp = row_to_dict(_get(
            c, "SELECT * FROM job_copies WHERE job_id=? AND copy_no=?",
            (spare_job_id, spare_copy_no)))
        if spare_cp is None:
            raise DomainError("spare_not_found",
                              f"备品份 {spare_job_id}#{spare_copy_no} 不存在",
                              404)
        if spare_cp.get("copy_kind") != "spare":
            raise DomainError(
                "not_a_spare_copy",
                f"工单 {job_id} 份 {spare_copy_no} 不是补制备品份", 409)
        prior_use = _get(
            c, "SELECT request_id FROM copy_exchanges "
               "WHERE spare_job_id=? AND spare_copy_no=?",
            (spare_job_id, spare_copy_no))
        if prior_use is not None:
            raise DomainError(
                "spare_already_used",
                f"备品份 {spare_copy_no} 已在换货 {prior_use['request_id']} "
                "中交给客户，不能第二次使用", 409)

        # 原有整份交付条件 + 实际批次未召回，均在同一写事务内复查。
        _, slots = _check_deliverable(
            c, job, spare_copy_no, allow_spare=True)
        delivered = _record_delivery(
            c, spare_job_id, spare_copy_no, slots,
            request_id=request_id, exchange_id=request_id)

        cur = c.execute(
            "UPDATE copy_custody SET status='returned', returned_at=?, "
            "return_exchange_id=? WHERE physical_job_id=? "
            "AND physical_copy_no=? AND status='with_customer'",
            (now, request_id, ret_job_id, ret_copy_no))
        if cur.rowcount != 1:
            raise DomainError(
                "return_concurrent_conflict",
                "当前在手份已被其他换货事务收回", 409)
        cur = c.execute(
            "UPDATE copy_custody SET logical_job_id=?, logical_copy_no=?, "
            "out_exchange_id=? WHERE physical_job_id=? "
            "AND physical_copy_no=? AND status='with_customer'",
            (logical_job, logical_copy_no, request_id,
             spare_job_id, spare_copy_no))
        if cur.rowcount != 1:
            raise DomainError(
                "spare_custody_conflict",
                "备品持有关系更新失败", 409)

        recalled_batches = sorted(
            {u["batch_id"] for u in recalled_bad if u.get("batch_id")})
        result = {
            "request_id": request_id,
            "exchange_id": request_id,
            "status": "completed",
            "job_id": job_id,
            "original_copy_no": logical_copy_no,
            "logical_job_id": logical_job,
            "logical_copy_no": logical_copy_no,
            "submitted_copy_no": original_input,
            "returned_job_id": ret_job_id,
            "returned_copy_no": ret_copy_no,
            "recovered_paper_ids": paper_set,
            "returned_delivery_items": old_items,
            "recalled_batches": recalled_batches,
            "spare_job_id": spare_job_id,
            "spare_copy_no": spare_copy_no,
            "spare_id": f"SPARE-{spare_job_id}-{spare_copy_no}",
            "spare_delivery_items": delivered["items"],
            "items": delivered["items"],
            "delivered_at": now,
            "current_job_id": spare_job_id,
            "current_copy_no": spare_copy_no,
            "idempotent_replay": False,
        }
        fingerprint = json.dumps({
            "request_id": request_id, "job_id": job_id,
            "original_copy_no": logical_copy_no,
            "returned_job_id": ret_job_id,
            "returned_copy_no": ret_copy_no,
            "spare_job_id": spare_job_id,
            "spare_copy_no": spare_copy_no,
            "papers": paper_set}, sort_keys=True, separators=(",", ":"))
        c.execute(
            "INSERT INTO copy_exchanges(request_id,job_id,"
            "original_copy_no,returned_job_id,returned_copy_no,"
            "spare_job_id,spare_copy_no,recovered_paper_ids,"
            "request_fingerprint,result_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, job_id, logical_copy_no, ret_job_id,
             ret_copy_no, spare_job_id, spare_copy_no,
             json.dumps(paper_set, ensure_ascii=False), fingerprint,
             json.dumps(result, ensure_ascii=False, sort_keys=True), now))
        log_event(c, "copy_exchanged", {
            "request_id": request_id, "job_id": job_id,
            "original_copy_no": logical_copy_no,
            "returned_copy_no": ret_copy_no,
            "recovered_paper_ids": paper_set,
            "spare_copy_no": spare_copy_no})
        return result


counter_exchange = exchange_copy
create_exchange = exchange_copy
submit_exchange = exchange_copy


def get_exchange(conn, request_id: str) -> dict:
    row = row_to_dict(_get(
        conn, "SELECT * FROM copy_exchanges WHERE request_id=?",
        (request_id,)))
    if row is None:
        raise DomainError("exchange_not_found",
                          f"换货请求 {request_id} 不存在", 404)
    result = json.loads(row["result_json"])
    result["record"] = {k: v for k, v in row.items() if k != "result_json"}
    return result


def list_copy_exchanges(conn, job_id: str | None = None,
                        copy_no: int | None = None) -> list[dict]:
    sql = "SELECT * FROM copy_exchanges"
    params: list = []
    clauses = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(job_id)
    if copy_no is not None:
        clauses.append("(original_copy_no=? OR returned_copy_no=? "
                       "OR spare_copy_no=?)")
        params.extend([copy_no, copy_no, copy_no])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at,rowid"
    rows = _all(conn, sql, params)
    out = []
    for row in rows:
        item = json.loads(row["result_json"])
        item["record"] = {k: v for k, v in row.items()
                          if k not in ("result_json",)}
        out.append(item)
    return out


# 查询函数的常见命名别名。
list_exchanges = list_copy_exchanges
list_exchange_requests = list_copy_exchanges
exchange_history = list_copy_exchanges
get_copy_history = get_copy_recall_view
copy_holdings = list_holdings
current_holdings = list_holdings
customer_holdings = list_holdings
pending_returns = list_recall_pending
recall_list = list_recalls
list_recall_records = list_recalls
recalled_batches = list_recalls
list_recalled_batches = list_recalls
list_recall_candidates = list_recall_pending
recall_candidates = list_recall_pending
recall_pending = list_recall_pending
pending_recall = list_recall_pending
list_recall_queue = list_recall_pending
list_spares = list_spare_copies


def list_available_spares(conn, job_id: str | None = None):
    return list_spare_copies(conn, job_id, only_available=True)


make_spare_copies = create_spare_copies
make_spare_copy = create_spare_copies
prepare_spare_copies = create_spare_copies
register_spare_copies = create_spare_copies
perform_copy_exchange = exchange_copy
request_copy_exchange = exchange_copy
get_exchange_result = get_exchange


# ---------------------------------------------------------------------------
# 查询视图
# ---------------------------------------------------------------------------

def list_papers(conn, job_id: str | None = None) -> list[dict]:
    if job_id:
        return _all(conn,
                    "SELECT * FROM papers WHERE job_id=? "
                    "ORDER BY copy_no,page_no,paper_id", (job_id,))
    return _all(conn,
                "SELECT * FROM papers ORDER BY job_id,copy_no,page_no")


def list_dispatches(conn, job_id: str | None = None) -> list[dict]:
    if job_id:
        return _all(conn,
                    "SELECT * FROM dispatches WHERE job_id=? "
                    "ORDER BY copy_no,page_no,attempt_no", (job_id,))
    return _all(conn, "SELECT * FROM dispatches ORDER BY job_id,copy_no,"
                      "page_no,attempt_no")


def pending_onsite(conn, job_id: str | None = None) -> list[dict]:
    """待现场核对项：未知/候选/矛盾/废页/未出纸的槽位与派工。"""
    sql = (
        "SELECT s.job_id,s.copy_no,s.page_no,s.status AS slot_status,"
        "s.conflict_note,d.dispatch_id,d.attempt_no,d.status AS "
        "dispatch_status,d.agent_id, "
        "(SELECT GROUP_CONCAT(paper_id||':'||state) FROM papers p "
        " WHERE p.job_id=s.job_id AND p.copy_no=s.copy_no "
        "AND p.page_no=s.page_no) AS papers "
        "FROM page_slots s JOIN dispatches d ON d.job_id=s.job_id "
        "AND d.copy_no=s.copy_no AND d.page_no=s.page_no "
        "AND d.attempt_no=(SELECT MAX(attempt_no) FROM dispatches d2 "
        " WHERE d2.job_id=s.job_id AND d2.copy_no=s.copy_no "
        "AND d2.page_no=s.page_no) "
        "WHERE s.status <> 'verified' "
        "AND NOT EXISTS (SELECT 1 FROM job_copies jc "
        " WHERE jc.job_id=s.job_id AND jc.copy_no=s.copy_no "
        " AND jc.status='cancelled') "
    )
    params: list = []
    if job_id:
        sql += " AND s.job_id=?"
        params.append(job_id)
    sql += " ORDER BY s.job_id,s.copy_no,s.page_no"
    return _all(conn, sql, params)


def list_investigations(conn, job_id: str | None = None) -> list[dict]:
    if job_id:
        return _all(conn,
                    "SELECT * FROM investigations WHERE job_id=? "
                    "ORDER BY id", (job_id,))
    return _all(conn, "SELECT * FROM investigations ORDER BY id")


def event_log(conn, limit: int = 200) -> list[dict]:
    return _all(conn,
                "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,))
