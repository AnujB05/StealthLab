import asyncio
from app.db.session import create_pool
from app.debate.state_machine import DebateStateMachine


async def main():
    pool = await create_pool()

    blocking = await pool.fetch(
        "SELECT d.id AS debate_id, d.state::text AS state, t.id AS trigger_id "
        "FROM task_nodes tn "
        "JOIN triggers t ON t.task_node_id = tn.id "
        "LEFT JOIN debates d ON d.trigger_id = t.id "
        "WHERE tn.name = 'Extract structured fields' AND tn.t_invalid IS NULL "
        "AND (d.id IS NULL OR d.state::text NOT IN ('APPROVED', 'REJECTED'))"
    )

    if not blocking:
        print("Nothing blocking 'Extract structured fields' -- already clear.")
        await pool.close()
        return

    print(f"Found {len(blocking)} blocking debate(s) on the real demo task:")
    for row in blocking:
        print(f"  debate_id={row['debate_id']} state={row['state']!r}")

    machine = DebateStateMachine(pool)
    for row in blocking:
        if row["debate_id"] is None:
            # A trigger with no debate ever opened for it -- nothing to
            # transition, but it still blocks record() the same way.
            # Deleting a trigger row (not a debate, not a knowledge fact)
            # is acceptable here: it was never a real decision, just a
            # dangling artifact from isolated testing.
            await pool.execute("DELETE FROM triggers WHERE id = $1", row["trigger_id"])
            print(f"  removed dangling trigger {row['trigger_id']} (no debate was ever opened)")
        else:
            await machine.transition(
                row["debate_id"], "REJECTED",
                reason="cleanup: stale debate from earlier testing, never resolved; "
                       "cleared to allow real re-testing of this task",
                actor="cleanup_script",
            )
            print(f"  transitioned debate {row['debate_id']} to REJECTED")

    await pool.close()
    print("\nDone. 'Extract structured fields' should be eligible for a fresh trigger now.")


asyncio.run(main())
