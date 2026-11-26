"""FastAPI 应用：把 HTTP 请求映射到 service.py 的领域状态机。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .db import connect, init_db
from .models import (ArrivalIn, CreateJobIn, DeactivationIn, NoPaperIn,
                     ReassignBatchIn, ReceiptIn, RegisterAgentIn,
                     ResolveConflictIn, RetrieveIn, WasteIn)
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
    return JSONResponse(status_code=exc.status,
                        content={"error": exc.code, "message": exc.message})


def db(request: Request):
    return request.app.state.db


# ---- 工单 ----------------------------------------------------------------

@app.post("/jobs", status_code=201)
def create_job(body: CreateJobIn, request: Request):
    return service.create_job(
        db(request), body.job_id, body.title,
        body.page_count, body.copy_count, body.allowed_batches)


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
