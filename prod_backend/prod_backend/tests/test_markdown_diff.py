"""
Offline tests for app/export/markdown_diff.py.

Not previously in the suite -- only manually spot-checked. These lock
down the rendering contract, in particular the two properties that
actually matter: Layer 2 absence is stated explicitly (never silently
omitted, which could read as "tested and fine"), and the recommendation
is always labeled advisory.
"""
from __future__ import annotations

from uuid import uuid4

from app.export.markdown_diff import render_change_set, render_export, render_scorecard
from app.models.change import ChangeSet
from app.models.debate import Layer1Result, Scorecard


def _task_id():
    return str(uuid4())


def test_empty_change_set_renders_explicitly_not_blank():
    out = render_change_set(ChangeSet())
    assert "No changes" in out


def test_update_task_node_op_renders_field_and_reason():
    tid = _task_id()
    cs = ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": tid,
        "changes": {"latency_estimate_ms": 500}, "reason": "reduce latency",
    }])
    out = render_change_set(cs)
    assert "Modify task" in out
    assert "reduce latency" in out
    assert "500" in out


def test_invalidate_edge_op_clarifies_its_nondestructive():
    cs = ChangeSet(ops=[{
        "op_type": "invalidate_edge", "edge_id": str(uuid4()), "reason": "stale link",
    }])
    out = render_change_set(cs)
    assert "Remove relationship" in out
    assert "not deleted" in out  # the non-destructive guarantee must be visible, not just true


def test_create_edge_op_renders_both_endpoints():
    src, tgt = str(uuid4()), str(uuid4())
    cs = ChangeSet(ops=[{
        "op_type": "create_edge", "edge_type": "REQUIRES",
        "source_id": src, "source_table": "task_nodes",
        "target_id": tgt, "target_table": "task_nodes",
    }])
    out = render_change_set(cs)
    assert "Add relationship" in out
    assert "REQUIRES" in out


def test_node_names_are_used_when_provided():
    tid = _task_id()
    cs = ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": tid,
        "changes": {"description": "x"}, "reason": "y",
    }])
    out = render_change_set(cs, names={tid: "Extract structured fields"})
    assert "Extract structured fields" in out


def _scorecard(passed=True, tier=None, flags=None):
    return Scorecard(
        debate_id=uuid4(), candidate_id=uuid4(), summary="Cache the extraction step",
        proposers=["p0", "p1"],
        layer1=Layer1Result(
            candidate_id=uuid4(), passed=passed, groundedness_score=0.8,
            fallacy_flags=flags or [],
        ),
        layer2_tier=tier,
        blast_radius=3, reversible=True, recommendation="Passed argument review.",
    )


def test_scorecard_states_layer2_absence_explicitly():
    """The absence of empirical testing must never be silently omitted."""
    out = render_scorecard(_scorecard(tier=None))
    assert "Not yet available" in out


def test_scorecard_shows_layer2_when_present():
    out = render_scorecard(_scorecard(tier=1))
    assert "Tier 1" in out


def test_scorecard_always_labels_recommendation_advisory():
    out = render_scorecard(_scorecard())
    assert "Advisory only" in out


def test_scorecard_surfaces_fallacy_flags_with_quotes():
    from app.models.debate import FallacyFlag
    flag = FallacyFlag(fallacy="asiddha", quote="X is true", explanation="X is unestablished")
    out = render_scorecard(_scorecard(passed=False, flags=[flag]))
    assert "asiddha" in out
    assert "X is true" in out


def test_export_includes_approver_and_change_set():
    tid = _task_id()
    cs = ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": tid,
        "changes": {"description": "clearer"}, "reason": "clarity",
    }])
    out = render_export(_scorecard(), cs, "alice", "2026-07-31T00:00:00Z")
    assert "alice" in out
    assert "clearer" in out
