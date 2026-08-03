"""
Debate lifecycle state machine (MVP plan, Section 7.1).

One explicit transition table, in one module, with every transition
appended to debate_events. Scattering status updates across route
handlers is what makes a later migration to a durable workflow engine
(Section 12 trigger: Jalpa/Prover-Estimator branching, or concurrent
debates needing retries) an archaeology exercise instead of a
translation. Keeping it here means that migration reads this table and
reimplements it.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import asyncpg

from app.models.debate import DebateState

# to_state -> set of legal from_states.
TRANSITIONS: dict[DebateState, frozenset[DebateState]] = {
    "OPEN": frozenset(),
    "IN_DEBATE": frozenset({"OPEN"}),
    "PENDING_EVAL": frozenset({"IN_DEBATE"}),
    "PENDING_APPROVAL": frozenset({"PENDING_EVAL"}),
    "APPROVED": frozenset({"PENDING_APPROVAL"}),
    # REJECTED is reachable from any pre-decision state: a debate can fail
    # by producing no candidates, by every candidate failing eval, or by an
    # approver declining. All three are the same terminal state.
    "REJECTED": frozenset({"OPEN", "IN_DEBATE", "PENDING_EVAL", "PENDING_APPROVAL"}),
}

TERMINAL: frozenset[DebateState] = frozenset({"APPROVED", "REJECTED"})


class IllegalTransition(Exception):
    pass


def can_transition(from_state: DebateState, to_state: DebateState) -> bool:
    return from_state in TRANSITIONS.get(to_state, frozenset())


def assert_transition(from_state: DebateState, to_state: DebateState) -> None:
    if not can_transition(from_state, to_state):
        raise IllegalTransition(
            f"cannot move debate from {from_state} to {to_state}; "
            f"legal predecessors of {to_state} are "
            f"{sorted(TRANSITIONS.get(to_state, frozenset())) or '(none -- initial state)'}"
        )


class DebateStateMachine:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def current_state(self, debate_id: UUID) -> DebateState:
        row = await self._pool.fetchrow(
            "SELECT state::text AS state FROM debates WHERE id = $1", debate_id
        )
        if row is None:
            raise LookupError(f"no debate {debate_id}")
        return row["state"]

    async def transition(
        self,
        debate_id: UUID,
        to_state: DebateState,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> DebateState:
        """
        Move a debate to `to_state`, recording an immutable event.

        The whole thing runs in one transaction with a row lock so two
        concurrent workers can't both read OPEN and both advance it.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT state::text AS state FROM debates WHERE id = $1 FOR UPDATE",
                    debate_id,
                )
                if row is None:
                    raise LookupError(f"no debate {debate_id}")
                from_state: DebateState = row["state"]
                assert_transition(from_state, to_state)

                await conn.execute(
                    "UPDATE debates SET state = $2::debate_state, "
                    "closed_at = CASE WHEN $2 IN ('APPROVED','REJECTED') THEN now() ELSE closed_at END "
                    "WHERE id = $1",
                    debate_id, to_state,
                )
                await conn.execute(
                    "INSERT INTO debate_events (debate_id, from_state, to_state, reason, actor) "
                    "VALUES ($1, $2::debate_state, $3::debate_state, $4, $5)",
                    debate_id, from_state, to_state, reason, actor,
                )
        return to_state

    async def history(self, debate_id: UUID) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT from_state::text AS from_state, to_state::text AS to_state, "
            "reason, actor, occurred_at FROM debate_events "
            "WHERE debate_id = $1 ORDER BY occurred_at, id",
            debate_id,
        )
        return [dict(r) for r in rows]
