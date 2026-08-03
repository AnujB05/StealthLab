import asyncio
from app.db.session import create_pool
from app.services.access import AccessScope
from app.services.retrieval import HybridRetriever


class NoEmbedder:
    async def embed_one(self, text, input_type="query"):
        raise RuntimeError("forcing lexical-only, matching your current setup")


async def main():
    pool = await create_pool()

    print("-- every row currently named 'Extract structured fields' --")
    rows = await pool.fetch(
        "SELECT id, description, visibility::text AS vis, t_invalid, t_created "
        "FROM task_nodes WHERE name = 'Extract structured fields' "
        "ORDER BY t_created DESC"
    )
    print(f"{len(rows)} total row(s), newest first:")
    for r in rows:
        live = "LIVE" if r["t_invalid"] is None else "superseded"
        print(f"  [{live:10s}] vis={r['vis']!r} desc={r['description']!r}")

    print()
    print("-- calling the real retriever with the SAME scope the real chat")
    print("   endpoint uses (anonymous), not the unrestricted internal default --")
    retriever = HybridRetriever(pool, embedder=NoEmbedder(), scope=AccessScope.anonymous())
    result = await retriever.retrieve("What does the extraction step depend on?", top_k=6)
    print(f"nodes found: {len(result.nodes)}")
    for n in result.nodes:
        print(f"  {n.name!r} (hops={n.hops}, matched_by={n.matched_by})")

    await pool.close()


asyncio.run(main())
