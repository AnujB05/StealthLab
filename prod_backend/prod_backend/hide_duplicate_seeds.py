import asyncio
from app.db.session import create_pool


async def main():
    pool = await create_pool()

    for table in ("task_nodes", "knowledge_nodes"):
        print(f"-- {table} --")
        dupes = await pool.fetch(
            f"SELECT name, array_agg(id ORDER BY t_created ASC) AS ids "
            f"FROM {table} "
            f"WHERE t_invalid IS NULL AND provenance = 'company_ingested' "
            f"AND visibility = 'public' "
            f"GROUP BY name HAVING COUNT(*) > 1"
        )
        for row in dupes:
            keep, hide = row["ids"][0], row["ids"][1:]
            await pool.execute(
                f"UPDATE {table} SET visibility = 'private', owner_id = 'demo_cleanup' "
                f"WHERE id = ANY($1::uuid[])",
                hide,
            )
            print(f"  {row['name']!r}: kept {keep}, hid {len(hide)} duplicate(s)")

    await pool.close()
    print("\nDone. Nothing deleted -- duplicates are marked private, not removed.")
    print("Re-run check_retrieval_2.py to confirm the node count drops to real duplicates only.")


asyncio.run(main())
