"""
Ingestion endpoint tests (MVP plan, Section 6), against a mocked pool.

The requirement being tested is specific: one malformed record must not
fail the batch. A mock pool is sufficient here because the thing under
test is the per-record validation and error-collection logic in
app/api/ingest.py, not Postgres itself.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.api.ingest import ingest_traces

VALID_TASK = str(uuid4())


def _mock_pool(execute_results=None, side_effects=None):
    """
    A pool whose acquire() context manager yields a connection whose
    execute() returns canned results (or raises, for the FK-violation
    case) in sequence.
    """
    conn = MagicMock()
    if side_effects is not None:
        conn.execute = AsyncMock(side_effect=side_effects)
    else:
        conn.execute = AsyncMock(side_effect=execute_results or [])

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


def test_malformed_record_does_not_fail_the_batch():
    pool, conn = _mock_pool(execute_results=["INSERT 0 1", "INSERT 0 1"])
    payload = {
        "records": [
            {"trace_id": "t1", "timestamp": "2026-07-01T00:00:00Z",
             "task_node_id": VALID_TASK, "outcome": "success"},
            {"trace_id": "t2", "outcome": "not_a_real_outcome"},  # malformed
            {"trace_id": "t3", "timestamp": "2026-07-01T00:00:01Z",
             "task_node_id": VALID_TASK, "outcome": "failure"},
        ]
    }
    result = asyncio.run(ingest_traces(payload, pool=pool))
    assert result.accepted == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].index == 1
    assert result.rejected[0].trace_id == "t2"
    assert "outcome" in result.rejected[0].error or "timestamp" in result.rejected[0].error


def test_missing_required_field_is_rejected_not_crashed():
    pool, conn = _mock_pool(execute_results=[])
    payload = {"records": [{"outcome": "success"}]}  # no trace_id, timestamp, task_node_id
    result = asyncio.run(ingest_traces(payload, pool=pool))
    assert result.accepted == 0
    assert len(result.rejected) == 1


def test_duplicate_trace_id_is_counted_not_rejected():
    """ON CONFLICT DO NOTHING means a retry-safe re-send, not an error."""
    pool, conn = _mock_pool(execute_results=["INSERT 0 0"])  # 0 rows affected = conflict
    payload = {"records": [
        {"trace_id": "dupe", "timestamp": "2026-07-01T00:00:00Z",
         "task_node_id": VALID_TASK, "outcome": "success"},
    ]}
    result = asyncio.run(ingest_traces(payload, pool=pool))
    assert result.accepted == 0
    assert result.duplicates == 1
    assert result.rejected == []


def test_fk_violation_is_a_per_record_rejection_not_a_crash():
    """A trace referencing an unseeded task_node_id must reject that record only."""
    pool, conn = _mock_pool(side_effects=[
        RuntimeError('insert or update on table "traces" violates foreign key constraint'),
        "INSERT 0 1",
    ])
    payload = {"records": [
        {"trace_id": "orphan", "timestamp": "2026-07-01T00:00:00Z",
         "task_node_id": str(uuid4()), "outcome": "success"},
        {"trace_id": "fine", "timestamp": "2026-07-01T00:00:01Z",
         "task_node_id": VALID_TASK, "outcome": "success"},
    ]}
    result = asyncio.run(ingest_traces(payload, pool=pool))
    assert result.accepted == 1
    assert len(result.rejected) == 1
    assert result.rejected[0].trace_id == "orphan"


def test_records_must_be_a_list():
    pool, conn = _mock_pool()
    result = asyncio.run(ingest_traces({"records": "not-a-list"}, pool=pool))
    assert result.accepted == 0
    assert "array" in result.rejected[0].error
