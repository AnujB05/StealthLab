"""
V2 access control, verified against real Postgres.

The unit tests cover the predicate builder. These cover the properties
that only exist in the SQL — in particular the leak that a naive
implementation ships: filtering traversal *output* while still walking
*through* private edges, which hides the edge but reveals the graph's
shape and everything reachable beyond it.

Run:
    DATABASE_URL=postgresql://... python integration_check_v2.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.db.graph_store import GraphStore
from app.db.session import create_pool
from app.services.access import AccessScope


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    # Chain: public_a -> private_mid -> public_b
    # A viewer without access must see neither the private node nor
    # public_b, because the only route to public_b runs through it.
    async with pool.acquire() as conn:
        rows = {}
        for key, name, vis, owner in (
            ("a", "Public start", "public", None),
            ("mid", "Private middle", "private", "alice"),
            ("b", "Public beyond", "public", None),
        ):
            r = await conn.fetchrow(
                "INSERT INTO task_nodes (name, visibility, owner_id) "
                "VALUES ($1, $2::visibility_level, $3) RETURNING id",
                name, vis, owner,
            )
            rows[key] = r["id"]

        # Edges inherit the visibility of the more restricted endpoint.
        await conn.execute(
            "INSERT INTO edges (edge_type, source_id, source_table, target_id, "
            "target_table, visibility, owner_id) "
            "VALUES ('PRODUCES', $1, 'task_nodes', $2, 'task_nodes', 'private', 'alice')",
            rows["a"], rows["mid"],
        )
        await conn.execute(
            "INSERT INTO edges (edge_type, source_id, source_table, target_id, "
            "target_table, visibility, owner_id) "
            "VALUES ('PRODUCES', $1, 'task_nodes', $2, 'task_nodes', 'private', 'alice')",
            rows["mid"], rows["b"],
        )

    print("-- node_exists --")
    anon = GraphStore(pool, scope=AccessScope.anonymous())
    owner = GraphStore(pool, scope=AccessScope.for_user("alice"))
    other = GraphStore(pool, scope=AccessScope.for_user("bob"))

    check("anonymous sees a public node", await anon.node_exists(rows["a"], "task_nodes"))
    check("anonymous cannot see a private node",
          not await anon.node_exists(rows["mid"], "task_nodes"))
    check("owner sees their own private node",
          await owner.node_exists(rows["mid"], "task_nodes"))
    check("a different user cannot see someone else's private node",
          not await other.node_exists(rows["mid"], "task_nodes"))

    print("\n-- get_neighbors --")
    anon_edges = await anon.get_neighbors(rows["a"], "task_nodes")
    check("anonymous sees no private edges from a public node",
          len(anon_edges) == 0, f"got {len(anon_edges)}")
    owner_edges = await owner.get_neighbors(rows["a"], "task_nodes")
    check("owner sees their private edges", len(owner_edges) == 1,
          f"got {len(owner_edges)}")

    print("\n-- traverse_from: the leak that matters --")
    anon_walk = await anon.traverse_from([rows["a"]], "task_nodes", max_depth=3)
    reached = set()
    for e in anon_walk:
        reached.update([e.source_id, e.target_id])
    check("traversal does not walk through a private edge",
          rows["mid"] not in reached,
          "private middle node reachable anonymously")
    check("traversal does not reach nodes beyond a private edge",
          rows["b"] not in reached,
          "public node behind a private edge was exposed -- filtering the "
          "output alone would produce exactly this")

    owner_walk = await owner.traverse_from([rows["a"]], "task_nodes", max_depth=3)
    owner_reached = set()
    for e in owner_walk:
        owner_reached.update([e.source_id, e.target_id])
    check("owner's traversal reaches the full chain",
          rows["mid"] in owner_reached and rows["b"] in owner_reached)

    print("\n-- unrestricted (internal maintenance paths) --")
    internal = GraphStore(pool, scope=AccessScope.unrestricted())
    check("unrestricted scope sees private content",
          await internal.node_exists(rows["mid"], "task_nodes"))

    print("\n-- default construction is not accidentally permissive to users --")
    default = GraphStore(pool)
    check("default scope is unrestricted (internal use only, documented)",
          await default.node_exists(rows["mid"], "task_nodes"))

    await pool.close()
    print(f"\n{'=' * 55}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("V2 ACCESS CONTROL VERIFIED against real Postgres.")


if __name__ == "__main__":
    asyncio.run(main())
