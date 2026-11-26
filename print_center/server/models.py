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
