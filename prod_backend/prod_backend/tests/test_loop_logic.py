"""
Offline tests for the parts of the loop that don't need a database.

These cover the logic most likely to be wrong in ways that are hard to
see by reading: convergence detection, supporter counting, illegal state
transitions, and the fail-closed behaviour of the Layer 1 gate.
"""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from app.debate.engine import DebateEngine
from app.debate.panel import MockAgent, _extract_json, assert_heterogeneous
from app.debate.state_machine import IllegalTransition, assert_transition, can_transition
from app.eval.layer1 import JudgeNotIndependent, Layer1Evaluator, enforce_independence
from app.models.change import ChangeSet
from app.models.debate import Candidate

TASK_ID = str(uuid4())


def _propose(summary="cache the extraction step", field="latency_estimate_ms", value=500):
    return json.dumps({
        "action": "propose",
        "summary": summary,
        "content": f"The step is slow; {summary}.",
        "cites": [{"node_id": TASK_ID, "node_table": "task_nodes"}],
        "change_set": {"ops": [{
            "op_type": "update_task_node", "task_node_id": TASK_ID,
            "changes": {field: value}, "reason": "reduce latency",
        }]},
    })


def _pass():
    return json.dumps({"action": "pass", "content": "nothing to add"})


def _amend(candidate_id):
    return json.dumps({
        "action": "amend", "candidate_id": str(candidate_id),
        "content": "agreed, tightening the estimate",
        "change_set": {"ops": [{
            "op_type": "update_task_node", "task_node_id": TASK_ID,
            "changes": {"latency_estimate_ms": 400}, "reason": "refined",
        }]},
    })


def _mock_panel(scripts):
    return [
        MockAgent(agent_id=f"p{i}", responses=s, family=f"fam{i}")
        for i, s in enumerate(scripts)
    ]


def test_json_extraction_survives_fences_and_prose():
    assert _extract_json('```json\n{"a": 1}\n```')["a"] == 1
    assert _extract_json('Sure! {"a": 2} hope that helps')["a"] == 2
    with pytest.raises(ValueError):
        _extract_json("no json here at all")
    with pytest.raises(ValueError):
        _extract_json("[1,2,3]")  # array, not object


def test_heterogeneity_is_enforced():
    same = [MockAgent(agent_id="a", responses=[], family="x"),
            MockAgent(agent_id="b", responses=[], family="x")]
    with pytest.raises(ValueError, match="not heterogeneous"):
        assert_heterogeneous(same)
    assert_heterogeneous(_mock_panel([[], []]))  # distinct families: fine


def test_debate_converges_when_a_full_round_passes():
    """A round where nobody proposes or amends means the panel is done."""
    panel = _mock_panel([[_propose(), _pass()], [_pass(), _pass()]])
    engine = DebateEngine(panel, max_rounds=5)
    result = asyncio.run(engine.run(uuid4(), uuid4(), {"metric": "latency"}))
    assert result.termination_reason == "converged"
    assert result.rounds_used == 2  # round 1 proposed, round 2 was silent
    assert len(result.candidates) == 1


def test_debate_stops_at_round_cap():
    """Agents that keep proposing must still be bounded."""
    panel = _mock_panel([[_propose() for _ in range(10)],
                         [_propose() for _ in range(10)]])
    engine = DebateEngine(panel, max_rounds=3)
    result = asyncio.run(engine.run(uuid4(), uuid4(), {}))
    assert result.termination_reason == "round_cap"
    assert result.rounds_used == 3


def test_no_candidates_is_distinct_from_convergence():
    panel = _mock_panel([[_pass()], [_pass()]])
    result = asyncio.run(DebateEngine(panel).run(uuid4(), uuid4(), {}))
    assert result.termination_reason == "no_candidates"
    assert result.candidates == []


def test_amend_adds_a_supporter_and_gates_eval():
    """Section 7: >=2 supporters to reach eval. One proposal alone shouldn't."""
    p0 = MockAgent(agent_id="p0", responses=[_propose(), _pass()], family="a")
    engine = DebateEngine([p0], max_rounds=2, enforce_heterogeneity=False)
    result = asyncio.run(engine.run(uuid4(), uuid4(), {}))
    assert len(result.candidates) == 1
    assert result.eligible_candidates(min_supporters=2) == []
    assert len(result.eligible_candidates(min_supporters=1)) == 1


def test_amending_unknown_candidate_is_recorded_as_pass():
    """A malformed amend must not invent a candidate or crash the round."""
    panel = _mock_panel([[_amend(uuid4()), _pass()], [_pass(), _pass()]])
    result = asyncio.run(DebateEngine(panel).run(uuid4(), uuid4(), {}))
    assert result.candidates == []
    assert result.turns[0].action == "pass"


def test_agent_failure_does_not_abort_the_round():
    class Exploding:
        agent_id, model_id, family = "bad", "m", "boom"

        async def respond(self, system, user):
            raise RuntimeError("rate limited")

    panel = [Exploding(), MockAgent(agent_id="ok", responses=[_propose(), _pass()], family="ok")]
    result = asyncio.run(DebateEngine(panel).run(uuid4(), uuid4(), {}))
    assert len(result.candidates) == 1  # the healthy agent still contributed
    assert all(t.speaker_id == "ok" for t in result.turns)


def test_state_machine_rejects_skipped_steps():
    assert can_transition("OPEN", "IN_DEBATE")
    assert not can_transition("OPEN", "APPROVED")
    assert not can_transition("APPROVED", "IN_DEBATE")  # terminal
    with pytest.raises(IllegalTransition, match="cannot move debate"):
        assert_transition("PENDING_EVAL", "APPROVED")
    # REJECTED is reachable from every pre-decision state
    for s in ("OPEN", "IN_DEBATE", "PENDING_EVAL", "PENDING_APPROVAL"):
        assert can_transition(s, "REJECTED")


def test_change_set_validation():
    empty = ChangeSet()
    assert any("empty" in p for p in empty.validate_ops())

    protected = ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": TASK_ID,
        "changes": {"tenant_id": "x"}, "reason": "sneaky",
    }])
    assert any("protected field" in p for p in protected.validate_ops())

    ok = ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": TASK_ID,
        "changes": {"description": "clearer"}, "reason": "clarity",
    }])
    assert ok.validate_ops() == []


def test_judge_must_be_independent():
    panel = _mock_panel([[], []])
    with pytest.raises(JudgeNotIndependent, match="also a panelist"):
        enforce_independence(panel[0], panel)
    same_family = MockAgent(agent_id="judge", responses=[], family="fam0")
    with pytest.raises(JudgeNotIndependent, match="family"):
        enforce_independence(same_family, panel)
    enforce_independence(MockAgent(agent_id="j", responses=[], family="other"), panel)


def test_layer1_fails_closed_when_judge_is_unavailable():
    """A broken judge must never yield a passing scorecard."""

    class DeadJudge:
        agent_id, model_id, family = "j", "m", "j"

        async def respond(self, system, user):
            raise RuntimeError("api down")

    cand = Candidate(
        debate_id=uuid4(), summary="s", rationale="r",
        change_set=ChangeSet(ops=[{
            "op_type": "update_task_node", "task_node_id": TASK_ID,
            "changes": {"description": "x"}, "reason": "y",
        }]),
    )
    result = asyncio.run(Layer1Evaluator(DeadJudge()).evaluate(cand))
    assert result.passed is False
    assert "judge unavailable" in result.notes


def test_layer1_discards_invented_fallacy_categories():
    class InventiveJudge:
        agent_id, model_id, family = "j", "m", "j"

        async def respond(self, system, user):
            return json.dumps({
                "fallacy_flags": [
                    {"fallacy": "vibes_based", "quote": "q", "explanation": "e"},
                    {"fallacy": "asiddha", "quote": "q2", "explanation": "e2"},
                ],
                "constructive": True, "notes": "",
            })

    cand = Candidate(debate_id=uuid4(), summary="s", rationale="r",
                     change_set=ChangeSet(ops=[{
                         "op_type": "update_task_node", "task_node_id": TASK_ID,
                         "changes": {"description": "x"}, "reason": "y"}]))
    result = asyncio.run(Layer1Evaluator(InventiveJudge()).evaluate(cand))
    assert [f.fallacy for f in result.fallacy_flags] == ["asiddha"]


def test_uncited_proposal_scores_zero_groundedness():
    class CleanJudge:
        agent_id, model_id, family = "j", "m", "j"

        async def respond(self, system, user):
            return json.dumps({"fallacy_flags": [], "constructive": True, "notes": ""})

    cand = Candidate(debate_id=uuid4(), summary="s", rationale="r",
                     change_set=ChangeSet(ops=[{
                         "op_type": "update_task_node", "task_node_id": TASK_ID,
                         "changes": {"description": "x"}, "reason": "y"}]))
    result = asyncio.run(Layer1Evaluator(CleanJudge()).evaluate(cand, cited=[]))
    assert result.groundedness_score == 0.0
    assert result.passed is False  # clean argument, but nothing anchors it
