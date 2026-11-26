"""HTTP 请求/响应模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CreateJobIn(BaseModel):
    job_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    page_count: int = Field(..., gt=0)
    copy_count: int = Field(..., gt=0)
    # 指定允许使用的批次编号；缺省/null = 旧流程，不参与纸料账
    allowed_batches: Optional[list[str]] = None
    # 企业授信取件：两字段必须同时给出（或同时省略）。
    # 给出后该工单每份成品按 price_per_copy（整数分）从企业共享额度结算
    credit_account_id: Optional[str] = Field(default=None, min_length=1)
    price_per_copy: Optional[int] = Field(default=None, ge=0)


class CreditPickupIn(BaseModel):
    """柜员提交的授信取件请求：工单号在路径上，份号与取件请求号在体里。"""
    copy_no: int = Field(..., gt=0)
    request_id: str = Field(..., min_length=1)


class ReconcilePickupIn(BaseModel):
    """对结果未定的取件请求发起对账/收敛（可选人工触发，重送也会自动收敛）。"""
    note: Optional[str] = None


class ArrivalIn(BaseModel):
    sheets: int = Field(..., gt=0)
    note: Optional[str] = None


class ReassignBatchIn(BaseModel):
    batch_id: str = Field(..., min_length=1)


class RegisterAgentIn(BaseModel):
    agent_id: str = Field(..., min_length=1)


class ReceiptIn(BaseModel):
    dispatch_id: str
    agent_id: str
    paper_id: str
    receipt_id: str


class DeactivationIn(BaseModel):
    dispatch_id: str
    agent_id: str
    executed_local: bool = False
    evidence_paper_id: Optional[str] = None


class NoPaperIn(BaseModel):
    note: Optional[str] = None


class RetrieveIn(BaseModel):
    paper_id: str
    dispatch_id: Optional[str] = None
    note: Optional[str] = None


class WasteIn(BaseModel):
    paper_id: str
    note: Optional[str] = None


class ResolveConflictIn(BaseModel):
    keep_paper_id: Optional[str] = None
    note: Optional[str] = None


class RecallBatchIn(BaseModel):
    batch_id: Optional[str] = None
    note: Optional[str] = None
    reason: Optional[str] = None


class SpareCopiesIn(BaseModel):
    # 兼容不同调用命名；service 层统一归一化。
    copy_count: Optional[int] = Field(default=None, gt=0)
    copies: Optional[int] = Field(default=None, gt=0)
    copies_count: Optional[int] = Field(default=None, gt=0)
    count: Optional[int] = Field(default=None, gt=0)
    quantity: Optional[int] = Field(default=None, gt=0)
    n: Optional[int] = Field(default=None, gt=0)
    spare_count: Optional[int] = Field(default=None, gt=0)
    page_count: Optional[int] = Field(default=None, gt=0)
    pages: Optional[int] = Field(default=None, gt=0)
    number_of_copies: Optional[int] = Field(default=None, gt=0)
    allowed_batches: Optional[list[str]] = None
    batch_ids: Optional[list[str]] = None
    batches: Optional[list[str]] = None
    batch_id: Optional[str] = None
    note: Optional[str] = None
    reason: Optional[str] = None


class ExchangeIn(BaseModel):
    # 路径中已有 job_id/copy_no 时，这些字段均可由路由补齐。
    request_id: Optional[str] = Field(default=None, min_length=1)
    exchange_request_id: Optional[str] = Field(default=None, min_length=1)
    exchange_request_no: Optional[str] = Field(default=None, min_length=1)
    request_no: Optional[str] = Field(default=None, min_length=1)
    exchange_id: Optional[str] = Field(default=None, min_length=1)
    idempotency_key: Optional[str] = Field(default=None, min_length=1)
    job_id: Optional[str] = None
    copy_no: Optional[int] = Field(default=None, gt=0)
    original_copy_no: Optional[int] = Field(default=None, gt=0)
    original_copy: Optional[int] = Field(default=None, gt=0)
    original_copy_id: Optional[str] = None
    copy_id: Optional[str] = None
    returned_copy_no: Optional[int] = Field(default=None, gt=0)
    held_copy_no: Optional[int] = Field(default=None, gt=0)
    current_copy_no: Optional[int] = Field(default=None, gt=0)
    spare_copy_no: Optional[int] = Field(default=None, gt=0)
    spare_no: Optional[int] = Field(default=None, gt=0)
    spare_id: Optional[str] = None
    spare_copy_id: Optional[str] = None
    recovered_paper_ids: Optional[list] = None
    returned_paper_ids: Optional[list] = None
    paper_ids: Optional[list] = None
    recovered_papers: Optional[list] = None
    papers: Optional[list] = None
    note: Optional[str] = None
