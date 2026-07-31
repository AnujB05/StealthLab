"""
Vada debate engine (MVP plan, Section 7).

Fixed round-robin, not free-for-all: deciding who speaks next requires an
arbitration layer, which is real complexity for no v0 benefit.

Termination is convergence-based with a hard cap -- a full round in which
nobody proposes or amends means the panel has said what it has to say.
This is deliberately NOT the sequential/alpha-spending machinery from
Section 8.2; that governs statistical replay testing, a different problem
with different failure modes.

No candidate is declared "leading" during the debate. Winnowing happens
at eval, on evidence.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from app.config import settings
from app.debate.panel import PanelAgent, _extract_json, assert_heterogeneous, gather_responses
from app.debate.prompts import VADA_SYSTEM, build_user_prompt
from app.models.change import ChangeSet
from app.models.debate import Candidate, Citation, DebateResult, DebateTurn

log = logging.getLogger(__name__)


def _render_transcript(turns: list[DebateTurn], limit: int = 40) -> str:
    lines = []
    for t in turns[-limit:]:
        head = f"[round {t.round_number}] {t.speaker_id} ({t.action})"
        if t.candidate_id:
            head += f" -> candidate {t.candidate_id}"
        lines.append(f"{head}\n{t.content}")
    return "\n\n".join(lines)


def _render_candidates(candidates: list[Candidate]) -> str:
    if not candidates:
        return ""
    return "\n\n".join(
        f"### {c.id}\n{c.summary}\nsupporters: {len(c.supporters)} "
        f"({', '.join(c.supporters)})\nops: {len(c.change_set.ops)}"
        for c in candidates
    )


def _parse_cites(raw: Any) -> list[Citation]:
    cites: list[Citation] = []
    for item in raw or []:
        try:
            cites.append(Citation(**item))
        except Exception:  # noqa: BLE001 -- a malformed cite is not fatal
            log.debug("discarding unparseable citation: %r", item)
    return cites


def _parse_change_set(raw: Any) -> ChangeSet:
    """
    A malformed change_set must not kill the turn. It becomes an empty
    set, validate_ops() flags it as proposing no actual change, and the
    candidate fails its scorecard on the merits instead of vanishing.
    """
    if not raw:
        return ChangeSet()
    try:
        return ChangeSet(**raw) if isinstance(raw, dict) else ChangeSet()
    except Exception as exc:  # noqa: BLE001
        log.warning("unparseable change_set, treating as empty: %s", exc)
        return ChangeSet()


class DebateEngine:
    def __init__(
        self,
        agents: list[PanelAgent],
        max_rounds: Optional[int] = None,
        enforce_heterogeneity: bool = True,
    ):
        if enforce_heterogeneity:
            assert_heterogeneous(agents)
        self.agents = agents
        self.max_rounds = max_rounds or settings.max_debate_rounds

    async def run(
        self,
        debate_id: UUID,
        trigger_id: UUID,
        trigger_context: dict[str, Any],
        graph_context: str = "",
        on_turn: Optional[Callable[[DebateTurn], Any]] = None,
    ) -> DebateResult:
        turns: list[DebateTurn] = []
        candidates: list[Candidate] = []
        by_id: dict[UUID, Candidate] = {}
        agent_failures: list[str] = []
        rounds_used = 0
        termination = "round_cap"

        for round_number in range(1, self.max_rounds + 1):
            rounds_used = round_number
            user = build_user_prompt(
                trigger_context=trigger_context,
                graph_context=graph_context,
                transcript=_render_transcript(turns),
                candidates=_render_candidates(candidates),
                round_number=round_number,
                max_rounds=self.max_rounds,
            )
            replies = await gather_responses(self.agents, VADA_SYSTEM, user)

            round_had_movement = False
            for agent in self.agents:
                reply = replies[agent.agent_id]
                if isinstance(reply, Exception):
                    log.warning("agent %s failed in round %d: %s",
                                agent.agent_id, round_number, reply)
                    agent_failures.append(f"{agent.agent_id} (round {round_number}): {reply}")
                    continue

                try:
                    payload = _extract_json(reply)
                except ValueError as exc:
                    log.warning("agent %s returned unparseable output: %s",
                                agent.agent_id, exc)
                    continue

                action = payload.get("action", "pass")
                if action not in ("propose", "amend", "pass"):
                    log.warning("agent %s returned unknown action %r; treating as pass",
                                agent.agent_id, action)
                    action = "pass"

                content = str(payload.get("content", "")).strip()
                cites = _parse_cites(payload.get("cites"))
                candidate_id: Optional[UUID] = None

                if action == "propose":
                    cand = Candidate(
                        id=uuid4(),
                        debate_id=debate_id,
                        summary=str(payload.get("summary") or content[:120] or "(no summary)"),
                        rationale=content,
                        change_set=_parse_change_set(payload.get("change_set")),
                        supporters=[agent.agent_id],
                    )
                    candidates.append(cand)
                    by_id[cand.id] = cand
                    candidate_id = cand.id
                    round_had_movement = True

                elif action == "amend":
                    raw_id = payload.get("candidate_id")
                    target = None
                    if raw_id:
                        try:
                            target = by_id.get(UUID(str(raw_id)))
                        except (ValueError, AttributeError):
                            target = None
                    if target is None:
                        # Amending a nonexistent candidate is a malformed
                        # turn, not a proposal -- record it as a pass so the
                        # transcript stays honest about what happened.
                        log.warning("agent %s amended unknown candidate %r",
                                    agent.agent_id, raw_id)
                        action = "pass"
                    else:
                        target.add_supporter(agent.agent_id)
                        new_ops = _parse_change_set(payload.get("change_set"))
                        if new_ops.ops:
                            target.change_set = new_ops
                        target.rationale += f"\n\n[amended by {agent.agent_id}]\n{content}"
                        candidate_id = target.id
                        round_had_movement = True

                turn = DebateTurn(
                    debate_id=debate_id,
                    round_number=round_number,
                    speaker_id=agent.agent_id,
                    speaker_kind="agent",
                    model_used=agent.model_id,
                    action=action,  # type: ignore[arg-type]
                    candidate_id=candidate_id,
                    content=content,
                    cites=cites,
                )
                turns.append(turn)
                if on_turn:
                    await _maybe_await(on_turn(turn))

            if not round_had_movement:
                # Section 7: a full round with no proposal and no amendment
                # means the panel has converged (or has nothing to say).
                termination = "converged"
                break

        if not candidates:
            termination = "no_candidates"

        return DebateResult(
            debate_id=debate_id,
            trigger_id=trigger_id,
            rounds_used=rounds_used,
            termination_reason=termination,  # type: ignore[arg-type]
            turns=turns,
            candidates=candidates,
            agent_failures=agent_failures,
        )


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value
