"""
Zero-to-docket in one command.

Seeds the example workflow and inserts trace data deliberately shaped to
cross the default thresholds in app/api/admin.py (_DEMO_RULES) -- without
this, a fresh database has nothing that would ever trigger a debate, and
"run scan" would truthfully report zero bottlenecks forever. This exists
specifically so a first run has something real to look at.

Usage (from the backend/ directory, with a populated .env):
    python scripts/bootstrap_demo.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()  # same .env the main app reads via pydantic-settings --
                # no manual shell export needed on any platform

from app.db.session import create_pool
from app.onboarding.seed import load_spec, Onboarder


async def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not found. Either:")
        print("  - set it in backend/.env, or")
        print("  - export it in your shell first")
        sys.exit(1)

    pool = await create_pool(database_url)

    spec_path = os.path.join(os.path.dirname(__file__), "..", "examples", "example_workflow.json")
    spec = load_spec(spec_path)
    onboarder = Onboarder(pool)
    seeded = await onboarder.seed(spec, created_by="bootstrap_demo")
    print(f"Seeded '{spec.workflow_name}': {len(seeded.task_ids)} tasks, {seeded.edge_count} edges")

    # The "extract" task gets a deliberately bad error rate -- 8 failures,
    # 2 successes out of 10 crosses _DEMO_RULES' 0.15 threshold clearly.
    #
    # trace_id embeds target_id rather than being a bare "demo-fail-0"
    # literal: trace_id is the traces table's primary key, and a bare
    # literal collides across runs against a persistent database. With
    # ON CONFLICT DO NOTHING, that doesn't crash -- it silently inserts
    # nothing, meaning every run after the first creates a brand-new task
    # node with zero real trace data attached to it. Confirmed live: this
    # was why a freshly-seeded demo task stopped triggering after several
    # repeated bootstrap runs, even though the print output looked
    # identical every time.
    target_id = seeded.task_ids["extract"]
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        for i in range(8):
            await conn.execute(
                "INSERT INTO traces (trace_id, timestamp, task_node_id, actor_id, "
                "action_type, outcome) VALUES ($1,$2,$3,'demo-agent','invoke_agent','failure') "
                "ON CONFLICT (trace_id) DO NOTHING",
                f"demo-fail-{target_id}-{i}", now, target_id,
            )
        for i in range(2):
            await conn.execute(
                "INSERT INTO traces (trace_id, timestamp, task_node_id, actor_id, "
                "action_type, outcome) VALUES ($1,$2,$3,'demo-agent','invoke_agent','success') "
                "ON CONFLICT (trace_id) DO NOTHING",
                f"demo-ok-{target_id}-{i}", now, target_id,
            )
    print("Inserted 10 traces for 'Extract structured fields' — 80% error rate, "
          "well past the demo threshold of 15%")

    await pool.close()
    print("\nNext: POST /v1/admin/scan (or click 'Run scan' in the docket UI) "
          "to open a debate on this bottleneck.")


if __name__ == "__main__":
    asyncio.run(main())
