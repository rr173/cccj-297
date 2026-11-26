"""印制服务 -> 模拟财务服务的 HTTP 客户端。

设计约束
========
* 两个服务分别持久化、不共用数据库；这里只走 HTTP，绝不直接打开
  财务库。
* 超时/连接失败统一抛 :class:`FinanceUnavailable`，由上层把请求置为
  "待处理"并明确展示——**绝不凭超时假定扣费成功，也不假定没扣款**。
* 财务侧的业务拒绝（额度不足、请求号冲突等）抛 :class:`FinanceRejected`，
  携带稳定错误码与说明；网络层故障与业务拒绝严格区分。
* 所有调用都带幂等键（request_id），重送天然安全。
"""
from __future__ import annotations

import os

try:  # HTTP 依赖在正常 requirements 环境中必然存在；保留惰性错误便于纯服务层测试
    import httpx
except ImportError:  # pragma: no cover - 仅最小 Python 环境会走到
    httpx = None


class FinanceUnavailable(Exception):
    """财务服务无响应/超时：结果未知，调用方必须按待处理对待。"""


class FinanceRejected(Exception):
    """财务服务明确拒绝（如额度不足、请求号冲突）。"""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def finance_base_url() -> str:
    return os.environ.get("FINANCE_URL", "http://127.0.0.1:8078").rstrip("/")


def _timeout() -> float:
    return float(os.environ.get("FINANCE_TIMEOUT", "4"))


def _request(method: str, path: str, body: dict | None = None) -> dict:
    if httpx is None:
        raise FinanceUnavailable("财务 HTTP 客户端依赖 httpx 未安装")
    try:
        r = httpx.request(method, finance_base_url() + path, json=body,
                          timeout=_timeout())
    except httpx.HTTPError as exc:
        raise FinanceUnavailable(f"财务服务无响应：{exc}") from exc
    if r.status_code >= 400:
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        raise FinanceRejected(
            payload.get("error", "finance_error"),
            payload.get("message", r.text[:200]), r.status_code)
    return r.json()


def healthy() -> bool:
    if httpx is None:
        return False
    try:
        r = httpx.get(finance_base_url() + "/health", timeout=_timeout())
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def hold(request_id: str, account_id: str, amount: int,
         reference: str | None = None, note: str | None = None) -> dict:
    return _request("POST", "/holds", {
        "request_id": request_id, "account_id": account_id,
        "amount": amount, "reference": reference, "note": note})


def settle(request_id: str, amount: int) -> dict:
    return _request("POST", f"/requests/{request_id}/settle",
                    {"amount": amount})


def release(request_id: str, reason: str | None = None) -> dict:
    return _request("POST", f"/requests/{request_id}/release",
                    {"reason": reason})


def get_request(request_id: str) -> dict:
    return _request("GET", f"/requests/{request_id}")
