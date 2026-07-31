"""
The loop: trigger -> debate -> eval -> scorecard -> (awaits approval).

Per Section 11 this is eventually represented as TaskNodes in the graph
it operates on. For v0 it is plain code -- self-representation is a real
property to build, but building it before the loop itself works would be
solving the harder problem first.

State transitions all route through DebateStateMachine rather than being
written inline here, so the lifecycle stays described in exactly one
place (Section 7.1).
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

import asyncpg

from app.config import settings
from app.db.graph_store import GraphStore
from app.debate.engine import DebateEngine
from app.debate.panel import PanelAgent
from app.debate.state_machine import DebateStateMachine
from app.eval.layer1 import Layer1Evaluator, enforce_independence
from app.models.debate import Candidate, DebateResult, Scorecard

log = logging.getLogger(__name__)


async def _render_graph_context(graph: GraphStore, task_node_id: UUID,
                                pool: asyncpg.Pool) -> str:
    """
    Assemble the neighbourhood of the flagged node for the panel prompt.

    Panelists cannot cite what they were never shown, so the quality of
    this context directly caps the achievable groundedness score.
    """
    lines: list[str] = []
    focus = await pool.fetchrow(
        "SELECT id, name, description, io_schema, skill_ref, cost_estimate, "
        "latency_estimate_ms FROM task_nodes WHERE id = $1", task_node_id
    )
    if focus:
        lines.append(f"### Flagged task (task_nodes:{focus['id']})")
        lines.append(f"name: {focus['name']}")
        if focus["description"]:
            lines.append(f"description: {focus['description']}")
        if focus["skill_ref"]:
            lines.append(f"skill: {focus['skill_ref']}")
        lines.append(f"io_schema: {json.dumps(focus['io_schema'])}")
        lines.append("")

    edges = await graph.traverse_from([task_node_id], "task_nodes", max_depth=2)
    if edges:
        lines.append("### Related")
        for e in edges[:50]:
            label = e.custom_edge_type or e.edge_type
            lines.append(
                f"- {e.source_table}:{e.source_id} —[{label}]→ "
                f"{e.target_table}:{e.target_id}"
            )
    return "\n".join(lines)


class LoopOrchestrator:
    def __init__(
        self,
        pool: asyncpg.Pool,
        panel: list[PanelAgent],
        judge: PanelAgent,
    ):
        enforce_independence(judge, panel)
        self._pool = pool
        self._graph = GraphStore(pool)
        self._engine = DebateEngine(panel)
        self._evaluator = Layer1Evaluator(judge, self._graph)
        self._machine = DebateStateMachine(pool)

    async def open_debate(self, trigger_id: UUID) -> UUID:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO debates (trigger_id) VALUES ($1) RETURNING id", trigger_id
                )
                debate_id = row["id"]
                await conn.execute(
                    "UPDATE triggers SET debate_id = $2 WHERE id = $1", trigger_id, debate_id
                )
                await conn.execute(
                    "INSERT INTO debate_events (debate_id, from_state, to_state, reason) "
                    "VALUES ($1, NULL, 'OPEN', 'trigger fired')",
                    debate_id,
                )
        return debate_id

    async def run(self, trigger_id: UUID) -> list[Scorecard]:
        trigger = await self._pool.fetchrow(
            "SELECT * FROM triggers WHERE id = $1", trigger_id
        )
        if trigger is None:
            raise LookupError(f"no trigger {trigger_id}")

        debate_id = trigger["debate_id"] or await self.open_debate(trigger_id)
        await self._machine.transition(debate_id, "IN_DEBATE", reason="panel convened")

        context = await _render_graph_context(
            self._graph, trigger["task_node_id"], self._pool
        )
        trigger_context = {
            "rule": trigger["rule_name"],
            "metric": trigger["metric_name"],
            "observed": float(trigger["observed_value"]),
            "threshold": float(trigger["threshold"]),
            "sample_size": trigger["sample_size"],
        }

        result = await self._engine.run(
            debate_id=debate_id,
            trigger_id=trigger_id,
            trigger_context=trigger_context,
            graph_context=context,
        )
        await self._persist_debate(result)

        if result.termination_reason == "no_candidates":
            reason = "panel produced no candidates"
            if result.agent_failures:
                reason += "; " + "; ".join(result.agent_failures)
            await self._machine.transition(debate_id, "REJECTED", reason=reason)
            return []

        await self._machine.transition(
            debate_id, "PENDING_EVAL",
            reason=f"{result.termination_reason} after {result.rounds_used} round(s)",
        )

        eligible = result.eligible_candidates(settings.min_supporters_for_eval)
        if not eligible:
            await self._machine.transition(
                debate_id, "REJECTED",
                reason=f"no candidate reached {settings.min_supporters_for_eval} supporters",
            )
            return []

        scorecards = await self._evaluate(result, eligible, trigger["task_node_id"])

        if not any(sc.layer1.passed for sc in scorecards):
            await self._machine.transition(
                debate_id, "REJECTED", reason="all candidates failed Layer 1"
            )
        else:
            await self._machine.transition(
                debate_id, "PENDING_APPROVAL",
                reason=f"{sum(sc.layer1.passed for sc in scorecards)} candidate(s) passed",
            )
        return scorecards

    async def _evaluate(
        self, result: DebateResult, eligible: list[Candidate], task_node_id: UUID
    ) -> list[Scorecard]:
        cites_by_candidate: dict[UUID, list] = {}
        for turn in result.turns:
            if turn.candidate_id:
                cites_by_candidate.setdefault(turn.candidate_id, []).extend(turn.cites)

        scorecards: list[Scorecard] = []
        for cand in eligible:
            layer1 = await self._evaluator.evaluate(
                cand, cites_by_candidate.get(cand.id, [])
            )
            blast = await self._graph.blast_radius(task_node_id)
            sc = Scorecard(
                debate_id=result.debate_id,
                candidate_id=cand.id,
                summary=cand.summary,
                proposers=cand.supporters,
                layer1=layer1,
                blast_radius=blast,
                # Every op type is invalidate-and-append, so any approved
                # change can be walked back. Revisit if a destructive op
                # type is ever added.
                reversible=True,
                recommendation=_recommend(layer1, blast),
            )
            await self._persist_scorecard(sc)
            scorecards.append(sc)
        return scorecards

    async def _persist_debate(self, result: DebateResult) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE debates SET round_number = $2, termination_reason = $3 WHERE id = $1",
                    result.debate_id, result.rounds_used, result.termination_reason,
                )
                for c in result.candidates:
                    await conn.execute(
                        "INSERT INTO candidates (id, debate_id, summary, rationale, "
                        "change_set, supporters) VALUES ($1,$2,$3,$4,$5,$6)",
                        c.id, c.debate_id, c.summary, c.rationale,
                        c.change_set.model_dump(mode="json"), c.supporters,
                    )
                for t in result.turns:
                    await conn.execute(
                        "INSERT INTO debate_turns (id, debate_id, round_number, speaker_id, "
                        "speaker_kind, speaker_role, model_used, action, candidate_id, content, cites) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                        t.id, t.debate_id, t.round_number, t.speaker_id, t.speaker_kind,
                        t.speaker_role, t.model_used, t.action, t.candidate_id, t.content,
                        [c.model_dump(mode="json") for c in t.cites],
                    )
                # The transcript is an episode: Section 3.1's non-lossy
                # audit layer covers debates, not just ingested documents.
                await conn.execute(
                    "INSERT INTO episodes (episode_type, content, metadata) "
                    "VALUES ('debate_transcript', $1, $2)",
                    "\n\n".join(f"[{t.round_number}] {t.speaker_id}: {t.content}"
                                for t in result.turns),
                    {"debate_id": str(result.debate_id)},
                )

    async def _persist_scorecard(self, sc: Scorecard) -> None:
        l1 = sc.layer1
        await self._pool.execute(
            "INSERT INTO scorecards (id, debate_id, candidate_id, layer1_passed, "
            "fallacy_flags, constructive, groundedness_score, unresolved_cites, "
            "blast_radius, reversible, recommendation) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            sc.id, sc.debate_id, sc.candidate_id, l1.passed,
            [f.model_dump() for f in l1.fallacy_flags],
            l1.constructive, l1.groundedness_score,
            l1.unresolved_cites, sc.blast_radius, sc.reversible,
            sc.recommendation,
        )


def _recommend(layer1, blast_radius: int) -> str:
    """
    Advisory text only (Section 8.3). Phrased as observations rather than
    a verdict: the approver decides, and a recommendation that reads like
    a decision invites rubber-stamping.
    """
    if not layer1.passed:
        reasons = []
        if layer1.fallacy_flags:
            reasons.append(f"{len(layer1.fallacy_flags)} reasoning flag(s)")
        if not layer1.constructive:
            reasons.append("no alternative proposed")
        if layer1.structural_problems:
            reasons.append("change set problems")
        if layer1.groundedness_score < settings.groundedness_threshold:
            reasons.append(f"low groundedness ({layer1.groundedness_score:.2f})")
        return "Did not pass argument review: " + ", ".join(reasons) + "."
    note = "Passed argument review."
    if blast_radius > 5:
        note += (
            f" Note {blast_radius} dependent tasks — wider impact than average,"
            " worth checking downstream effects."
        )
    note += " No empirical testing has been run; this assesses the argument only."
    return note
