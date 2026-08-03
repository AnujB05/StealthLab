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
        #
        # trace_id embeds trigger_task_id rather than being a bare "fail-0"
        # style literal: trace_id is the traces table's primary key, and a
        # bare literal collides across runs against a persistent (not
        # dropped-and-recreated) database, crashing with a unique
        # violation on the second run. ON CONFLICT DO NOTHING alone would
        # avoid the crash but silently skip every insert, leaving this
        # run's freshly-seeded task with zero trace rows and nothing for
        # scan() to find. Embedding the task id keeps every run's ids
        # genuinely unique, so each run's task gets its own real data.
        for i in range(15):
            await conn.execute(
                "INSERT INTO traces (trace_id, timestamp, task_node_id, action_type, outcome) "
                "VALUES ($1,$2,$3,'invoke_agent','failure') "
                "ON CONFLICT (trace_id) DO NOTHING",
                f"fail-{trigger_task_id}-{i}", now, trigger_task_id,
            )
        for i in range(5):
            await conn.execute(
                "INSERT INTO traces (trace_id, timestamp, task_node_id, action_type, outcome) "
                "VALUES ($1,$2,$3,'invoke_agent','success') "
                "ON CONFLICT (trace_id) DO NOTHING",
                f"ok-{trigger_task_id}-{i}", now, trigger_task_id,
            )

    detector = TriggerDetector(pool)
    rule = ThresholdRule(name="high_error", metric="error_rate", threshold=0.10, min_samples=10)
    hits = await detector.scan([rule], now=now + timedelta(seconds=5))
    # Checking membership, not exact count: this database is not isolated
    # per run (no cleanup step, deliberately, since a scratch database
    # isn't worth it for this stage -- see the project's testing plan).
    # A previous run's leftover data can independently satisfy the same
    # threshold, and scan() correctly finds every real bottleneck, not
    # just this run's. Asserting "my task is among the hits" is what the
    # system actually promises; asserting "my task is the only hit"
    # assumes an isolation guarantee the shared database doesn't provide,
    # and would flag a false failure exactly like this one.
    check("scan finds the bottleneck",
          any(h.task_node_id == trigger_task_id for h in hits),
          f"trigger_task_id not among {len(hits)} hit(s)")
    this_run_hit = next((h for h in hits if h.task_node_id == trigger_task_id), None)
    if this_run_hit:
        check("observed error rate is computed correctly",
              abs(this_run_hit.observed_value - 0.75) < 0.01,
              f"expected ~0.75, got {this_run_hit.observed_value}")

    below_rule = ThresholdRule(name="too_strict", metric="error_rate", threshold=0.99, min_samples=10)
    no_hits = await detector.scan([below_rule], now=now + timedelta(seconds=5))
    check("a threshold nothing crosses produces no hits", len(no_hits) == 0)

    sparse_rule = ThresholdRule(name="needs_more_data", metric="error_rate",
                                threshold=0.10, min_samples=1000)
    sparse_hits = await detector.scan([sparse_rule], now=now + timedelta(seconds=5))
    check("min_samples gate suppresses low-volume noise", len(sparse_hits) == 0)

    recorded_ids = await detector.record(hits)
    recorded_rows = await pool.fetch(
        "SELECT task_node_id FROM triggers WHERE id = ANY($1::uuid[])", recorded_ids
    )
    recorded_task_ids = {r["task_node_id"] for r in recorded_rows}
    check("record() persists a trigger row for this run's bottleneck",
          trigger_task_id in recorded_task_ids,
          f"trigger_task_id not among the {len(recorded_ids)} recorded trigger(s)")

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
