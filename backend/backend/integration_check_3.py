"""
Third live pass: ingestion endpoint, Layer1Evaluator against a real
GraphStore, and the full approval decide() endpoint -- the last three
gaps that had no live coverage. No real LLM calls (no credentials
available in this environment); the judge here is scripted, same as the
offline tests, but everything it touches (the graph lookups it drives)
is real.
"""
import asyncio
import os
import sys
from uuid import uuid4

sys.path.insert(0, os.path.dirname(__file__))

from app.api.approval import ApprovalRequest, decide
from app.api.ingest import ingest_traces
from app.db.graph_store import GraphStore
from app.db.session import create_pool
from app.eval.layer1 import Layer1Evaluator
from app.models.debate import Candidate, Citation
from app.onboarding.seed import Onboarder, TaskSpec, WorkflowSpec


class ScriptedJudge:
    """Same shape as tests/test_loop_logic.py's mocks -- no network."""
    agent_id, model_id, family = "judge", "mock", "independent"

    async def respond(self, system, user):
        import json
        return json.dumps({"fallacy_flags": [], "constructive": True, "notes": ""})


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    onboarder = Onboarder(pool)
    seeded = await onboarder.seed(WorkflowSpec(
        workflow_name="third_pass_check",
        tasks=[TaskSpec(key="target", name="Extract fields",
                        latency_estimate_ms=4000, cost_estimate=0.02)],
    ))
    task_id = seeded.task_ids["target"]

    # --- Ingestion endpoint against a real pool, not a mock ---
    print("-- Ingestion endpoint (real DB) --")
    payload = {
        "records": [
            {"trace_id": "live-1", "timestamp": "2026-07-31T00:00:00Z",
             "task_node_id": str(task_id), "outcome": "success"},
            {"trace_id": "live-2", "timestamp": "2026-07-31T00:00:01Z",
             "task_node_id": str(uuid4()), "outcome": "failure"},  # unseeded FK
            {"trace_id": "live-3", "outcome": "bad"},  # malformed
        ]
    }
    result = await ingest_traces(payload, pool=pool)
    check("valid record accepted", result.accepted == 1, f"accepted={result.accepted}")
    check("FK violation rejected per-record, not a crash", len(result.rejected) == 2)
    check("batch did not abort on first bad record",
          result.accepted + len(result.rejected) == 3)

    dup = await ingest_traces({"records": [payload["records"][0]]}, pool=pool)
    check("re-sending the same trace_id counts as duplicate, not error",
          dup.duplicates == 1 and dup.rejected == [])

    row = await pool.fetchrow("SELECT outcome FROM traces WHERE trace_id='live-1'")
    check("the accepted record actually persisted correctly",
          row is not None and row["outcome"] == "success")

    # --- Layer1Evaluator against a real GraphStore ---
    print("\n-- Layer1Evaluator + live GraphStore --")
    graph = GraphStore(pool)
    evaluator = Layer1Evaluator(ScriptedJudge(), graph)

    valid_cite_candidate = Candidate(
        debate_id=uuid4(), summary="s", rationale="r",
        change_set={"ops": [{
            "op_type": "update_task_node", "task_node_id": str(task_id),
            "changes": {"description": "cached"}, "reason": "perf",
        }]},
    )
    real_cite = [Citation(node_id=task_id, node_table="task_nodes")]
    l1 = await evaluator.evaluate(valid_cite_candidate, real_cite)
    check("a citation to a real, live node scores full groundedness",
          l1.groundedness_score == 1.0, f"got {l1.groundedness_score}")
    check("candidate with a real citation and clean judge passes", l1.passed)

    fake_cite = [Citation(node_id=uuid4(), node_table="task_nodes")]
    l1_bad = await evaluator.evaluate(valid_cite_candidate, fake_cite)
    check("a citation to a nonexistent node scores zero groundedness",
          l1_bad.groundedness_score == 0.0)
    check("nonexistent citation shows up in unresolved_cites",
          len(l1_bad.unresolved_cites) == 1)
    check("candidate with only a fake citation fails eval", not l1_bad.passed)

    mixed = [Citation(node_id=task_id, node_table="task_nodes"),
             Citation(node_id=uuid4(), node_table="task_nodes")]
    l1_mixed = await evaluator.evaluate(valid_cite_candidate, mixed)
    check("mixed real/fake citations score partial groundedness",
          abs(l1_mixed.groundedness_score - 0.5) < 0.01, f"got {l1_mixed.groundedness_score}")

    # --- Full approval endpoint, both decisions ---
    print("\n-- Approval endpoint decide() end to end --")
    trig = await pool.fetchrow(
        "INSERT INTO triggers (task_node_id, rule_name, metric_name, observed_value, "
        "threshold, sample_size, detail) VALUES ($1,'r','error_rate',0.5,0.1,20,$2) "
        "RETURNING id", task_id, {},
    )
    debate_row = await pool.fetchrow(
        "INSERT INTO debates (trigger_id, state) VALUES ($1, 'PENDING_APPROVAL') RETURNING id",
        trig["id"],
    )
    debate_id = debate_row["id"]

    change_set = {"ops": [{
        "op_type": "update_task_node", "task_node_id": str(task_id),
        "changes": {"latency_estimate_ms": 200}, "reason": "approval flow check",
    }]}
    cand_row = await pool.fetchrow(
        "INSERT INTO candidates (debate_id, summary, rationale, change_set, supporters) "
        "VALUES ($1,'test candidate','because', $2, $3) RETURNING id",
        debate_id, change_set, ["p0", "p1"],
    )
    sc_row = await pool.fetchrow(
        "INSERT INTO scorecards (debate_id, candidate_id, layer1_passed, groundedness_score, "
        "constructive, blast_radius, reversible, recommendation) "
        "VALUES ($1,$2,true,1.0,true,0,true,'looks fine') RETURNING id",
        debate_id, cand_row["id"],
    )

    response = await decide(
        sc_row["id"],
        ApprovalRequest(approver_id="checker", approver_role="ops_lead", decision="approved"),
        pool=pool,
    )
    check("decide() returns applied ops", len(response.applied_ops) == 1)
    check("decide() renders export markdown", response.export_markdown is not None
          and "checker" in response.export_markdown)

    new_id = response.applied_ops[0]["new_id"]
    new_row = await pool.fetchrow(
        "SELECT latency_estimate_ms FROM task_nodes WHERE id = $1", new_id
    )
    check("the approved change actually landed in the graph",
          new_row["latency_estimate_ms"] == 200)

    approval_row = await pool.fetchrow(
        "SELECT approver_role FROM approvals WHERE scorecard_id = $1", sc_row["id"]
    )
    check("approver_role was actually persisted (this was the bug fixed earlier)",
          approval_row["approver_role"] == "ops_lead")

    debate_state = await pool.fetchrow(
        "SELECT state::text AS s FROM debates WHERE id = $1", debate_id
    )
    check("debate transitioned to APPROVED", debate_state["s"] == "APPROVED")

    # Rejection path, on a second fresh debate
    trig2 = await pool.fetchrow(
        "INSERT INTO triggers (task_node_id, rule_name, metric_name, observed_value, "
        "threshold, sample_size, detail) VALUES ($1,'r2','error_rate',0.5,0.1,20,$2) "
        "RETURNING id", task_id, {},
    )
    debate2 = await pool.fetchrow(
        "INSERT INTO debates (trigger_id, state) VALUES ($1, 'PENDING_APPROVAL') RETURNING id",
        trig2["id"],
    )
    cand2 = await pool.fetchrow(
        "INSERT INTO candidates (debate_id, summary, rationale, change_set, supporters) "
        "VALUES ($1,'reject me','because', $2, $3) RETURNING id",
        debate2["id"], {"ops": []}, ["p0", "p1"],
    )
    sc2 = await pool.fetchrow(
        "INSERT INTO scorecards (debate_id, candidate_id, layer1_passed, groundedness_score, "
        "constructive, blast_radius, reversible, recommendation) "
        "VALUES ($1,$2,true,1.0,true,0,true,'n/a') RETURNING id",
        debate2["id"], cand2["id"],
    )
    reject_response = await decide(
        sc2["id"], ApprovalRequest(approver_id="checker", decision="rejected"), pool=pool,
    )
    check("rejection applies nothing", reject_response.applied_ops == [])
    reject_state = await pool.fetchrow(
        "SELECT state::text AS s FROM debates WHERE id = $1", debate2["id"]
    )
    check("debate transitioned to REJECTED", reject_state["s"] == "REJECTED")

    await pool.close()
    print(f"\n{'='*50}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("ALL CHECKS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
