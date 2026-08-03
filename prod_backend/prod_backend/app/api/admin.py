"""
Manual loop trigger (MVP plan, Section 15 Phase B / v1.1+ scheduling gap).

Before this file, nothing in the application ever called
TriggerDetector.scan() or LoopOrchestrator.run() -- the loop existed as
code but nothing invoked it. For v0, invocation is a manually-called
endpoint rather than an in-process scheduler: matches the "don't build
infrastructure before you need it" discipline used everywhere else in
this codebase (Section 12). Call it from a cron job, a dashboard button,
or curl. An actual scheduler is a config change away, not a rewrite --
whatever calls this endpoint on a timer is the seam.

Default thresholds are deliberately low so a demo can produce a real
trigger without weeks of production data. Real calibration against a
workflow's actual baseline (Section 15.1, Phase B) replaces these before
this endpoint is trustworthy for anything but a demo.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import enforce_limits, make_cost_recorder
from app.debate.panel import default_judge, default_layer2_agent, default_panel
from app.services.loop import LoopOrchestrator
from app.services.triggers import ThresholdRule, TriggerDetector

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin", tags=["admin"])

_DEMO_RULES = [
    ThresholdRule(name="high_error_rate", metric="error_rate", threshold=0.15, min_samples=5),
    ThresholdRule(name="high_rework_rate", metric="rework_rate", threshold=0.20, min_samples=5),
]


async def get_pool(request: Request):
    return request.app.state.pool


class DebateOutcome(BaseModel):
    debate_id: UUID
    state: str
    termination_reason: Optional[str]
    candidates_proposed: int
    candidates_passed_layer1: int
    detail: Optional[str] = None


class ScanResponse(BaseModel):
    triggers_found: int
    debates_run: int
    outcomes: list[DebateOutcome] = []
    errors: list[str] = []


@router.post("/scan", response_model=ScanResponse)
async def run_scan(
    thresholds: Optional[list[ThresholdRule]] = None,
    pool=Depends(get_pool),
    scope_key: str = Depends(enforce_limits),
) -> ScanResponse:
    """
    Detect bottlenecks against current trace data and run the full loop
    (debate -> Layer 1 eval -> scorecard) for each newly-recorded trigger.
    Requires ANTHROPIC_API_KEY, FIREWORKS_API_KEY, OPENAI_API_KEY, and
    GOOGLE_API_KEY to be set -- this is the endpoint that makes real LLM
    calls, the one thing this whole project hasn't been able to verify
    live in this environment.
    """
    detector = TriggerDetector(pool)
    hits = await detector.scan(thresholds or _DEMO_RULES)
    recorded = await detector.record(hits)

    recorder = make_cost_recorder(pool, scope_key, operation="debate")
    try:
        orchestrator = LoopOrchestrator(
            pool, default_panel(), default_judge(),
            layer2_agent=default_layer2_agent(), on_call=recorder,
        )
    except Exception as exc:  # noqa: BLE001 -- most likely missing API keys
        raise HTTPException(
            500, f"could not construct debate panel (check API keys in .env): {exc}"
        ) from exc

    outcomes: list[DebateOutcome] = []
    errors: list[str] = []
    for trigger_id in recorded:
        try:
            await orchestrator.run(trigger_id)
            row = await pool.fetchrow(
                "SELECT d.id, d.state::text AS state, d.termination_reason "
                "FROM debates d JOIN triggers t ON t.debate_id = d.id "
                "WHERE t.id = $1", trigger_id,
            )
            if row:
                candidate_count = await pool.fetchval(
                    "SELECT COUNT(*) FROM candidates WHERE debate_id = $1", row["id"]
                )
                passed_count = await pool.fetchval(
                    "SELECT COUNT(*) FROM scorecards WHERE debate_id = $1 AND layer1_passed",
                    row["id"],
                )
                # The actual reason a debate closed -- including real agent
                # failure detail, not just "no candidates" -- lives in the
                # event log, not the debates row itself.
                detail_row = await pool.fetchrow(
                    "SELECT reason FROM debate_events WHERE debate_id = $1 "
                    "AND to_state IN ('REJECTED', 'PENDING_APPROVAL') "
                    "ORDER BY occurred_at DESC LIMIT 1",
                    row["id"],
                )
                outcomes.append(DebateOutcome(
                    debate_id=row["id"], state=row["state"],
                    termination_reason=row["termination_reason"],
                    candidates_proposed=candidate_count or 0,
                    candidates_passed_layer1=passed_count or 0,
                    detail=detail_row["reason"] if detail_row else None,
                ))
        except Exception as exc:  # noqa: BLE001
            log.error("debate failed for trigger %s: %s", trigger_id, exc)
            errors.append(f"trigger {trigger_id}: {exc}")

    return ScanResponse(
        triggers_found=len(recorded), debates_run=len(outcomes),
        outcomes=outcomes, errors=errors,
    )
