import asyncio
from app.db.session import create_pool


async def main():
    pool = await create_pool()

    print("-- debate states, grouped --")
    rows = await pool.fetch(
        "SELECT state::text AS state, COUNT(*) AS n FROM debates GROUP BY state ORDER BY n DESC"
    )
    for r in rows:
        print(f"  {r['state']:20s} {r['n']}")

    print()
    print("-- how many task_nodes currently have an 'unresolved' debate --")
    print("   (this is exactly what blocks record() from creating a new trigger)")
    blocking = await pool.fetch(
        "SELECT tn.name, COUNT(*) AS n FROM task_nodes tn "
        "JOIN triggers t ON t.task_node_id = tn.id "
        "LEFT JOIN debates d ON d.trigger_id = t.id "
        "WHERE d.id IS NULL OR d.state::text NOT IN ('APPROVED', 'REJECTED') "
        "GROUP BY tn.name ORDER BY n DESC LIMIT 15"
    )
    for r in blocking:
        print(f"  {r['name']:35s} {r['n']} blocking trigger(s)")

    print()
    print("-- does the actual error-rate data still cross the threshold? --")
    real_check = await pool.fetch(
        "SELECT tn.id, tn.name, "
        "COUNT(*) AS n, "
        "AVG(CASE WHEN tr.outcome = 'failure' THEN 1.0 ELSE 0.0 END) AS error_rate "
        "FROM task_nodes tn JOIN traces tr ON tr.task_node_id = tn.id "
        "WHERE tn.t_invalid IS NULL "
        "GROUP BY tn.id, tn.name "
        "HAVING COUNT(*) >= 5 "
        "ORDER BY error_rate DESC LIMIT 10"
    )
    for r in real_check:
        print(f"  {r['name']:35s} n={r['n']:4d} error_rate={float(r['error_rate']):.2f}")

    await pool.close()


asyncio.run(main())
