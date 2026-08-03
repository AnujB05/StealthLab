"""
Tests for the internal execution harness.

Uses a mocked pool since the point under test is the harness's own logic
(registry lookup, success/failure capture, trace_id construction), not
SQL correctness -- that's covered live in the conversation this was
verified against, with a real trigger genuinely forming from real
execution data.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services.execution import (
    ExecutionHarness,
    SkillNotRegistered,
    SkillRegistry,
    default_registry,
)

TASK_ID = uuid4()


def _pool():
    pool = MagicMock()
    pool.execute = AsyncMock()
    return pool


# --- Registry ---

def test_registry_rejects_duplicate_registration():
    reg = SkillRegistry()
    reg.register("a", AsyncMock())
    with pytest.raises(ValueError, match="already registered"):
        reg.register("a", AsyncMock())


def test_unregistered_skill_raises_not_silently_missing():
    reg = SkillRegistry()
    with pytest.raises(SkillNotRegistered, match="not registered"):
        reg.get("nope")


def test_default_registry_has_the_example_skills():
    reg = default_registry()
    assert "echo" in reg
    assert "flaky_example" in reg


# --- Harness: success path ---

def test_successful_skill_produces_a_success_trace():
    async def good_skill(input_data):
        return {"result": input_data["x"] * 2}

    reg = SkillRegistry()
    reg.register("double", good_skill)
    pool = _pool()
    harness = ExecutionHarness(pool, registry=reg)

    result = asyncio.run(harness.execute(TASK_ID, "double", {"x": 5}))

    assert result.outcome == "success"
    assert result.output == {"result": 10}
    assert result.error is None
    pool.execute.assert_called_once()


# --- Harness: failure path (the case that matters most) ---

def test_a_skill_raising_produces_a_failure_trace_not_a_crash():
    async def bad_skill(input_data):
        raise RuntimeError("simulated real failure")

    reg = SkillRegistry()
    reg.register("bad", bad_skill)
    pool = _pool()
    harness = ExecutionHarness(pool, registry=reg)

    result = asyncio.run(harness.execute(TASK_ID, "bad", {}))

    assert result.outcome == "failure"
    assert result.output is None
    assert "simulated real failure" in result.error
    # The failure itself is what gets recorded -- confirm a trace was
    # still written, not skipped because the skill raised.
    pool.execute.assert_called_once()


def test_unregistered_skill_writes_no_trace_at_all():
    """
    A skill_ref that doesn't exist is a configuration error, not an
    execution outcome -- it must not be recorded as a fake 'failure'
    trace, which would misrepresent real skill reliability data.
    """
    pool = _pool()
    harness = ExecutionHarness(pool, registry=SkillRegistry())

    with pytest.raises(SkillNotRegistered):
        asyncio.run(harness.execute(TASK_ID, "ghost", {}))

    pool.execute.assert_not_called()


# --- trace_id construction ---

def test_trace_id_is_unique_per_call():
    """
    Repeated calls must not collide on trace_id -- traces.trace_id is a
    primary key, and this project has hit exactly this collision bug
    three separate times in other scripts already.
    """
    async def ok(input_data):
        return {}

    reg = SkillRegistry()
    reg.register("ok", ok)
    pool = _pool()
    harness = ExecutionHarness(pool, registry=reg)

    r1 = asyncio.run(harness.execute(TASK_ID, "ok", {}))
    r2 = asyncio.run(harness.execute(TASK_ID, "ok", {}))

    assert r1.trace_id != r2.trace_id


def test_trace_id_embeds_the_task_id():
    async def ok(input_data):
        return {}

    reg = SkillRegistry()
    reg.register("ok", ok)
    pool = _pool()
    harness = ExecutionHarness(pool, registry=reg)

    result = asyncio.run(harness.execute(TASK_ID, "ok", {}))
    assert str(TASK_ID) in result.trace_id


# --- latency measurement ---

def test_latency_is_measured_not_hardcoded():
    async def slow(input_data):
        await asyncio.sleep(0.05)
        return {}

    reg = SkillRegistry()
    reg.register("slow", slow)
    pool = _pool()
    harness = ExecutionHarness(pool, registry=reg)

    result = asyncio.run(harness.execute(TASK_ID, "slow", {}))
    assert result.latency_ms >= 40  # allow scheduling slack, must reflect the real sleep
