import asyncio
from app.db.session import create_pool


async def main():
    pool = await create_pool()

    print("-- every version of 'Extract structured fields', oldest first --")
    print("   (t_invalid IS NULL means this version is the current live one)")
    rows = await pool.fetch(
        "SELECT id, description, t_valid, t_invalid, created_by "
        "FROM task_nodes WHERE name = 'Extract structured fields' "
        "ORDER BY t_created ASC"
    )
    for r in rows:
        status = "LIVE NOW" if r["t_invalid"] is None else f"superseded at {r['t_invalid']}"
        print(f"  id={r['id']}")
        print(f"    description: {r['description']!r}")
        print(f"    status: {status}")
        print(f"    created_by: {r['created_by']!r}")
        print()

    print("-- SUPERSEDES edges (the link between old and new versions) --")
    edges = await pool.fetch(
        "SELECT source_id, target_id, properties FROM edges WHERE edge_type = 'SUPERSEDES'"
    )
    for e in edges:
        print(f"  {e['source_id']} -> {e['target_id']}  ({e['properties']})")

    await pool.close()


asyncio.run(main())
