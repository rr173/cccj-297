"""模拟财务服务 HTTP 应用（可独立启动，独立持久化）。

默认端口 8078，数据库路径由 FINANCE_DB 指定，与印制服务的
PRINTCENTER_DB 是两个完全独立的 SQLite 文件。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import service
from .db import connect, init_db
from .service import FinanceError

DB_PATH = os.environ.get("FINANCE_DB", "/tmp/print_center/finance.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    app.state.db = connect(DB_PATH)
    yield
    app.state.db.close()


app = FastAPI(title="模拟企业授信财务服务", version="1.0",
              lifespan=lifespan)


@app.exception_handler(FinanceError)
async def finance_error_handler(request: Request, exc: FinanceError):
    return JSONResponse(status_code=exc.status,
                        content={"error": exc.code, "message": exc.message})


def db(request: Request):
    return request.app.state.db


class AccountIn(BaseModel):
    account_id: str = Field(..., min_length=1)
    credit_limit: int = Field(..., ge=0)          # 整数分
    name: Optional[str] = None


class HoldIn(BaseModel):
    request_id: str = Field(..., min_length=1)    # 取件请求号（幂等键）
    account_id: str = Field(..., min_length=1)
    amount: int = Field(..., ge=0)                # 每份价格，整数分
    reference: Optional[str] = None
    note: Optional[str] = None


class SettleIn(BaseModel):
    amount: Optional[int] = Field(default=None, ge=0)


class ReleaseIn(BaseModel):
    reason: Optional[str] = None


@app.get("/health")
def health():
    return {"ok": True, "service": "finance"}


# ---- 企业账户 -------------------------------------------------------------

@app.post("/accounts", status_code=201)
def create_account(body: AccountIn, request: Request):
    return service.create_account(
        db(request), body.account_id, body.credit_limit, body.name)


@app.get("/accounts")
def accounts(request: Request):
    return {"accounts": service.list_accounts(db(request))}


@app.get("/accounts/{account_id}")
def account(account_id: str, request: Request):
    return service.get_account(db(request), account_id)


# ---- 资金处理（按取件请求号幂等） ------------------------------------------

@app.post("/holds", status_code=201)
def hold(body: HoldIn, request: Request):
    return service.hold(db(request), body.request_id, body.account_id,
                        body.amount, body.reference, body.note)


@app.post("/requests/{request_id}/settle")
def settle(request_id: str, body: SettleIn, request: Request):
    return service.settle(db(request), request_id, body.amount)


@app.post("/requests/{request_id}/release")
def release(request_id: str, body: ReleaseIn, request: Request):
    return service.release(db(request), request_id, body.reason)


# ---- 查询 -----------------------------------------------------------------

@app.get("/requests/{request_id}")
def get_request(request_id: str, request: Request):
    return service.get_request(db(request), request_id)


@app.get("/requests")
def list_requests(account_id: Optional[str] = None, request: Request = None):
    return {"requests": service.list_requests(db(request), account_id)}


@app.get("/ledger")
def ledger(account_id: Optional[str] = None, request: Request = None):
    return {"ledger": service.ledger(db(request), account_id)}


@app.get("/events")
def events(limit: int = 200, request: Request = None):
    return {"events": service.event_log(db(request), limit)}
