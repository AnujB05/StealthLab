"""
Minimal execution harness (internal skills only -- no external
marketplace, deliberately, see the design note in the conversation this
was built from).

This is the first thing in the project that actually *runs* a task
rather than proposing, debating, or evaluating a change to one. That's
a real architectural step, not an incremental feature: everything before
this assumed the product only observes and governs external execution.
This harness means it can also, optionally, execute a narrow, explicitly
registered set of trusted skills itself.

Deliberately NOT a plugin system and NOT connected to any external
skill/marketplace source yet. `SKILL_REGISTRY` is a closed, hand-written
Python dict -- the only thing `skill_ref` can currently resolve to is
something a developer explicitly wrote and reviewed. Wiring this up to
run arbitrary code from an external catalog is a distinct, later
decision that needs its own sandboxing and capability-boundary design,
not an extension of this file.

The concrete unblock this provides: traces produced here are real
execution outcomes, not synthetic demo seeding. That's what Layer 2's
Tier 2 (off-policy evaluation) has been blocked on since it was first
scoped -- the trace schema records executions, but nothing was ever
generating a genuine one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

import asyncpg

log = logging.getLogger(__name__)

# A skill takes whatever input dict the caller provides and returns an
# output dict. Raising is a legitimate way to signal failure -- the
# harness records it as 'failure', not a crash.
Skill = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ExecutionResult:
    trace_id: str
    outcome: str  # 'success' | 'failure' | 'needs_rework'
    latency_ms: int
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class SkillNotRegistered(Exception):
    pass


class SkillRegistry:
    """
    The closed set of skills this harness is willing to run.

    Registering a skill is a deliberate, reviewed code change -- there is
    no `register_from_string` or dynamic-import path here on purpose.
    That's what keeps this safe to run at all: the input to `execute()`
    is untrusted (task input, real or test data), but the code that runs
    is never untrusted, because it was never sourced from anywhere but
    this file.
    """

    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, name: str, fn: Skill) -> None:
        if name in self._skills:
            raise ValueError(f"skill {name!r} already registered")
        self._skills[name] = fn

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise SkillNotRegistered(
                f"skill_ref {name!r} is not registered. Registered skills: "
                f"{sorted(self._skills)}"
            ) from None

    def __contains__(self, name: str) -> bool:
        return name in self._skills


# --- Example skills, deliberately trivial ---
# Real skills go here as the harness gets real use. These exist to prove
# the harness itself works end to end, not as production functionality.

async def _echo_skill(input_data: dict[str, Any]) -> dict[str, Any]:
    """Always succeeds. Useful for verifying the harness's happy path."""
    return {"echoed": input_data}


async def _flaky_skill(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Fails if the input is missing a required field. Useful for verifying
    the harness's failure path produces a real 'failure' trace, not a
    crash.
    """
    if "required_field" not in input_data:
        raise ValueError("missing required_field")
    return {"processed": input_data["required_field"]}


def default_registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register("echo", _echo_skill)
    reg.register("flaky_example", _flaky_skill)
    return reg


class ExecutionHarness:
    """
    Runs a registered skill against real input and writes a real trace
    row. This is the actual point of this file -- everything else here
    exists to make this method trustworthy.
    """

    def __init__(self, pool: asyncpg.Pool, registry: Optional[SkillRegistry] = None):
        self._pool = pool
        self._registry = registry or default_registry()

    async def execute(
        self,
        task_node_id: UUID,
        skill_ref: str,
        input_data: dict[str, Any],
        actor_id: str = "execution_harness",
    ) -> ExecutionResult:
        skill = self._registry.get(skill_ref)  # raises SkillNotRegistered, not silently no-oped

        trace_id = f"exec-{task_node_id}-{time.time_ns()}"
        start = time.monotonic()
        try:
            output = await skill(input_data)
            outcome = "success"
            error = None
        except Exception as exc:  # noqa: BLE001
            # A skill failing is a real, expected outcome to record, not
            # something to propagate as a harness-level exception. The
            # whole point is capturing this as data.
            output = None
            outcome = "failure"
            error = str(exc)
            log.warning("skill %r failed for task %s: %s", skill_ref, task_node_id, exc)

        latency_ms = int((time.monotonic() - start) * 1000)

        await self._pool.execute(
            "INSERT INTO traces (trace_id, timestamp, task_node_id, actor_id, "
            "action_type, outcome, latency_ms) "
            "VALUES ($1, now(), $2, $3, 'execute_tool', $4, $5) "
            "ON CONFLICT (trace_id) DO NOTHING",
            trace_id, task_node_id, actor_id, outcome, latency_ms,
        )

        return ExecutionResult(
            trace_id=trace_id, outcome=outcome, latency_ms=latency_ms,
            output=output, error=error,
        )
