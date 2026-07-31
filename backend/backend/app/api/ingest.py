"""
Trace ingestion endpoint (MVP plan, Section 6).

Records are validated and inserted individually. Section 6 requires this:
one malformed row from a customer's exporter must not reject the batch,
or a single bad record stalls their whole pipeline and they turn the
integration off.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.config import settings
from app.models.trace import IngestResult, RejectedRecord, TraceBatch, TraceRecord

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/traces", tags=["ingestion"])


async def get_pool(request: Request):
    return request.app.state.pool


@router.post("", response_model=IngestResult)
async def ingest_traces(payload: dict[str, Any], pool=Depends(get_pool)) -> IngestResult:
    raw_records = payload.get("records", [])
    if not isinstance(raw_records, list):
        return IngestResult(
            accepted=0,
            rejected=[RejectedRecord(index=0, error="'records' must be an array")],
        )

    tenant = UUID(settings.default_tenant_id)
    accepted = 0
    duplicates = 0
    rejected: list[RejectedRecord] = []

    async with pool.acquire() as conn:
        for i, raw in enumerate(raw_records):
            try:
                rec = TraceRecord(**raw)
            except ValidationError as exc:
                first = exc.errors()[0]
                rejected.append(RejectedRecord(
                    index=i,
                    trace_id=(raw or {}).get("trace_id") if isinstance(raw, dict) else None,
                    error=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
                ))
                continue
            except TypeError:
                rejected.append(RejectedRecord(index=i, error="record must be an object"))
                continue

            try:
                result = await conn.execute(
                    "INSERT INTO traces (trace_id, tenant_id, timestamp, task_node_id, "
                    "actor_id, action_type, outcome, cost, latency_ms, parent_trace_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
                    "ON CONFLICT (trace_id) DO NOTHING",
                    rec.trace_id, tenant, rec.timestamp, rec.task_node_id,
                    rec.actor_id, rec.action_type, rec.outcome,
                    rec.cost, rec.latency_ms, rec.parent_trace_id,
                )
                if result.endswith(" 0"):
                    duplicates += 1
                else:
                    accepted += 1
            except Exception as exc:  # noqa: BLE001
                # Most commonly a task_node_id FK violation: the customer
                # sent a trace for a node we haven't onboarded. That is a
                # per-record problem, not a batch failure.
                rejected.append(RejectedRecord(
                    index=i, trace_id=rec.trace_id, error=str(exc).split("\n")[0]
                ))

    if rejected:
        log.info("ingest: %d accepted, %d duplicate, %d rejected",
                 accepted, duplicates, len(rejected))
    return IngestResult(accepted=accepted, rejected=rejected, duplicates=duplicates)
