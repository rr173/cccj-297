"""模拟财务服务领域逻辑：企业共享额度的锁定/扣费/释放。

核心保证
========
1. 同一企业的不同工单共用同一账户额度；可用额度 =
   初始额度 − 全部 held 的锁定额 − 全部 settled 的净扣费。
2. ``hold`` 按 request_id 幂等：同号重送绝不重复锁额；request_id
   已用于其他账户/金额的请求时明确拒绝（冲突），不做静默复用。
3. ``hold`` 成功即把这笔钱从可用额度中划走并持久化：其他取件不能
   再动用这部分额度，后续 settle 不需要企业再补钱。
4. ``settle`` 只接受 held → settled（同 request_id、同金额），
   台账表现为 hold + settle 各一条、净扣费恰好一次；对已 settled
   的请求重放为幂等成功。
5. ``release`` 把 held 还原为可用并记 voided（未产生净费用）；
   已 settled 的钱不退（交付后的纸张矛盾只走印制侧追查，不自动退钱）。
6. 所有资金动作只追加 ``fund_ledger``，不修改/删除既有台账行；
   净费用 = settle 合计 − release 合计（release 只冲销 hold，
   不触碰已 settle 的钱）。
"""
from __future__ import annotations

from .db import WriteTx, log_event, utcnow


class FinanceError(Exception):
    """4xx 业务错误，携带稳定错误码。"""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _get(conn, sql: str, params=()):
    return conn.execute(sql, params).fetchone()


def _all(conn, sql: str, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _account_row(c, account_id: str, must_exist: bool = True):
    row = _get(c, "SELECT * FROM enterprise_accounts WHERE account_id=?",
               (account_id,))
    if row is None and must_exist:
        raise FinanceError("account_not_found",
                           f"企业账户 {account_id} 不存在", 404)
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 账户管理
# ---------------------------------------------------------------------------

def create_account(conn, account_id: str, credit_limit: int,
                   name: str | None = None) -> dict:
    """登记企业账户与初始额度（整数分）。已存在则报错，不覆盖额度。"""
    if credit_limit < 0:
        raise FinanceError("invalid_limit", "初始额度不能为负", 400)
    with WriteTx(conn) as c:
        if _get(c, "SELECT 1 FROM enterprise_accounts WHERE account_id=?",
                (account_id,)) is not None:
            raise FinanceError("account_exists",
                               f"企业账户 {account_id} 已存在")
        c.execute(
            "INSERT INTO enterprise_accounts(account_id,name,credit_limit,"
            "created_at) VALUES (?,?,?,?)",
            (account_id, name, credit_limit, utcnow()))
        log_event(c, "account_created", {
            "account_id": account_id, "credit_limit": credit_limit,
            "name": name})
    return get_account(conn, account_id)


def _totals(c, account_id: str) -> dict:
    held = _get(
        c, "SELECT COALESCE(SUM(amount),0) AS n FROM fund_requests "
           "WHERE account_id=? AND status='held'", (account_id,))["n"]
    settled = _get(
        c, "SELECT COALESCE(SUM(amount),0) AS n FROM fund_requests "
           "WHERE account_id=? AND status='settled'", (account_id,))["n"]
    return {"held": held, "settled": settled}


def get_account(conn, account_id: str) -> dict:
    """账户视图：初始额度、待清算锁定、已扣费、当前可用。"""
    with WriteTx(conn) as c:
        acc = _account_row(c, account_id)
        totals = _totals(c, account_id)
    available = acc["credit_limit"] - totals["held"] - totals["settled"]
    return {
        "account_id": account_id, "name": acc["name"],
        "credit_limit": acc["credit_limit"],
        "held_pending": totals["held"],
        "settled_charges": totals["settled"],
        "available": available,
    }


def list_accounts(conn) -> list[dict]:
    ids = [r["account_id"] for r in conn.execute(
        "SELECT account_id FROM enterprise_accounts ORDER BY account_id")]
    return [get_account(conn, a) for a in ids]


# ---------------------------------------------------------------------------
# 资金处理：hold / settle / release（全部按 request_id 幂等）
# ---------------------------------------------------------------------------

def hold(conn, request_id: str, account_id: str, amount: int,
         reference: str | None = None, note: str | None = None) -> dict:
    """为一笔取件锁定额度。

    成功后该 request_id 处于 held：钱已确定地留给这笔取件，其他取件
    无法再动用；额度不足则拒绝且不产生任何台账。
    """
    if amount < 0:
        raise FinanceError("invalid_amount", "金额必须是非负整数分", 400)
    with WriteTx(conn) as c:
        acc = _account_row(c, account_id)
        existing = _get(c, "SELECT * FROM fund_requests WHERE request_id=?",
                        (request_id,))
        if existing is not None:
            ex = dict(existing)
            if ex["account_id"] != account_id or ex["amount"] != amount:
                raise FinanceError(
                    "request_id_conflict",
                    f"取件请求号 {request_id} 已用于账户 "
                    f"{ex['account_id']} 的 {ex['amount']} 分请求，"
                    "不能复用于其他取件", 409)
            # 同号同参重送：幂等返回既有状态，不再锁一次钱
            return _request_view(c, ex)
        totals = _totals(c, account_id)
        available = acc["credit_limit"] - totals["held"] - totals["settled"]
        if amount > available:
            log_event(c, "hold_rejected_insufficient_credit", {
                "request_id": request_id, "account_id": account_id,
                "amount": amount, "available": available})
            raise FinanceError(
                "insufficient_credit",
                f"企业账户 {account_id} 可用额度不足：需要 {amount} 分，"
                f"当前可用 {available} 分（初始 {acc['credit_limit']}，"
                f"待清算锁定 {totals['held']}，已扣费 {totals['settled']}）",
                409)
        now = utcnow()
        c.execute(
            "INSERT INTO fund_requests(request_id,account_id,amount,status,"
            "reference,note,held_at,created_at,updated_at) "
            "VALUES (?,?,?,'held',?,?,?,?,?)",
            (request_id, account_id, amount, reference, note, now, now, now))
        c.execute(
            "INSERT INTO fund_ledger(request_id,account_id,kind,amount,"
            "created_at) VALUES (?,?, 'hold', ?,?)",
            (request_id, account_id, amount, now))
        log_event(c, "credit_held", {
            "request_id": request_id, "account_id": account_id,
            "amount": amount, "reference": reference})
        return _request_view(
            c, dict(_get(c, "SELECT * FROM fund_requests WHERE request_id=?",
                         (request_id,))))


def settle(conn, request_id: str, amount: int | None = None) -> dict:
    """确认扣费：held → settled。已 settled 的重放为幂等成功。"""
    with WriteTx(conn) as c:
        row = _get(c, "SELECT * FROM fund_requests WHERE request_id=?",
                   (request_id,))
        if row is None:
            raise FinanceError(
                "request_not_found",
                f"取件请求 {request_id} 在财务侧无记录，无法清算；"
                "请先用同一请求号锁定额度", 404)
        req = dict(row)
        if amount is not None and amount != req["amount"]:
            raise FinanceError(
                "amount_mismatch",
                f"请求 {request_id} 锁定金额为 {req['amount']} 分，"
                f"清算金额 {amount} 分不一致", 409)
        if req["status"] == "settled":
            return _request_view(c, req)      # 重放：不重复扣费
        if req["status"] != "held":
            raise FinanceError(
                "request_not_held",
                f"请求 {request_id} 状态为 {req['status']}，"
                "只有待清算（held）请求可以确认扣费", 409)
        now = utcnow()
        c.execute(
            "UPDATE fund_requests SET status='settled', settled_at=?, "
            "updated_at=? WHERE request_id=?", (now, now, request_id))
        c.execute(
            "INSERT INTO fund_ledger(request_id,account_id,kind,amount,"
            "created_at) VALUES (?,?, 'settle', ?,?)",
            (request_id, req["account_id"], req["amount"], now))
        log_event(c, "credit_settled", {
            "request_id": request_id, "account_id": req["account_id"],
            "amount": req["amount"]})
        return _request_view(
            c, dict(_get(c, "SELECT * FROM fund_requests WHERE request_id=?",
                         (request_id,))))


def release(conn, request_id: str, reason: str | None = None) -> dict:
    """释放锁定：held → voided，额度恢复可用。

    已 settled 的请求不能释放（交付成立后不自动退钱）；voided 重放
    为幂等成功。
    """
    with WriteTx(conn) as c:
        row = _get(c, "SELECT * FROM fund_requests WHERE request_id=?",
                   (request_id,))
        if row is None:
            raise FinanceError(
                "request_not_found",
                f"取件请求 {request_id} 在财务侧无记录，无需释放", 404)
        req = dict(row)
        if req["status"] == "voided":
            return _request_view(c, req)      # 重放：幂等
        if req["status"] == "settled":
            raise FinanceError(
                "already_settled",
                f"请求 {request_id} 已确认为净扣费，不能释放；交付后的"
                "争议按追查规则处理，不自动退款", 409)
        if req["status"] != "held":
            raise FinanceError(
                "request_not_held",
                f"请求 {request_id} 状态为 {req['status']}，无可释放锁定",
                409)
        now = utcnow()
        c.execute(
            "UPDATE fund_requests SET status='voided', voided_at=?, "
            "updated_at=? WHERE request_id=?", (now, now, request_id))
        c.execute(
            "INSERT INTO fund_ledger(request_id,account_id,kind,amount,"
            "created_at) VALUES (?,?, 'release', ?,?)",
            (request_id, req["account_id"], req["amount"], now))
        log_event(c, "credit_released", {
            "request_id": request_id, "account_id": req["account_id"],
            "amount": req["amount"], "reason": reason})
        return _request_view(
            c, dict(_get(c, "SELECT * FROM fund_requests WHERE request_id=?",
                         (request_id,))))


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _request_view(c, req: dict) -> dict:
    ledger = _all(
        c, "SELECT kind,amount,created_at FROM fund_ledger "
           "WHERE request_id=? ORDER BY id", (req["request_id"],))
    # hold 只是额度锁定，不是费用；净费用按请求状态呈现：
    # held（待清算）：尚未产生净费用，钱仍在锁定中 -> 0；
    # settled（确已扣费）：原价一次净扣费 -> amount；
    # voided（已释放）：hold 与 release 相抵，未产生净费用 -> 0。
    if req["status"] == "settled":
        net_charge = req["amount"]
    else:
        net_charge = 0
    return {**req, "ledger": ledger, "net_charge": net_charge}


def get_request(conn, request_id: str) -> dict:
    """按取件请求号追溯资金状态（待清算/确已扣费/未产生净费用）。"""
    row = _get(conn, "SELECT * FROM fund_requests WHERE request_id=?",
               (request_id,))
    if row is None:
        raise FinanceError("request_not_found",
                           f"取件请求 {request_id} 无资金记录", 404)
    with WriteTx(conn) as c:
        return _request_view(c, dict(row))


def list_requests(conn, account_id: str | None = None) -> list[dict]:
    if account_id is not None:
        _account_row(conn, account_id)
        rows = _all(conn,
                    "SELECT * FROM fund_requests WHERE account_id=? "
                    "ORDER BY rowid", (account_id,))
    else:
        rows = _all(conn, "SELECT * FROM fund_requests ORDER BY rowid")
    with WriteTx(conn) as c:
        return [_request_view(c, r) for r in rows]


def ledger(conn, account_id: str | None = None) -> list[dict]:
    if account_id is not None:
        _account_row(conn, account_id)
        return _all(conn,
                    "SELECT * FROM fund_ledger WHERE account_id=? "
                    "ORDER BY id", (account_id,))
    return _all(conn, "SELECT * FROM fund_ledger ORDER BY id")


def event_log(conn, limit: int = 200) -> list[dict]:
    return _all(conn,
                "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,))
