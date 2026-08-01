"""
Second pass: DebateStateMachine and TriggerDetector against real Postgres.

Neither has ANY test coverage as of the last review -- state_machine's
pure transition-legality functions are offline-tested, but its actual
DB-touching methods (the FOR UPDATE lock, the transaction, the
debate_events insert) never ran against a real connection. TriggerDetector
has no tests of any kind, offline or live.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from app.db.session import create_pool
from app.debate.state_machine import DebateStateMachine, IllegalTransition
from app.onboarding.seed import Onboarder, TaskSpec, WorkflowSpec
from app.services.triggers import ThresholdRule, TriggerDetector


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    onboarder = Onboarder(pool)
    seeded = await onboarder.seed(WorkflowSpec(
        workflow_name="state_and_trigger_check",
        tasks=[
            TaskSpec(key="t1", name="Task for state machine section"),
            TaskSpec(key="t2", name="Task for trigger detection section"),
        ],
    ))
    task_id = seeded.task_ids["t1"]
    trigger_task_id = seeded.task_ids["t2"]  # kept separate: t1 ends the test IN_DEBATE,

    # --- DebateStateMachine: real transaction, real row lock, real insert ---
    print("-- DebateStateMachine (previously zero live coverage) --")
    trigger_row = await pool.fetchrow(
        "INSERT INTO triggers (task_node_id, rule_name, metric_name, observed_value, "
        "threshold, sample_size, detail) VALUES ($1,'r','error_rate',0.5,0.1,20,$2) "
        "RETURNING id", task_id, {},
    )
    trigger_id = trigger_row["id"]
    debate_row = await pool.fetchrow(
        "INSERT INTO debates (trigger_id) VALUES ($1) RETURNING id", trigger_id
    )
    debate_id = debate_row["id"]

    machine = DebateStateMachine(pool)
    state = await machine.current_state(debate_id)
    check("current_state reads the real row", state == "OPEN", f"got {state!r}")

    new_state = await machine.transition(debate_id, "IN_DEBATE", reason="test", actor="checker")
    check("transition writes the new state", new_state == "IN_DEBATE")

    row = await pool.fetchrow("SELECT state::text AS s FROM debates WHERE id=$1", debate_id)
    check("the write actually persisted", row["s"] == "IN_DEBATE")

    history = await machine.history(debate_id)
    check("debate_events recorded the transition",
          len(history) == 1 and history[0]["to_state"] == "IN_DEBATE",
          f"got {history}")

    try:
        await machine.transition(debate_id, "APPROVED", reason="skip ahead")
        check("illegal transition (IN_DEBATE -> APPROVED) is rejected", False)
    except IllegalTransition:
        check("illegal transition (IN_DEBATE -> APPROVED) is rejected", True)

    post_row = await pool.fetchrow("SELECT state::text AS s FROM debates WHERE id=$1", debate_id)
    check("a rejected transition did not mutate the row",
          post_row["s"] == "IN_DEBATE", f"got {post_row['s']!r}")

    # --- TriggerDetector: real GROUP BY / HAVING aggregation ---
    print("\n-- TriggerDetector (previously zero coverage, offline or live) --")
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        # 15 failures, 5 successes -> 75% error rate, well above a 10% threshold
        for i in range(15):
            await conn.execute(
                "INSERT INTO traces (trace_id, timestamp, task_node_id, action_type, outcome) "
                "VALUES ($1,$2,$3,'invoke_agent','failure')",
                f"fail-{i}", now, trigger_task_id,
            )
        for i in range(5):
            await conn.execute(
                "INSERT INTO traces (trace_id, timestamp, task_node_id, action_type, outcome) "
                "VALUES ($1,$2,$3,'invoke_agent','success')",
                f"ok-{i}", now, trigger_task_id,
            )

    detector = TriggerDetector(pool)
    rule = ThresholdRule(name="high_error", metric="error_rate", threshold=0.10, min_samples=10)
    hits = await detector.scan([rule], now=now + timedelta(seconds=5))
    check("scan finds the bottleneck", len(hits) == 1, f"got {len(hits)} hits")
    if hits:
        check("observed error rate is computed correctly",
              abs(hits[0].observed_value - 0.75) < 0.01,
              f"expected ~0.75, got {hits[0].observed_value}")

    below_rule = ThresholdRule(name="too_strict", metric="error_rate", threshold=0.99, min_samples=10)
    no_hits = await detector.scan([below_rule], now=now + timedelta(seconds=5))
    check("a threshold nothing crosses produces no hits", len(no_hits) == 0)

    sparse_rule = ThresholdRule(name="needs_more_data", metric="error_rate",
                                threshold=0.10, min_samples=1000)
    sparse_hits = await detector.scan([sparse_rule], now=now + timedelta(seconds=5))
    check("min_samples gate suppresses low-volume noise", len(sparse_hits) == 0)

    recorded_ids = await detector.record(hits)
    check("record() persists a trigger row", len(recorded_ids) == 1)

    dup_hits = await detector.scan([rule], now=now + timedelta(seconds=5))
    dup_recorded = await detector.record(dup_hits)
    check("a second scan does not duplicate an open debate's trigger",
          len(dup_recorded) == 0, f"got {len(dup_recorded)} (expected 0 -- debate already open)")

    await pool.close()

    print(f"\n{'='*50}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("ALL CHECKS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
