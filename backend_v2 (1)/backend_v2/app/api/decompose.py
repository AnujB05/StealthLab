"""
Tab 1: problem in, decomposition out (V2).

`POST /v1/decompose` returns a *proposal* and stores it. Nothing
generated from public input enters the shared graph until a human
approves it via `POST /v1/decompose/{id}/decide` — the same discipline
the debate loop applies, to a different input source.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import enforce_limits, get_scope, make_cost_recorder
from app.debate.panel import default_judge, default_panel
from app.models.change import ChangeSet
from app.services.access import AccessScope
from app.services.decomposition import DecompositionService
from app.services.knowledge_update import ChangeApplicationError, KnowledgeUpdater
from app.services.retrieval import HybridRetriever

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/decompose", tags=["decomposition"])


async def get_pool(request: Request):
    return request.app.state.pool


class DecomposeRequest(BaseModel):
    problem: str = Field(min_length=1, max_length=20_000)


class DecomposeResponse(BaseModel):
    id: UUID
    feasible: bool
    reasoning: str
    ops: list[dict]
    node_count: int
    safe_to_propose: bool
    structural_problems: list[str]
    objections: list[str]
    suspected_manipulation: bool
    input_flagged: bool
    input_truncated: bool
    related_existing: list[str]


class DecideRequest(BaseModel):
    approver_id: str = Field(min_length=1)
    decision: str = Field(pattern="^(approved|rejected)$")


class DecideResponse(BaseModel):
    id: UUID
    decision: str
    created_nodes: list[dict] = []
    refs: dict[str, str] = {}


@router.post("", response_model=DecomposeResponse)
async def decompose(
    body: DecomposeRequest,
    pool=Depends(get_pool),
    scope: AccessScope = Depends(get_scope),
    scope_key: str = Depends(enforce_limits),
) -> DecomposeResponse:
    panel = default_panel()
    recorder = make_cost_recorder(pool, scope_key, operation="decomposition")
    service = DecompositionService(
        generator=panel[0],
        # A different model family for critique: a model reviewing its
        # own output shares whatever blind spot produced the flaw.
        critic=panel[1] if len(panel) > 1 else default_judge(),
        retriever=HybridRetriever(pool, scope=scope),
        on_call=recorder,
    )

    result = await service.decompose(body.problem)
    ops = [op.model_dump(mode="json") for op in result.change_set.ops]

    row = await pool.fetchrow(
        "INSERT INTO decompositions (submitter_key, problem, feasible, reasoning, "
        "change_set, structural_problems, objections, suspected_manipulation, "
        "input_flagged) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
        scope_key, body.problem, result.feasible, result.reasoning,
        {"ops": ops}, result.structural_problems, result.objections,
        result.suspected_manipulation, bool(result.input_flags),
    )

    return DecomposeResponse(
        id=row["id"],
        feasible=result.feasible,
        reasoning=result.reasoning,
        ops=ops,
        node_count=result.node_count,
        safe_to_propose=result.safe_to_propose,
        structural_problems=result.structural_problems,
        objections=result.objections,
        suspected_manipulation=result.suspected_manipulation,
        input_flagged=bool(result.input_flags),
        input_truncated=result.input_truncated,
        related_existing=result.related_existing,
    )


@router.get("/pending")
async def list_pending(pool=Depends(get_pool)):
    """Proposals awaiting a decision, newest first."""
    rows = await pool.fetch(
        "SELECT id, problem, reasoning, change_set, objections, "
        "suspected_manipulation, input_flagged, created_at "
        "FROM decompositions WHERE status = 'proposed' AND feasible "
        "ORDER BY created_at DESC LIMIT 100"
    )
    return [dict(r) for r in rows]


@router.get("/{decomposition_id}")
async def get_decomposition(decomposition_id: UUID, pool=Depends(get_pool)):
    row = await pool.fetchrow(
        "SELECT * FROM decompositions WHERE id = $1", decomposition_id
    )
    if row is None:
        raise HTTPException(404, "decomposition not found")
    return dict(row)


@router.post("/{decomposition_id}/decide", response_model=DecideResponse)
async def decide(
    decomposition_id: UUID,
    body: DecideRequest,
    pool=Depends(get_pool),
) -> DecideResponse:
    row = await pool.fetchrow(
        "SELECT id, change_set, status, feasible FROM decompositions WHERE id = $1",
        decomposition_id,
    )
    if row is None:
        raise HTTPException(404, "decomposition not found")
    if row["status"] != "proposed":
        # Idempotency guard: re-approving would create a duplicate
        # subgraph, since every apply inserts new nodes.
        raise HTTPException(409, f"already {row['status']}")

    created: list[dict] = []
    refs: dict[str, str] = {}

    if body.decision == "approved":
        if not row["feasible"]:
            raise HTTPException(400, "cannot approve an infeasible decomposition")
        change_set = ChangeSet(**(row["change_set"] or {"ops": []}))
        try:
            outcome = await KnowledgeUpdater(pool).apply_generated(
                change_set, approver_id=body.approver_id
            )
        except ChangeApplicationError as exc:
            # Leave it 'proposed' so it can be re-examined rather than
            # silently marked decided against work that never landed.
            raise HTTPException(409, str(exc)) from exc
        created = outcome["applied"]
        refs = outcome["refs"]

    await pool.execute(
        "UPDATE decompositions SET status = $2, approver_id = $3, "
        "decided_at = now(), applied_refs = $4 WHERE id = $1",
        decomposition_id, body.decision, body.approver_id, refs or None,
    )

    return DecideResponse(
        id=decomposition_id, decision=body.decision,
        created_nodes=created, refs=refs,
    )
