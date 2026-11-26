"""FastAPI 应用：把 HTTP 请求映射到 service.py 的领域状态机。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .db import connect, init_db
from .models import (ArrivalIn, CreateJobIn, CreditPickupIn,
                     DeactivationIn, ExchangeIn, NoPaperIn,
                     ReassignBatchIn, RecallBatchIn, ReceiptIn,
                     ReconcilePickupIn, RegisterAgentIn,
                     ResolveConflictIn, RetrieveIn, SpareCopiesIn,
                     WasteIn)
from .service import DomainError

DB_PATH = os.environ.get("PRINTCENTER_DB", "/tmp/print_center/demo_server.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    app.state.db = connect(DB_PATH)
    yield
    app.state.db.close()


app = FastAPI(title="小型印制中心续打服务", version="1.1",
              lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True}


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    content = {"error": exc.code, "message": exc.message}
    details = getattr(exc, "details", None)
    if details:
        content["details"] = details
    return JSONResponse(status_code=exc.status, content=content)


def db(request: Request):
    return request.app.state.db


# ---- 工单 ----------------------------------------------------------------

@app.post("/jobs", status_code=201)
def create_job(body: CreateJobIn, request: Request):
    return service.create_job(
        db(request), body.job_id, body.title,
        body.page_count, body.copy_count, body.allowed_batches,
        credit_account_id=body.credit_account_id,
        price_per_copy=body.price_per_copy)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    return service.get_job(db(request), job_id)


# ---- 纸料批次与承诺 --------------------------------------------------------

@app.post("/paper/batches/{batch_id}/arrivals", status_code=201)
def arrival(batch_id: str, body: ArrivalIn, request: Request):
    return service.register_arrival(
        db(request), batch_id, body.sheets, body.note)


@app.get("/paper/batches")
def batches(request: Request):
    return {"batches": service.list_batches(db(request))}


@app.get("/paper/batches/{batch_id}")
def batch(batch_id: str, request: Request):
    return service.batch_view(db(request), batch_id)


@app.get("/jobs/{job_id}/commitments")
def commitments(job_id: str, request: Request):
    return service.job_commitments(db(request), job_id)


@app.post("/jobs/{job_id}/copies/{copy_no}/reassign-batch")
def reassign_batch(job_id: str, copy_no: int, body: ReassignBatchIn,
                   request: Request):
    return service.reassign_copy_batch(
        db(request), job_id, copy_no, body.batch_id)


@app.post("/jobs/{job_id}/copies/{copy_no}/cancel")
def cancel_copy(job_id: str, copy_no: int, request: Request):
    return service.cancel_copy(db(request), job_id, copy_no)


@app.get("/dispatches/{dispatch_id}/withdrawal")
def dispatch_withdrawal(dispatch_id: str, request: Request):
    return service.dispatch_withdrawal(db(request), dispatch_id)


# ---- 批次召回、备品与柜台换货 ---------------------------------------------

@app.post("/paper/batches/{batch_id}/recall", status_code=201)
@app.post("/paper/batches/{batch_id}/recalls", status_code=201)
def recall_batch(batch_id: str, request: Request,
                 body: Optional[RecallBatchIn] = None):
    note = (body.note or body.reason) if body else None
    return service.recall_batch(db(request), batch_id, note)


@app.post("/recall", status_code=201)
def recall_by_body(body: RecallBatchIn, request: Request):
    if not body.batch_id:
        raise DomainError("batch_id_required", "召回必须指定 batch_id", 400)
    return service.recall_batch(db(request), body.batch_id,
                                body.note or body.reason)


@app.post("/recalls", status_code=201)
def create_recall(body: RecallBatchIn, request: Request):
    return recall_by_body(body, request)


@app.post("/recalls/{batch_id}", status_code=201)
def recall_batch_alias(batch_id: str, request: Request,
                       body: Optional[RecallBatchIn] = None):
    return recall_batch(batch_id, request, body)


@app.get("/recalls/pending")
def recalls_pending(request: Request, batch_id: Optional[str] = None,
                    job_id: Optional[str] = None):
    return {"pending": service.list_recall_pending(
        db(request), batch_id=batch_id, job_id=job_id)}


@app.get("/recall-pending")
def recall_pending_alias(request: Request, batch_id: Optional[str] = None,
                         job_id: Optional[str] = None):
    return recalls_pending(request, batch_id, job_id)


@app.get("/recall/queue")
def recall_queue_alias(request: Request, batch_id: Optional[str] = None,
                       job_id: Optional[str] = None):
    return recalls_pending(request, batch_id, job_id)


@app.get("/recall/pending")
def recall_pending_slash_alias(request: Request,
                               batch_id: Optional[str] = None,
                               job_id: Optional[str] = None):
    return recalls_pending(request, batch_id, job_id)


@app.get("/recall-list")
def recall_list_alias(request: Request, batch_id: Optional[str] = None,
                      job_id: Optional[str] = None):
    return recalls_pending(request, batch_id, job_id)


@app.get("/paper/batches/{batch_id}/pending")
def batch_pending(batch_id: str, request: Request):
    return {"batch_id": batch_id,
            "pending": service.list_recall_pending(
                db(request), batch_id=batch_id)}


@app.get("/recalls")
def recalls(request: Request):
    return {"recalls": service.list_recalls(db(request))}


@app.get("/recall-dashboard")
def recall_dashboard(request: Request, job_id: Optional[str] = None):
    return service.recall_dashboard(db(request), job_id)


@app.get("/recalls/{batch_id}")
def recall_detail(batch_id: str, request: Request):
    return service.get_recall(db(request), batch_id)


@app.get("/recalls/{batch_id}/pending")
def recall_detail_pending(batch_id: str, request: Request):
    return {"batch_id": batch_id,
            "pending": service.list_recall_pending(
                db(request), batch_id=batch_id)}


@app.post("/jobs/{job_id}/spares", status_code=201)
@app.post("/jobs/{job_id}/spare", status_code=201)
@app.post("/jobs/{job_id}/spare-copies", status_code=201)
def create_spares(job_id: str, request: Request,
                  body: Optional[SpareCopiesIn] = None):
    data = body.model_dump(exclude_none=True) if body is not None else {}
    return service.create_spare_copies(db(request), job_id, **data)


@app.get("/jobs/{job_id}/spares")
def list_spares(job_id: str, request: Request,
                only_available: bool = False):
    return service.list_spare_copies(
        db(request), job_id, only_available=only_available)


@app.get("/spares")
def all_spares(request: Request, only_available: bool = False):
    return service.list_spare_copies(
        db(request), None, only_available=only_available)


def _exchange_kwargs(body: ExchangeIn, job_id: Optional[str] = None,
                     copy_no: Optional[int] = None) -> dict:
    data = body.model_dump(exclude_none=True)
    request_id = (data.pop("request_id", None)
                  or data.pop("exchange_request_id", None)
                  or data.pop("exchange_request_no", None)
                  or data.pop("request_no", None)
                  or data.pop("idempotency_key", None)
                  or data.pop("exchange_id", None))
    recovered = (data.pop("recovered_paper_ids", None)
                 or data.pop("returned_paper_ids", None)
                 or data.pop("recovered_papers", None)
                 or data.pop("papers", None)
                 or data.pop("paper_ids", None))
    original = (data.pop("original_copy_no", None)
                or data.pop("original_copy", None)
                or data.pop("original_copy_id", None)
                or data.pop("copy_id", None)
                or data.pop("copy_no", None) or copy_no)
    spare = (data.pop("spare_copy_no", None)
             or data.pop("spare_no", None)
             or data.pop("spare_id", None)
             or data.pop("spare_copy_id", None))
    returned = (data.pop("returned_copy_no", None)
                or data.pop("held_copy_no", None)
                or data.pop("current_copy_no", None))
    body_job = data.pop("job_id", None) or job_id
    # 其余 note 等字段不进入领域函数。
    return {"request_id": request_id, "job_id": body_job,
            "original_copy_no": original,
            "returned_copy_no": returned,
            "spare_copy_no": spare,
            "recovered_paper_ids": recovered}


@app.post("/exchanges", status_code=201)
@app.post("/exchange", status_code=201)
@app.post("/exchange-requests", status_code=201)
@app.post("/counter-exchange", status_code=201)
@app.post("/counter/exchange", status_code=201)
@app.post("/recalls/exchange", status_code=201)
def create_exchange(body: ExchangeIn, request: Request):
    return service.exchange_copy(
        db(request), **_exchange_kwargs(body))


@app.post("/jobs/{job_id}/copies/{copy_no}/exchange", status_code=201)
@app.post("/jobs/{job_id}/copies/{copy_no}/exchanges", status_code=201)
def exchange_for_copy(job_id: str, copy_no: int, body: ExchangeIn,
                      request: Request):
    return service.exchange_copy(
        db(request), **_exchange_kwargs(body, job_id, copy_no))


@app.get("/exchanges")
def list_exchanges(request: Request, job_id: Optional[str] = None,
                   copy_no: Optional[int] = None):
    return {"exchanges": service.list_copy_exchanges(
        db(request), job_id, copy_no)}


@app.get("/exchanges/{request_id}")
@app.get("/exchange-requests/{request_id}")
@app.get("/counter-exchanges/{request_id}")
def exchange_detail(request_id: str, request: Request):
    return service.get_exchange(db(request), request_id)


@app.get("/holdings")
def holdings(request: Request, job_id: Optional[str] = None,
             include_returned: bool = False):
    return service.list_holdings(
        db(request), job_id, include_returned=include_returned)


@app.get("/copies/in-hand")
def copies_in_hand(request: Request, job_id: Optional[str] = None):
    return service.list_holdings(db(request), job_id)


@app.get("/counter-exchanges")
def counter_exchanges(request: Request, job_id: Optional[str] = None,
                      copy_no: Optional[int] = None):
    return list_exchanges(request, job_id, copy_no)


@app.get("/jobs/{job_id}/holdings")
def job_holdings(job_id: str, request: Request,
                 include_returned: bool = False):
    return service.list_holdings(
        db(request), job_id, include_returned=include_returned)


@app.get("/jobs/{job_id}/copies/{copy_no}/current-delivery-list")
def current_delivery_list(job_id: str, copy_no: int, request: Request):
    return service.current_delivery_list(db(request), job_id, copy_no)


@app.get("/jobs/{job_id}/copies/{copy_no}/recall-view")
@app.get("/jobs/{job_id}/copies/{copy_no}/history")
def copy_recall_view(job_id: str, copy_no: int, request: Request):
    return service.get_copy_recall_view(db(request), job_id, copy_no)


# ---- 代理 ----------------------------------------------------------------

@app.post("/agents/register", status_code=201)
def register_agent(body: RegisterAgentIn, request: Request):
    return service.register_agent(db(request), body.agent_id)


@app.post("/agents/{agent_id}/heartbeat")
def heartbeat(agent_id: str, request: Request):
    service.heartbeat(db(request), agent_id)
    return {"ok": True}


@app.post("/agents/{agent_id}/claim")
def claim(agent_id: str, request: Request):
    return {"dispatch": service.claim_dispatch(db(request), agent_id)}


@app.get("/agents/{agent_id}/dispatches")
def agent_dispatches(agent_id: str, request: Request):
    return {"dispatches": service.agent_dispatches(db(request), agent_id)}


@app.post("/receipts")
def receipt(body: ReceiptIn, request: Request):
    return service.receive_receipt(
        db(request), body.dispatch_id, body.agent_id,
        body.paper_id, body.receipt_id)


@app.post("/deactivations")
def deactivation(body: DeactivationIn, request: Request):
    return service.deactivate_dispatch(
        db(request), body.dispatch_id, body.agent_id,
        body.executed_local, body.evidence_paper_id)


# ---- 现场核对 -------------------------------------------------------------

@app.get("/onsite/pending")
def pending(job_id: Optional[str] = None, request: Request = None):
    return {"pending": service.pending_onsite(db(request), job_id)}


@app.post("/jobs/{job_id}/copies/{copy_no}/pages/{page_no}/no-paper")
def no_paper(job_id: str, copy_no: int, page_no: int, body: NoPaperIn,
             request: Request):
    return service.onsite_no_paper(
        db(request), job_id, copy_no, page_no, body.note)


@app.post("/jobs/{job_id}/copies/{copy_no}/pages/{page_no}/retrieve")
def retrieve(job_id: str, copy_no: int, page_no: int, body: RetrieveIn,
             request: Request):
    return service.onsite_retrieve(
        db(request), job_id, copy_no, page_no, body.paper_id,
        body.dispatch_id, body.note)


@app.post("/jobs/{job_id}/copies/{copy_no}/pages/{page_no}/waste")
def waste(job_id: str, copy_no: int, page_no: int, body: WasteIn,
          request: Request):
    return service.onsite_waste(
        db(request), job_id, copy_no, page_no, body.paper_id, body.note)


@app.post("/jobs/{job_id}/copies/{copy_no}/pages/{page_no}/resolve-conflict")
def resolve_conflict(job_id: str, copy_no: int, page_no: int,
                     body: ResolveConflictIn, request: Request):
    return service.resolve_conflict(
        db(request), job_id, copy_no, page_no,
        body.keep_paper_id, body.note)


# ---- 补打 ----------------------------------------------------------------

@app.post("/jobs/{job_id}/copies/{copy_no}/pages/{page_no}/reprint",
          status_code=201)
def reprint(job_id: str, copy_no: int, page_no: int, request: Request):
    return service.reprint(db(request), job_id, copy_no, page_no)


# ---- 交付 ----------------------------------------------------------------

@app.get("/jobs/{job_id}/delivery-readiness")
def readiness(job_id: str, request: Request):
    return {"copies": service.delivery_readiness(db(request), job_id)}


@app.post("/jobs/{job_id}/copies/{copy_no}/deliver")
def deliver(job_id: str, copy_no: int, request: Request):
    return service.confirm_delivery(db(request), job_id, copy_no)


# ---- 企业授信取件 ---------------------------------------------------------

@app.post("/jobs/{job_id}/credit-pickups", status_code=202)
def credit_pickup(job_id: str, body: CreditPickupIn, request: Request):
    """柜员提交授信取件（份号 + 取件请求号）。同号重送幂等收敛。"""
    return service.request_credit_pickup(
        db(request), job_id, body.copy_no, body.request_id)


@app.get("/credit-pickups/{request_id}")
def credit_pickup_status(request_id: str, request: Request):
    """柜员查询取件结果（含冻结的交付清单与资金状态）。"""
    return service.get_pickup(db(request), request_id)


@app.get("/credit-pickups")
def credit_pickups(job_id: Optional[str] = None, request: Request = None):
    return {"pickups": service.list_pickups(db(request), job_id)}


@app.post("/credit-pickups/{request_id}/reconcile")
def credit_pickup_reconcile(request_id: str, body: ReconcilePickupIn,
                            request: Request):
    """对结果未定的取件请求主动对账收敛（与同号重送等价）。"""
    return service.reconcile_pickup(db(request), request_id)


@app.get("/jobs/{job_id}/delivery-list")
def delivery_list(job_id: str, copy_no: Optional[int] = None,
                  request: Request = None):
    return service.delivery_list(db(request), job_id, copy_no)


# ---- 视图 ----------------------------------------------------------------

@app.get("/papers")
def papers(job_id: Optional[str] = None, request: Request = None):
    return {"papers": service.list_papers(db(request), job_id)}


@app.get("/dispatches")
def dispatches(job_id: Optional[str] = None, request: Request = None):
    return {"dispatches": service.list_dispatches(db(request), job_id)}


@app.get("/investigations")
def investigations(job_id: Optional[str] = None, request: Request = None):
    return {"investigations": service.list_investigations(
        db(request), job_id)}


@app.get("/events")
def events(limit: int = 200, request: Request = None):
    return {"events": service.event_log(db(request), limit)}
