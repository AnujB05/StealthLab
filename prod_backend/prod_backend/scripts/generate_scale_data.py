"""
Synthetic data generation for load/scale testing.

Bulk-inserts via `copy_records_to_table` rather than one INSERT per row
-- at the volumes this is meant to test (thousands to tens of thousands
of rows), row-at-a-time inserts would dominate the benchmark's own
runtime and make the results measure Python/network overhead instead of
the database operations actually under test.

Generates a branching workflow shape (not a single chain), since that's
what makes `GraphStore.traverse_from`'s recursive CTE actually work for
its answer -- a chain gives every traversal the same trivial shape
regardless of depth, and wouldn't expose whether fan-out is the real
cost driver.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg


@dataclass
class GeneratedScale:
    task_node_ids: list[UUID]
    knowledge_node_ids: list[UUID]
    edge_count: int
    trace_count: int


async def generate(
    pool: asyncpg.Pool,
    n_tasks: int,
    fanout: int = 3,
    traces_per_task: int = 20,
    seed: int = 42,
) -> GeneratedScale:
    """
    Builds a branching task graph: task 0 is the root, each subsequent
    task attaches to a random earlier task (bounded by `fanout` choices),
    producing realistic branching rather than a flat chain or a star.

    Every task also gets `traces_per_task` trace rows, error rate varying
    randomly per task so TriggerDetector.scan()'s aggregate query has
    real, non-uniform data to group over, not a single repeated value
    the query planner could special-case.
    """
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)

    task_ids = [uuid4() for _ in range(n_tasks)]
    task_rows = [
        (tid, f"scale-task-{i}", f"synthetic load-test task {i}", "company_ingested")
        for i, tid in enumerate(task_ids)
    ]

    knowledge_ids = [uuid4() for _ in range(max(1, n_tasks // 10))]
    knowledge_rows = [
        (kid, "policy", f"scale-knowledge-{i}", "company_ingested")
        for i, kid in enumerate(knowledge_ids)
    ]

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.copy_records_to_table(
                "task_nodes", records=task_rows,
                columns=["id", "name", "description", "provenance"],
            )
            await conn.copy_records_to_table(
                "knowledge_nodes", records=knowledge_rows,
                columns=["id", "node_type", "name", "provenance"],
            )

            edge_rows = []
            for i in range(1, n_tasks):
                choices = min(fanout, i)
                for _ in range(rng.randint(1, choices)):
                    source = task_ids[rng.randint(0, i - 1)]
                    edge_rows.append((
                        uuid4(), "PRODUCES", source, "task_nodes",
                        task_ids[i], "task_nodes", now, now,
                    ))
                if knowledge_ids and rng.random() < 0.3:
                    edge_rows.append((
                        uuid4(), "REQUIRES", task_ids[i], "task_nodes",
                        knowledge_ids[rng.randrange(len(knowledge_ids))], "knowledge_nodes",
                        now, now,
                    ))
            await conn.copy_records_to_table(
                "edges",
                records=edge_rows,
                columns=["id", "edge_type", "source_id", "source_table",
                         "target_id", "target_table", "t_valid", "t_created"],
            )

            trace_rows = []
            for i, tid in enumerate(task_ids):
                error_rate = rng.random()
                for j in range(traces_per_task):
                    outcome = "failure" if rng.random() < error_rate else "success"
                    trace_rows.append((
                        f"scale-{tid}-{j}",
                        now - timedelta(minutes=rng.randint(0, 60 * 24)),
                        tid, "invoke_agent", outcome,
                    ))
            await conn.copy_records_to_table(
                "traces", records=trace_rows,
                columns=["trace_id", "timestamp", "task_node_id", "action_type", "outcome"],
            )

    return GeneratedScale(
        task_node_ids=task_ids, knowledge_node_ids=knowledge_ids,
        edge_count=len(edge_rows), trace_count=len(trace_rows),
    )
