"""
Live integration test against a real Postgres instance.

Not part of the offline suite (tests/test_loop_logic.py, tests/test_ingest.py)
-- this needs DATABASE_URL pointed at an actual Postgres and is meant to be
run explicitly, not on every offline test invocation. It exists to verify
the specific things a mock cannot: real transaction behavior, real CTE
recursion, real bi-temporal filtering, real FK enforcement at the
application layer.

Run: DATABASE_URL=postgresql://postgres:yourpassword@localhost/workflow_test \
     python3 integration_check.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.db.graph_store import GraphStore
from app.db.session import create_pool
from app.models.change import ChangeSet
from app.onboarding.seed import KnowledgeSpec, Onboarder, TaskSpec, WorkflowSpec
from app.services.knowledge_update import KnowledgeUpdater


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    # --- Seed a small real workflow via the actual Onboarder ---
    spec = WorkflowSpec(
        workflow_name="integration_check",
        tasks=[
            TaskSpec(key="intake", name="Intake"),
            TaskSpec(key="extract", name="Extract fields",
                    latency_estimate_ms=4000, cost_estimate=0.02),
            TaskSpec(key="review", name="Human review"),
        ],
        edges=[
            {"edge_type": "PRODUCES", "source": "intake", "target": "extract"},
            {"edge_type": "PRODUCES", "source": "extract", "target": "review"},
        ],
    )
    onboarder = Onboarder(pool)
    seeded = await onboarder.seed(spec)
    check("onboarding seeds real rows", len(seeded.task_ids) == 3)

    extract_id = seeded.task_ids["extract"]
    review_id = seeded.task_ids["review"]

    import datetime
    checkpoint = datetime.datetime.now(datetime.timezone.utc)
    await asyncio.sleep(0.05)  # ensure supersession gets a strictly later t_created

    # --- GraphStore.traverse_from: real recursive CTE ---
    print("\n-- GraphStore --")
    graph = GraphStore(pool)
    edges = await graph.traverse_from([extract_id], "task_nodes", max_depth=2)
    check("traverse_from finds the 2-hop neighborhood",
          len(edges) == 2, f"got {len(edges)} edges, expected 2")

    exists = await graph.node_exists(extract_id, "task_nodes")
    check("node_exists confirms a real, valid node", exists is True)

    blast = await graph.blast_radius(extract_id)
    check("blast_radius counts dependents", blast >= 1, f"got {blast}")

    # --- KnowledgeUpdater._supersede_task: the flagged-risky path ---
    print("\n-- KnowledgeUpdater (this is the code flagged as untested) --")
    updater = KnowledgeUpdater(pool)
    cs = ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": str(extract_id),
        "changes": {"latency_estimate_ms": 500, "description": "now cached"},
        "reason": "integration check",
    }])
    applied = await updater.apply(cs, approver_id="integration_check")
    check("apply() returns the new node id", "new_id" in applied[0])
    new_id = applied[0]["new_id"]

    old_row = await pool.fetchrow(
        "SELECT t_invalid FROM task_nodes WHERE id = $1", extract_id
    )
    check("old version was closed, not deleted", old_row is not None and old_row["t_invalid"] is not None)

    new_row = await pool.fetchrow(
        "SELECT name, description, latency_estimate_ms, cost_estimate, t_invalid "
        "FROM task_nodes WHERE id = $1", new_id
    )
    check("new version carries the change", new_row["latency_estimate_ms"] == 500)
    check("new version carries forward untouched fields",
          new_row["cost_estimate"] is not None and float(new_row["cost_estimate"]) == 0.02,
          "cost_estimate should have survived the merge unchanged")
    check("new version is currently valid", new_row["t_invalid"] is None)

    supersedes = await pool.fetchrow(
        "SELECT 1 FROM edges WHERE edge_type = 'SUPERSEDES' "
        "AND source_id = $1 AND target_id = $2", new_id, extract_id,
    )
    check("SUPERSEDES edge links new version to old", supersedes is not None)

    # The critical check: did edges pointing at the OLD extract node get
    # rewired to point at the NEW one? If not, "review" is now orphaned
    # from the workflow and it would fail silently in production.
    rewired = await pool.fetch(
        "SELECT source_id, target_id, edge_type::text AS edge_type FROM edges "
        "WHERE t_invalid IS NULL AND edge_type = 'PRODUCES' "
        "AND (source_id = $1 OR target_id = $1)", new_id,
    )
    check(
        "edges into/out of the old node were rewired to the new node",
        len(rewired) == 2,
        f"expected 2 live PRODUCES edges touching the new node, found {len(rewired)}",
    )

    old_edges_still_live = await pool.fetch(
        "SELECT 1 FROM edges WHERE t_invalid IS NULL AND edge_type = 'PRODUCES' "
        "AND (source_id = $1 OR target_id = $1)", extract_id,
    )
    check("no live edges remain pointing at the superseded old node",
          len(old_edges_still_live) == 0)

    # Confirm the graph is still walkable end to end through the new node.
    edges_after = await graph.traverse_from([seeded.task_ids["intake"]], "task_nodes", max_depth=3)
    reaches_review = any(
        (e.target_id == review_id or e.source_id == review_id) for e in edges_after
    )
    check("the workflow is still traversable intake -> ... -> review after supersession",
          reaches_review)

    # --- Bi-temporal filtering: point-in-time query ---
    # A real checkpoint, not an arbitrary lookback -- captured after
    # onboarding and before supersession, so "as_of" has an actual
    # pre-change graph state to reconstruct.
    print("\n-- Bi-temporal correctness --")
    old_as_of_checkpoint = await graph.get_neighbors(extract_id, "task_nodes", as_of=checkpoint)
    check("as_of before the change reflects the pre-supersession graph state",
          len(old_as_of_checkpoint) >= 1,
          f"got {len(old_as_of_checkpoint)} edges for the OLD node id as of the checkpoint")

    new_as_of_checkpoint = await graph.get_neighbors(new_id, "task_nodes", as_of=checkpoint)
    check("as_of before the change does not yet see the new node's edges",
          len(new_as_of_checkpoint) == 0,
          f"got {len(new_as_of_checkpoint)} edges for a node that didn't exist yet at checkpoint time")

    new_as_of_now = await graph.get_neighbors(new_id, "task_nodes")
    check("as_of now (default) sees the new node's edges",
          len(new_as_of_now) >= 1)

    await pool.close()

    print(f"\n{'='*50}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("ALL CHECKS PASSED against a real Postgres instance.")


if __name__ == "__main__":
    asyncio.run(main())
