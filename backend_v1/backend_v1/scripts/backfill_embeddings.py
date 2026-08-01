"""
Backfill embeddings for nodes seeded before embedding generation existed.

Needed because every node currently in any database from V0 was created
without one -- onboarding only started embedding as of V1 item #3.
Without this, existing graphs would be permanently invisible to semantic
search with no error to indicate why.

Usage (from backend/, with a populated .env):
    python scripts/backfill_embeddings.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from app.db.session import create_pool
from app.services.embeddings import Embedder, node_text, to_pgvector

BATCH = 64


async def backfill_table(pool, embedder, table: str) -> int:
    has_description = table == "task_nodes"
    rows = await pool.fetch(
        f"SELECT id, name, {'description' if has_description else 'NULL AS description'} "
        f"FROM {table} WHERE embedding IS NULL AND t_invalid IS NULL"
    )
    if not rows:
        print(f"  {table}: nothing to backfill")
        return 0

    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        texts = [node_text(r["name"], r["description"]) for r in chunk]
        vectors = await embedder.embed(texts, input_type="document")
        async with pool.acquire() as conn:
            for row, vector in zip(chunk, vectors):
                await conn.execute(
                    f"UPDATE {table} SET embedding = $2::vector WHERE id = $1",
                    row["id"], to_pgvector(vector),
                )
        done += len(chunk)
        print(f"  {table}: {done}/{len(rows)}")
    return done


async def main():
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL not found — set it in backend/.env")
        sys.exit(1)

    pool = await create_pool(os.environ["DATABASE_URL"])
    embedder = Embedder()
    print(f"Backfilling with {embedder.model} (dimension {embedder.dimension})")

    total = 0
    for table in ("task_nodes", "knowledge_nodes"):
        total += await backfill_table(pool, embedder, table)

    await pool.close()
    print(f"\nDone — {total} node(s) embedded.")
    if total:
        print("Semantic search over these nodes is now active.")


if __name__ == "__main__":
    asyncio.run(main())
