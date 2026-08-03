"""Ad-hoc check: does the new admin.py diagnostic query correctly
distinguish a silent all-agents-failed debate from a real one?
Not part of the permanent suite -- reuses MockAgent to stand in for the
LLM calls the FastAPI route can't make without real credentials, but
exercises the exact same SQL the endpoint now runs.
"""
import asyncio
import json
import os
from uuid import uuid4

from app.db.session import create_pool
from app.debate.engine import DebateEngine
from app.debate.panel import MockAgent
from app.debate.state_machine import DebateStateMachine
from app.onboarding.seed import Onboarder, TaskSpec, WorkflowSpec


async def diagnostic_query(pool, debate_id):
    row = await pool.fetchrow(
        "SELECT state::text AS state, termination_reason FROM debates WHERE id = $1",
        debate_id,
    )
    candidates = await pool.fetchval(
        "SELECT COUNT(*) FROM candidates WHERE debate_id = $1", debate_id
    )
    return dict(row), candidates


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])
    onboarder = Onboarder(pool)
    seeded = await onboarder.seed(WorkflowSpec(
        workflow_name="diagnostic_check",
        tasks=[TaskSpec(key="t", name="Some task")],
    ))
    task_id = seeded.task_ids["t"]
    machine = DebateStateMachine(pool)

    # Case 1: all agents fail (simulates missing API keys) -> silent REJECTED
    failing_agents = [
        MockAgent(agent_id="a", responses=[], family="fam_a"),
        MockAgent(agent_id="b", responses=[], family="fam_b"),
    ]
    trig1 = await pool.fetchrow(
        "INSERT INTO triggers (task_node_id, rule_name, metric_name, observed_value, "
        "threshold, sample_size, detail) VALUES ($1,'r','error_rate',0.5,0.1,20,$2) "
        "RETURNING id", task_id, {},
    )
    deb1 = await pool.fetchrow(
        "INSERT INTO debates (trigger_id) VALUES ($1) RETURNING id", trig1["id"]
    )
    await machine.transition(deb1["id"], "IN_DEBATE")
    result1 = await DebateEngine(failing_agents, enforce_heterogeneity=False).run(
        deb1["id"], trig1["id"], {}
    )
    await pool.execute(
        "UPDATE debates SET termination_reason = $2 WHERE id = $1",
        deb1["id"], result1.termination_reason,
    )
    await machine.transition(deb1["id"], "REJECTED", reason="panel produced no candidates")
    diag1 = await diagnostic_query(pool, deb1["id"])
    print("Case 1 (simulated missing keys):", diag1)
    assert diag1[0]["state"] == "REJECTED"
    assert diag1[0]["termination_reason"] == "no_candidates"
    assert diag1[1] == 0

    # Case 2: agents actually propose something -> real PENDING_APPROVAL-shaped outcome
    real_agents = [
        MockAgent(agent_id="a", responses=[json.dumps({
            "action": "propose", "summary": "fix it", "content": "reasoning",
            "change_set": {"ops": [{"op_type": "update_task_node",
                          "task_node_id": str(task_id),
                          "changes": {"description": "x"}, "reason": "y"}]},
        }), json.dumps({"action": "pass", "content": ""})], family="fam_c"),
        MockAgent(agent_id="b", responses=[json.dumps({
            "action": "amend", "candidate_id": "__PLACEHOLDER__", "content": "agreed",
        }), json.dumps({"action": "pass", "content": ""})], family="fam_d"),
    ]
    trig2 = await pool.fetchrow(
        "INSERT INTO triggers (task_node_id, rule_name, metric_name, observed_value, "
        "threshold, sample_size, detail) VALUES ($1,'r2','error_rate',0.5,0.1,20,$2) "
        "RETURNING id", task_id, {},
    )
    deb2 = await pool.fetchrow(
        "INSERT INTO debates (trigger_id) VALUES ($1) RETURNING id", trig2["id"]
    )
    await machine.transition(deb2["id"], "IN_DEBATE")
    result2 = await DebateEngine([real_agents[0]], enforce_heterogeneity=False).run(
        deb2["id"], trig2["id"], {}
    )
    for c in result2.candidates:
        await pool.execute(
            "INSERT INTO candidates (id, debate_id, summary, rationale, change_set, supporters) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            c.id, c.debate_id, c.summary, c.rationale,
            c.change_set.model_dump(mode="json"), c.supporters,
        )
    await pool.execute(
        "UPDATE debates SET termination_reason = $2 WHERE id = $1",
        deb2["id"], result2.termination_reason,
    )
    diag2 = await diagnostic_query(pool, deb2["id"])
    print("Case 2 (real candidate proposed):", diag2)
    assert diag2[1] == 1, f"expected 1 candidate, got {diag2[1]}"

    print("\nDiagnostic correctly distinguishes both cases.")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
