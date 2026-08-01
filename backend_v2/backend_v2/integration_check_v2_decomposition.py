"""
Applying an approved decomposition, verified against real Postgres (V2).

The escalation checks here are the point. `apply_generated()` re-runs
the capability validation at apply time rather than trusting the stored
proposal — validating only at generation would make the database a trust
boundary it was never designed to be, so a proposal tampered with in
storage would apply unchecked. These tests attempt exactly that.

Run:
    DATABASE_URL=postgresql://... python integration_check_v2_decomposition.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.db.session import create_pool
from app.models.change import ChangeSet
from app.services.knowledge_update import ChangeApplicationError, KnowledgeUpdater


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])
    updater = KnowledgeUpdater(pool)
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    print("-- applying a valid generated decomposition --")
    change_set = ChangeSet(ops=[
        {"op_type": "create_task_node", "ref": "t1", "name": "Ingest client PDFs",
         "description": "Receive and register incoming PDFs"},
        {"op_type": "create_task_node", "ref": "t2", "name": "Extract tables"},
        {"op_type": "create_task_node", "ref": "t3", "name": "Build summary charts"},
        {"op_type": "create_edge", "edge_type": "PRODUCES",
         "source_ref": "t1", "target_ref": "t2"},
        {"op_type": "create_edge", "edge_type": "PRODUCES",
         "source_ref": "t2", "target_ref": "t3"},
    ])
    outcome = await updater.apply_generated(change_set, approver_id="reviewer")

    check("all three nodes created", len(outcome["refs"]) == 3, str(outcome["refs"]))
    check("local refs resolved to real ids", all(outcome["refs"].values()))

    t1_id = outcome["refs"]["t1"]
    row = await pool.fetchrow(
        "SELECT name, provenance::text AS provenance FROM task_nodes WHERE id = $1", t1_id
    )
    check("node persisted with the proposed name", row["name"] == "Ingest client PDFs")
    check("tagged public_generated, never mistakable for company fact",
          row["provenance"] == "public_generated", row["provenance"])

    edges = await pool.fetch(
        "SELECT source_id, target_id FROM edges "
        "WHERE provenance = 'public_generated' AND source_id = ANY($1::uuid[])",
        [outcome["refs"][r] for r in ("t1", "t2", "t3")],
    )
    check("both edges created", len(edges) == 2, f"got {len(edges)}")

    created_ids = {str(v) for v in outcome["refs"].values()}
    touched = set()
    for e in edges:
        touched.update([str(e["source_id"]), str(e["target_id"])])
    check("edges wired only between the newly created nodes",
          touched <= created_ids)

    print("\n-- a tampered proposal cannot escalate at apply time --")
    # The scenario: a proposal passed the capability check at generation,
    # then was modified in storage before approval.
    hostile = ChangeSet(ops=[{
        "op_type": "update_task_node", "task_node_id": t1_id,
        "changes": {"name": "pwned"}, "reason": "injected",
    }])
    blocked = False
    try:
        await updater.apply_generated(hostile, approver_id="reviewer")
    except ChangeApplicationError as exc:
        blocked = "capability check" in str(exc)
    check("re-validated at apply time, not trusted from storage", blocked)

    unchanged = await pool.fetchrow("SELECT name FROM task_nodes WHERE id = $1", t1_id)
    check("the existing node was left untouched",
          unchanged["name"] == "Ingest client PDFs")

    print("\n-- generated content stays in its own subgraph --")
    sneaky = ChangeSet(ops=[
        {"op_type": "create_task_node", "ref": "n1", "name": "New step"},
        {"op_type": "create_edge", "edge_type": "PRODUCES",
         "source_ref": "n1", "target_id": t1_id, "target_table": "task_nodes"},
    ])
    blocked_attach = False
    try:
        await updater.apply_generated(sneaky, approver_id="reviewer")
    except ChangeApplicationError:
        blocked_attach = True
    check("an edge attaching to an existing node is refused", blocked_attach)

    await pool.close()
    print(f"\n{'=' * 55}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("V2 DECOMPOSITION APPLY VERIFIED against real Postgres.")


if __name__ == "__main__":
    asyncio.run(main())
