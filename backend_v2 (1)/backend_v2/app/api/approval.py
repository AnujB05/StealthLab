"""
Approval endpoints (MVP plan, Section 9).

The approve path does three things in order: record the human decision,
apply the change set, write a VALIDATED_BY edge. If application fails the
whole thing rolls back and the debate stays PENDING_APPROVAL -- an
approval recorded against a change that did not apply would be a false
audit trail, which is worse than no audit trail.

`role` is read but not enforced (Section 12 auth placeholder). Enforcement
arrives with real RBAC at the second-customer trigger; the field exists
now so adding enforcement is a check, not a migration.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.debate.state_machine import DebateStateMachine, IllegalTransition
from app.export.markdown_diff import render_export
from app.models.change import ChangeSet
from app.models.debate import Layer1Result, Scorecard
from app.services.knowledge_update import ChangeApplicationError, KnowledgeUpdater

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/approvals", tags=["approval"])


async def get_pool(request: Request):
    return request.app.state.pool


class ApprovalRequest(BaseModel):
    approver_id: str
    approver_role: Optional[str] = None  # unenforced placeholder, Section 12
    decision: str  # "approved" | "rejected"
    note: Optional[str] = None


class ApprovalResponse(BaseModel):
    approval_id: UUID
    decision: str
    applied_ops: list[dict] = []
    export_markdown: Optional[str] = None


@router.post("/{scorecard_id}", response_model=ApprovalResponse)
async def decide(
    scorecard_id: UUID, body: ApprovalRequest, pool=Depends(get_pool)
) -> ApprovalResponse:
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")

    row = await pool.fetchrow(
        "SELECT s.id, s.debate_id, s.candidate_id, s.layer1_passed, s.blast_radius, "
        "s.reversible, s.recommendation, c.summary, c.change_set, c.supporters "
        "FROM scorecards s JOIN candidates c ON c.id = s.candidate_id WHERE s.id = $1",
        scorecard_id,
    )
    if row is None:
        raise HTTPException(404, "scorecard not found")

    machine = DebateStateMachine(pool)
    updater = KnowledgeUpdater(pool)
    now = datetime.now(timezone.utc)
    applied: list[dict] = []
    export_md: Optional[str] = None

    change_set = ChangeSet(**(row["change_set"] or {"ops": []}))

    if body.decision == "approved":
        try:
            applied = await updater.apply(change_set, body.approver_id, at=now)
        except ChangeApplicationError as exc:
            # Leave the debate in PENDING_APPROVAL so it can be retried or
            # re-debated once the conflict is understood.
            log.error("change application failed for scorecard %s: %s", scorecard_id, exc)
            raise HTTPException(409, f"could not apply change: {exc}") from exc

    approval_row = await pool.fetchrow(
        "INSERT INTO approvals (scorecard_id, candidate_id, approver_id, approver_role, "
        "decision, note, decided_at, applied_at, applied_ops) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
        scorecard_id, row["candidate_id"], body.approver_id, body.approver_role,
        body.decision, body.note, now, now if applied else None, applied,
    )

    try:
        await machine.transition(
            row["debate_id"],
            "APPROVED" if body.decision == "approved" else "REJECTED",
            reason=body.note or f"{body.decision} by {body.approver_id}",
            actor=body.approver_id,
        )
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc)) from exc

    if body.decision == "approved":
        scorecard = Scorecard(
            id=row["id"],
            debate_id=row["debate_id"],
            candidate_id=row["candidate_id"],
            summary=row["summary"],
            proposers=row["supporters"] or [],
            layer1=Layer1Result(candidate_id=row["candidate_id"], passed=row["layer1_passed"]),
            blast_radius=row["blast_radius"],
            reversible=row["reversible"],
            recommendation=row["recommendation"],
        )
        export_md = render_export(
            scorecard, change_set, body.approver_id, now.isoformat()
        )

    return ApprovalResponse(
        approval_id=approval_row["id"],
        decision=body.decision,
        applied_ops=applied,
        export_markdown=export_md,
    )


@router.get("/pending")
async def list_pending(pool=Depends(get_pool)):
    """Scorecards awaiting a decision, newest first. Summary view only —
    use GET /{scorecard_id} for the full detail a real approval screen needs."""
    rows = await pool.fetch(
        "SELECT s.id, s.debate_id, s.candidate_id, s.layer1_passed, s.groundedness_score, "
        "s.blast_radius, s.reversible, s.recommendation, c.summary, c.supporters, s.created_at "
        "FROM scorecards s "
        "JOIN candidates c ON c.id = s.candidate_id "
        "JOIN debates d ON d.id = s.debate_id "
        "WHERE d.state = 'PENDING_APPROVAL' "
        "ORDER BY s.created_at DESC"
    )
    return [dict(r) for r in rows]


@router.get("/{scorecard_id}")
async def get_detail(scorecard_id: UUID, pool=Depends(get_pool)):
    """
    Full scorecard detail: Layer 1 reasoning, the proposed change set, and
    the debate transcript that produced it. This is what a real approval
    screen renders — the summary list alone isn't enough to approve
    responsibly against.
    """
    row = await pool.fetchrow(
        "SELECT s.*, c.summary, c.rationale, c.change_set, c.supporters, "
        "d.round_number, d.termination_reason, t.task_node_id "
        "FROM scorecards s "
        "JOIN candidates c ON c.id = s.candidate_id "
        "JOIN debates d ON d.id = s.debate_id "
        "JOIN triggers t ON t.debate_id = d.id "
        "WHERE s.id = $1",
        scorecard_id,
    )
    if row is None:
        raise HTTPException(404, "scorecard not found")

    turns = await pool.fetch(
        "SELECT round_number, speaker_id, speaker_kind, model_used, action, "
        "candidate_id, content, cites, created_at "
        "FROM debate_turns WHERE debate_id = $1 ORDER BY round_number, created_at",
        row["debate_id"],
    )

    detail = dict(row)
    detail["transcript"] = [dict(t) for t in turns]
    return detail
