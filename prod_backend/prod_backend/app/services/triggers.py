"""
Threshold-based bottleneck detection (MVP plan, Section 5).

The rules are Phase A machinery; their numeric thresholds are Phase B
calibration and cannot be responsibly defaulted, because "a 5% error rate
is bad" is a claim about a specific workflow's real baseline, not a
universal fact. ThresholdRule therefore takes its numbers as data.

`min_samples` is not decoration. Firing a trigger on 2 executions produces
debates about noise, which wastes panel compute and -- worse -- trains
approvers to ignore the queue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

import asyncpg

Metric = Literal["error_rate", "rework_rate", "avg_cost", "avg_latency_ms"]
Direction = Literal["above", "below"]


@dataclass(frozen=True)
class ThresholdRule:
    name: str
    metric: Metric
    threshold: float
    direction: Direction = "above"
    min_samples: int = 20
    window: timedelta = timedelta(days=30)


@dataclass(frozen=True)
class TriggerHit:
    task_node_id: UUID
    rule_name: str
    metric_name: str
    observed_value: float
    threshold: float
    sample_size: int
    detail: dict


_METRIC_SQL: dict[Metric, str] = {
    "error_rate": "AVG(CASE WHEN outcome = 'failure' THEN 1.0 ELSE 0.0 END)",
    "rework_rate": "AVG(CASE WHEN outcome = 'needs_rework' THEN 1.0 ELSE 0.0 END)",
    "avg_cost": "AVG(cost)",
    "avg_latency_ms": "AVG(latency_ms)",
}


class TriggerDetector:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def scan(
        self, rules: list[ThresholdRule], now: Optional[datetime] = None
    ) -> list[TriggerHit]:
        now = now or datetime.now(timezone.utc)
        hits: list[TriggerHit] = []

        for rule in rules:
            agg = _METRIC_SQL[rule.metric]  # KeyError here = programmer error, not user input
            comparison = ">" if rule.direction == "above" else "<"
            rows = await self._pool.fetch(
                f"""
                SELECT task_node_id, {agg} AS value, COUNT(*) AS n
                FROM traces
                WHERE timestamp >= $1
                GROUP BY task_node_id
                HAVING COUNT(*) >= $2 AND {agg} {comparison} $3
                """,
                now - rule.window, rule.min_samples, rule.threshold,
            )
            for r in rows:
                if r["value"] is None:  # all-NULL cost/latency column
                    continue
                hits.append(TriggerHit(
                    task_node_id=r["task_node_id"],
                    rule_name=rule.name,
                    metric_name=rule.metric,
                    observed_value=float(r["value"]),
                    threshold=rule.threshold,
                    sample_size=int(r["n"]),
                    detail={
                        "direction": rule.direction,
                        "window_days": rule.window.days,
                        "min_samples": rule.min_samples,
                    },
                ))
        return hits

    async def record(self, hits: list[TriggerHit]) -> list[UUID]:
        """
        Persist hits, skipping task nodes that already have an unresolved
        trigger. Checked against the trigger existing at all -- not just
        against an open debate -- because nothing guarantees a debate has
        been opened for a trigger by the time the next scan runs; guarding
        on debates alone let duplicate triggers slip through in the gap
        between "recorded" and "debate opened" (found by live testing,
        not by inspection -- the gap doesn't show up unless something
        actually scans twice with a real delay in between).
        """
        ids: list[UUID] = []
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for h in hits:
                    unresolved = await conn.fetchrow(
                        "SELECT 1 FROM triggers t "
                        "LEFT JOIN debates d ON d.trigger_id = t.id "
                        "WHERE t.task_node_id = $1 "
                        "AND (d.id IS NULL OR d.state NOT IN ('APPROVED', 'REJECTED'))",
                        h.task_node_id,
                    )
                    if unresolved:
                        continue
                    row = await conn.fetchrow(
                        "INSERT INTO triggers (task_node_id, rule_name, metric_name, "
                        "observed_value, threshold, sample_size, detail) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
                        h.task_node_id, h.rule_name, h.metric_name,
                        h.observed_value, h.threshold, h.sample_size, h.detail,
                    )
                    ids.append(row["id"])
        return ids
