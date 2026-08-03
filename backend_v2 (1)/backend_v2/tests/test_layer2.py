"""
Tests for the Layer 2 evaluator (tier 3 simulated replay).

Uses a scripted agent rather than a real model — what's under test is the
aggregation, correction, and guard logic, not model behaviour.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.eval.layer2 import (
    TIER_SIMULATED,
    Layer2Result,
    ReplayObservation,
    SimulatedReplayEvaluator,
)
from app.eval.statistics import Verdict
from app.models.change import ChangeSet

TASK_ID = uuid4()


def _change_set():
    return ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": str(TASK_ID),
        "changes": {"latency_estimate_ms": 500}, "reason": "cache results",
    }])


def _trace(trace_id: str, outcome: str, latency: int = 4000, cost: float = 0.02):
    return {"trace_id": trace_id, "outcome": outcome, "latency_ms": latency,
            "cost": cost, "timestamp": None}


def _pool_with_traces(traces):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=traces)
    return pool


class ScriptedAgent:
    """Returns a fixed simulated outcome for every call."""

    agent_id, model_id, family = "sim", "mock", "mock"

    def __init__(self, outcome="success", latency=500, cost=0.01, raise_on=None):
        self.outcome, self.latency, self.cost = outcome, latency, cost
        self.raise_on = raise_on or set()
        self.calls = 0

    async def respond(self, system, user):
        self.calls += 1
        if self.calls in self.raise_on:
            raise RuntimeError("simulated provider failure")
        return json.dumps({
            "outcome": self.outcome, "latency_ms": self.latency,
            "cost": self.cost, "reasoning": "cached", "confidence": "medium",
        })


def test_insufficient_history_makes_no_statistical_claim():
    """
    Fewer traces than the minimum must produce an explicit refusal to
    compare -- not a comparison on 3 data points.
    """
    pool = _pool_with_traces([_trace(f"t{i}", "failure") for i in range(3)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent())
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set(), min_n=8))

    assert result.sufficient_data is False
    assert result.comparisons == []
    assert any("minimum" in n for n in result.notes)


def test_clear_improvement_is_detected():
    """All-failure baseline vs all-success simulation should register."""
    pool = _pool_with_traces([_trace(f"t{i}", "failure", 4000) for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent(outcome="success", latency=500))
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))

    assert result.sufficient_data is True
    success = next(c for c in result.comparisons if c.metric == "success_rate")
    assert success.verdict == Verdict.BETTER
    latency = next(c for c in result.comparisons if c.metric == "latency_ms")
    assert latency.verdict == Verdict.BETTER


def test_no_change_reports_no_detectable_difference():
    """A simulation identical to baseline must not report improvement."""
    pool = _pool_with_traces([_trace(f"t{i}", "success", 4000, 0.02) for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent(outcome="success", latency=4000, cost=0.02))
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))

    for comparison in result.comparisons:
        assert comparison.verdict == Verdict.NO_DETECTABLE_DIFFERENCE


def test_regression_is_detected_as_worse():
    pool = _pool_with_traces([_trace(f"t{i}", "success", 500) for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent(outcome="failure", latency=9000))
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))

    success = next(c for c in result.comparisons if c.metric == "success_rate")
    assert success.verdict == Verdict.WORSE


def test_failed_simulations_reduce_n_rather_than_being_guessed():
    """
    A provider failure must drop that observation, never substitute a
    fabricated one.
    """
    pool = _pool_with_traces([_trace(f"t{i}", "failure") for i in range(20)])
    agent = ScriptedAgent(raise_on={1, 2, 3})
    ev = SimulatedReplayEvaluator(pool, agent)
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))

    assert result.n_observations == 17


def test_too_many_failed_simulations_blocks_the_comparison():
    pool = _pool_with_traces([_trace(f"t{i}", "failure") for i in range(10)])
    agent = ScriptedAgent(raise_on=set(range(1, 9)))
    ev = SimulatedReplayEvaluator(pool, agent)
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set(), min_n=8))

    assert result.sufficient_data is False
    assert any("simulations succeeded" in n for n in result.notes)


def test_invalid_simulated_outcome_is_dropped():
    """A model returning an out-of-vocabulary outcome must not be trusted."""

    class BadAgent:
        agent_id, model_id, family = "bad", "m", "m"

        async def respond(self, system, user):
            return json.dumps({"outcome": "probably_fine", "latency_ms": 100})

    pool = _pool_with_traces([_trace(f"t{i}", "failure") for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, BadAgent())
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))

    assert result.n_observations == 0
    assert result.sufficient_data is False


def test_tier_is_always_labelled_as_simulation():
    """
    The scorecard must never be able to present this as measurement.
    """
    pool = _pool_with_traces([_trace(f"t{i}", "failure") for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent())
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))

    assert result.tier == TIER_SIMULATED
    assert "not measurement" in result.tier_label or "model opinion" in result.tier_label
    assert any("not observed measurements" in n for n in result.notes)


def test_value_delivered_requires_statistical_significance():
    """
    This field feeds a pricing conversation. It must stay None unless the
    improvement is real -- billing off noise would be indefensible.
    """
    result = Layer2Result(candidate_id=uuid4(), tier=TIER_SIMULATED)
    assert result.value_delivered is None

    pool = _pool_with_traces([_trace(f"t{i}", "success", 4000, 0.02) for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent(outcome="success", latency=4000, cost=0.02))
    unchanged = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))
    assert unchanged.value_delivered is None, "no real improvement means no billable value"


def test_value_delivered_populated_on_real_improvement():
    pool = _pool_with_traces([_trace(f"t{i}", "failure") for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent(outcome="success"))
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))
    assert result.value_delivered is not None
    assert result.value_delivered > 0


def test_metrics_dict_is_serializable_and_complete():
    pool = _pool_with_traces([_trace(f"t{i}", "failure") for i in range(20)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent())
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set()))

    payload = result.to_metrics_dict()
    json.dumps(payload)  # must round-trip for the JSONB column
    assert payload["tier"] == 3
    assert "tier_label" in payload
    assert len(payload["comparisons"]) > 0
    for comparison in payload["comparisons"]:
        assert "ci_lower" in comparison and "ci_upper" in comparison
        assert "p_value" in comparison and "verdict" in comparison


def test_underpowered_result_is_flagged_as_such():
    """
    'Not significant' with a tiny n must be distinguishable from
    'genuinely no effect'.
    """
    pool = _pool_with_traces([_trace(f"t{i}", "success", 4000, 0.02) for i in range(9)])
    ev = SimulatedReplayEvaluator(pool, ScriptedAgent(outcome="success", latency=4050, cost=0.021))
    result = asyncio.run(ev.evaluate(uuid4(), TASK_ID, _change_set(), min_n=8))

    assert result.sufficient_data is True
    assert any("underpowered" in n for n in result.notes)


# --- Wiring regression ---

def test_orchestrator_skips_layer2_when_no_agent_given():
    """Without an agent, Layer 2 is skipped cleanly -- no fabricated section."""
    from app.services.loop import LoopOrchestrator

    panel = [MagicMock(agent_id="a", family="fa"), MagicMock(agent_id="b", family="fb")]
    judge = MagicMock(agent_id="j", family="fj")
    orch = LoopOrchestrator(MagicMock(), panel, judge)
    assert orch._layer2 is None


def test_orchestrator_constructs_layer2_when_agent_given():
    """
    Regression guard: Layer 2 was fully built and fully unreachable,
    because the API constructed the orchestrator without an agent. This
    asserts the wire exists.
    """
    from app.services.loop import LoopOrchestrator

    panel = [MagicMock(agent_id="a", family="fa"), MagicMock(agent_id="b", family="fb")]
    judge = MagicMock(agent_id="j", family="fj")
    layer2_agent = MagicMock(agent_id="l2", family="fj")
    orch = LoopOrchestrator(MagicMock(), panel, judge, layer2_agent=layer2_agent)

    assert orch._layer2 is not None
    assert isinstance(orch._layer2, SimulatedReplayEvaluator)


def test_admin_endpoint_passes_a_layer2_agent():
    """
    The specific bug: run_scan built the orchestrator without a layer2
    agent, so Layer 2 never executed in any real run. Asserted at the
    source rather than trusting the call site stays correct.
    """
    import inspect
    from app.api import admin

    source = inspect.getsource(admin.run_scan)
    assert "layer2_agent" in source, "admin.run_scan must wire a Layer 2 agent"
