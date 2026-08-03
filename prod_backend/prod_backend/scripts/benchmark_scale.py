"""
Load/scale benchmark for GraphStore.traverse_from, TriggerDetector.scan,
and HybridRetriever's lexical path -- flagged as "NOT LOAD TESTED" since
the very first version of this project and never actually measured
until now.

Absolute numbers here are illustrative of THIS machine, not necessarily
Supabase's hosted Postgres -- what matters more is the SHAPE of how
latency grows with scale (linear, and therefore fine, vs superlinear,
which would mean the recursive CTE or the aggregate scan is the thing to
actually fix before real production traffic).

Run:
    DATABASE_URL=postgresql://... python scripts/benchmark_scale.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.session import create_pool
from app.db.graph_store import GraphStore
from app.services.access import AccessScope
from app.services.retrieval import HybridRetriever
from app.services.triggers import TriggerDetector, ThresholdRule
from scripts.generate_scale_data import generate

TIERS = [100, 1_000, 10_000]
REPEATS = 5  # per operation, report the median -- a single measurement
             # is too noisy to trust, especially at the small tiers


class NoEmbedder:
    async def embed_one(self, text, input_type="query"):
        raise RuntimeError("benchmarking lexical-only path deliberately")


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


async def time_it(fn, repeats=REPEATS) -> tuple[float, object]:
    times = []
    result = None
    for _ in range(repeats):
        t0 = time.monotonic()
        result = await fn()
        times.append((time.monotonic() - t0) * 1000)
    return median(times), result


async def run_tier(pool, n_tasks: int) -> dict:
    scale = await generate(pool, n_tasks=n_tasks, fanout=3, traces_per_task=20)
    scope = AccessScope.anonymous()
    graph = GraphStore(pool, scope=scope)
    retriever = HybridRetriever(pool, embedder=NoEmbedder(), scope=scope)
    detector = TriggerDetector(pool)

    root = scale.task_node_ids[0]
    leaf = scale.task_node_ids[-1]

    results = {}

    for depth in (1, 2, 3):
        t, edges = await time_it(
            lambda d=depth: graph.traverse_from([root], "task_nodes", max_depth=d)
        )
        results[f"traverse_from(depth={depth})"] = (t, f"{len(edges)} edges")

    t, edges = await time_it(
        lambda: graph.get_neighbors(leaf, "task_nodes")
    )
    results["get_neighbors(leaf)"] = (t, f"{len(edges)} edges")

    t, hits = await time_it(
        lambda: retriever._lexical_search("synthetic load-test workflow step", limit=6)
    )
    results["lexical_search"] = (t, f"{len(hits)} hits")

    t, triggers = await time_it(
        lambda: detector.scan([ThresholdRule(name="x", metric="error_rate",
                                              threshold=0.3, min_samples=5)])
    )
    results["trigger_scan(full table)"] = (t, f"{len(triggers)} hits")

    return results


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])

    print(f"{'operation':32s} " + "".join(f"{n:>12,} tasks" for n in TIERS))
    print("-" * (32 + 18 * len(TIERS)))

    all_results = {}
    for n in TIERS:
        await pool.execute("TRUNCATE traces, edges, task_nodes, knowledge_nodes CASCADE")
        results = await run_tier(pool, n)
        for op, (t, detail) in results.items():
            all_results.setdefault(op, []).append((t, detail))
        print(f"  ...tier {n:,} done")

    print()
    print(f"{'operation':32s} " + "".join(f"{n:>12,} tasks" for n in TIERS))
    print("-" * (32 + 18 * len(TIERS)))
    for op, series in all_results.items():
        row = "".join(f"{t:>10.1f}ms  " for t, _ in series)
        print(f"{op:32s} {row}")

    print()
    print("Detail at largest tier:")
    for op, series in all_results.items():
        print(f"  {op}: {series[-1][1]}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
