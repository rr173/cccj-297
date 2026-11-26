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
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .db import WriteTx, log_event, row_to_dict, utcnow


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
    if copy["status"] == "delivered":
        return copy["status"]  # 已交付不回改
    if copy["status"] == "investigating":
        return copy["status"]

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
        "'investigating')",
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
# 工单与派工创建
# ---------------------------------------------------------------------------

def create_job(conn, job_id: str, title: str, page_count: int,
               copy_count: int) -> dict:
    if page_count <= 0 or copy_count <= 0:
        raise DomainError("invalid_job", "页数与份数必须为正整数", 400)
    with WriteTx(conn) as c:
        if _get(c, "SELECT 1 FROM jobs WHERE job_id=?", (job_id,)):
            raise DomainError("job_exists", f"工单 {job_id} 已存在")
        now = utcnow()
        c.execute(
            "INSERT INTO jobs(job_id,title,page_count,copy_count,created_at)"
            " VALUES (?,?,?,?,?)",
            (job_id, title, page_count, copy_count, now),
        )
        for copy_no in range(1, copy_count + 1):
            c.execute(
                "INSERT INTO job_copies(job_id,copy_no,status) VALUES (?,?,?)",
                (job_id, copy_no, "pending"),
            )
            for page_no in range(1, page_count + 1):
                c.execute(
                    "INSERT INTO page_slots(job_id,copy_no,page_no,status)"
                    " VALUES (?,?,?,'pending')",
                    (job_id, copy_no, page_no),
                )
                dispatch_id = _new_id("dsp")
                c.execute(
                    "INSERT INTO dispatches(dispatch_id,job_id,copy_no,"
                    "page_no,attempt_no,status,created_at) "
                    "VALUES (?,?,?,?,1,'queued',?)",
                    (dispatch_id, job_id, copy_no, page_no, now),
                )
        log_event(c, "job_created", {
            "job_id": job_id, "title": title,
            "page_count": page_count, "copy_count": copy_count,
        })
    return get_job(conn, job_id)


def get_job(conn, job_id: str) -> dict:
    job = row_to_dict(_get(conn, "SELECT * FROM jobs WHERE job_id=?",
                           (job_id,)))
    if job is None:
        raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
    job["copies"] = _all(
        conn, "SELECT * FROM job_copies WHERE job_id=? ORDER BY copy_no",
        (job_id,))
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


def claim_dispatch(conn, agent_id: str) -> dict | None:
    """领取一个最早的 queued 派工（FIFO）。

    关键：只有 queued 派工可被领取。claimed 派工在收到回执或停用确认
    之前不会再次入队，因此失联/超时不会产生重复派工。
    """
    with WriteTx(conn) as c:
        if _get(c, "SELECT 1 FROM agents WHERE agent_id=? AND active=1",
                (agent_id,)) is None:
            raise DomainError("agent_not_active",
                              f"代理 {agent_id} 未登记或已停用", 409)
        row = _get(
            c,
            "SELECT * FROM dispatches WHERE status='queued' "
            "ORDER BY created_at, rowid LIMIT 1",
        )
        if row is None:
            return None
        d = dict(row)
        c.execute(
            "UPDATE dispatches SET status='claimed', agent_id=?, "
            "claimed_at=? WHERE dispatch_id=? AND status='queued'",
            (agent_id, utcnow(), d["dispatch_id"]),
        )
        if c.execute(
                "SELECT changes()").fetchone()[0] != 1:
            raise DomainError("claim_lost", "派工已被其他代理领取", 409)
        c.execute("UPDATE agents SET last_seen_at=? WHERE agent_id=?",
                  (utcnow(), agent_id))
        log_event(c, "dispatch_claimed", {
            "dispatch_id": d["dispatch_id"], "agent_id": agent_id,
            "job_id": d["job_id"], "copy_no": d["copy_no"],
            "page_no": d["page_no"], "attempt_no": d["attempt_no"],
        })
        return _dispatch(c, d["dispatch_id"])


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
        slots = _all(
            conn, "SELECT * FROM page_slots WHERE job_id=? AND copy_no=? "
                  "ORDER BY page_no", (job_id, cp["copy_no"]))
        blocking = [
            {"page_no": s["page_no"], "status": s["status"],
             "note": s["conflict_note"]}
            for s in slots if s["status"] != "verified"]
        result.append({
            "copy_no": cp["copy_no"], "status": cp["status"],
            "paused_reason": cp["paused_reason"],
            "ready": not blocking, "blocking_slots": blocking})
    return result


def confirm_delivery(conn, job_id: str, copy_no: int) -> dict:
    with WriteTx(conn) as c:
        job = row_to_dict(_get(c, "SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)))
        if job is None:
            raise DomainError("job_not_found", f"工单 {job_id} 不存在", 404)
        cp = row_to_dict(_get(
            c, "SELECT * FROM job_copies WHERE job_id=? AND copy_no=?",
            (job_id, copy_no)))
        if cp is None:
            raise DomainError("copy_not_found", "份不存在", 404)
        if cp["status"] == "delivered":
            raise DomainError("already_delivered", "该份已交付")
        slots = _all(
            c, "SELECT * FROM page_slots WHERE job_id=? AND copy_no=? "
              "ORDER BY page_no", (job_id, copy_no))
        if len(slots) != job["page_count"]:
            raise DomainError("slot_count_mismatch", "槽位不完整")
        bad = [s for s in slots if s["status"] != "verified"
               or not s["retained_paper_id"]]
        if bad:
            raise DomainError(
                "not_deliverable",
                "只有整份每页都明确且有效（verified）才可交付；"
                f"未满足页：{[s['page_no'] for s in bad]}")
        now = utcnow()
        items = []
        for s in slots:
            paper = _paper(c, s["retained_paper_id"])
            c.execute(
                "INSERT INTO delivery_items(job_id,copy_no,page_no,"
                "paper_id,dispatch_id,delivered_at) VALUES (?,?,?,?,?,?)",
                (job_id, copy_no, s["page_no"], s["retained_paper_id"],
                 paper["dispatch_id"], now))
            items.append({"copy_no": copy_no, "page_no": s["page_no"],
                          "paper_id": s["retained_paper_id"],
                          "dispatch_id": paper["dispatch_id"]})
        c.execute(
            "UPDATE job_copies SET status='delivered', paused_reason=NULL "
            "WHERE job_id=? AND copy_no=?", (job_id, copy_no))
        if not _get(c, "SELECT 1 FROM job_copies WHERE job_id=? AND "
                       "status<>'delivered'", (job_id,)):
            c.execute("UPDATE jobs SET status='delivered' WHERE job_id=?",
                      (job_id,))
        log_event(c, "delivery_confirmed", {
            "job_id": job_id, "copy_no": copy_no, "items": items})
        return {"job_id": job_id, "copy_no": copy_no,
                "delivered_at": now, "items": items}


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
