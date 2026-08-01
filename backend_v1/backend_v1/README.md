# Workflow Debate Platform — Backend (Phase A)

Phase A of the MVP plan: the domain-agnostic skeleton. Everything here is
generic machinery, built and tested with no domain content anywhere in it.
That is deliberate — it's what makes the generalisability claim checkable
rather than aspirational. Phase B (first onboarding, threshold
calibration, end-to-end validation) needs a confirmed real workflow.

## Layout

```
db/01_ontology.sql     KnowledgeNode / TaskNode / edges / episodes / traces
db/02_loop.sql         triggers / debates / turns / candidates / scorecards / approvals
app/models/            Pydantic mirrors of both schemas + ChangeSet
app/db/                connection pool (with the JSONB codec) + GraphStore
app/debate/            panel agents, state machine, Vada engine, prompts
app/eval/              Layer 1 evaluator (Nirnaya) + judge rubric
app/services/          trigger detection, knowledge update, loop orchestrator
app/export/            markdown diff rendering
app/onboarding/        reusable workflow seeding
app/api/               FastAPI routes (ingest, approval, admin)
scripts/               bootstrap_demo.py — seeds a demo workflow + trace data
tests/                 offline logic tests (no DB required)
TEST_REPORT.md         full record of everything verified live in development
```

## Setup

```bash
# 1. Database — any Postgres 15+ with pgvector (Supabase, or local)
createdb workflow_db
psql -d workflow_db -f db/01_ontology.sql
psql -d workflow_db -f db/02_loop.sql

# 2. Backend
pip install -r requirements.txt
cp .env.example .env   # fill in DATABASE_URL at minimum
uvicorn app.main:app --reload
```

`GET /health` should return `{"status":"ok"}` at this point. That's the
whole app working — but the docket will be empty, correctly, since
nothing has created a trigger yet.

**To see it do something**, with `DATABASE_URL` exported in your shell:
```bash
python scripts/bootstrap_demo.py
```
Seeds the example workflow and inserts trace data shaped to actually
cross the default bottleneck thresholds — without this there's nothing
for a scan to find. Then either `curl -X POST localhost:8000/v1/admin/scan`
or click "Run scan" in the frontend.

**Four API keys are needed for that last step specifically** — one per
debate-panel seat plus an independent judge: `ANTHROPIC_API_KEY`,
`FIREWORKS_API_KEY` (Kimi K3), `OPENAI_API_KEY`, `GOOGLE_API_KEY`
(Gemini — the fourth exists because the judge must be a model family
none of the three panelists use). Everything else in this backend runs
without any of them.

Run the tests with `python -m pytest tests/ -q` — no database, no keys.

## Notes for review

**Verification status, by item:**

| Item | Coverage |
|---|---|
| 1. Ontology | Live: schema, `task_nodes` JSONB fields |
| 2. Ingestion | Live: FK rejection, `ON CONFLICT` dedup, batch resilience to malformed records |
| 3. Vāda debate | Offline only: engine logic (convergence, round-cap, agent-failure resilience), SDK wrapper parsing against mocked responses, `_extract_json` against realistic model-output quirks. **Real API calls to Anthropic/Fireworks/OpenAI have never been made** — no credentials in this environment. This is the one gap that cannot be closed from here. |
| 4. Layer 1 eval | Live: groundedness scoring against a real `GraphStore` with real and nonexistent citations, judge-independence enforcement, fail-closed behavior |
| 5. Approval + update | Live: `_supersede_task` edge-rewiring, bi-temporal point-in-time queries, and the full `decide()` endpoint end to end (approve and reject paths) |
| 6. Delivery formatter | Offline: 10 tests, including that Layer 2 absence and advisory-only status are always stated explicitly, never silently omitted |
| 7. Onboarding | Live: real seeding, spec validation catching bad wiring |
| State machine | Live: real transaction, real row lock, real illegal-transition rejection |
| Trigger detection | Live: real `GROUP BY`/`HAVING` aggregation, dedup logic |

**Two real bugs found and fixed this session, both invisible without live testing:**

1. Every JSONB write pre-serialized with `json.dumps()` and cast `::jsonb`,
   which silently corrupted the connection's JSONB decoding for
   subsequent reads once the type codec was registered. Affected every
   JSONB column in the system. Fixed by passing native Python objects and
   letting the codec encode.

2. `TriggerDetector.record()`'s duplicate-suppression checked for an
   *open debate*, but nothing in `record()` opens one — that happens in a
   separate call. In the gap between a trigger being recorded and a
   debate actually being opened for it, a second scan found nothing to
   join against and inserted a duplicate trigger for the same
   bottleneck. Fixed by checking for an unresolved trigger, not just an
   open debate.

Both were caught by writing tests that exercised real, multi-step
sequences against actual state — not by inspection, and not by mocks.

**Genuinely not yet exercised:** anything requiring real LLM responses
(item 3's actual API behavior), the vector index at scale, or concurrent
writers against `DebateStateMachine`'s row lock.

**Fail-closed choices worth knowing about:**
- A judge outage marks a candidate failed, never passed (`Layer1Evaluator`).
- An uncited proposal scores 0.0 groundedness and does not pass.
- A change set that fails to apply rolls back and leaves the debate in
  `PENDING_APPROVAL` rather than recording an approval that didn't happen.

## Deployment

Railway or Render, per the plan's Section 12. Both platforms need one
setting the monorepo layout makes non-default: point them at the
`backend/` subdirectory, not the repo root.

- **Railway:** New service → Deploy from GitHub repo → in the service's
  Settings, set **Root Directory** to `backend`. It picks up the
  `Procfile` automatically. Add the environment variables from
  `.env.example` under Variables.
- **Render:** New Web Service → connect the repo → set **Root Directory**
  to `backend` in the create form. Build command
  `pip install -r requirements.txt`, start command
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT` (same as the
  `Procfile`, Render just wants it typed into the form directly).

Either way, set `DATABASE_URL` to a real hosted Postgres with pgvector
enabled (Supabase's is the easiest to get pgvector on without extra
setup) and `FRONTEND_ORIGIN` to wherever the frontend ends up deployed —
CORS will reject the frontend's requests until that matches exactly.

## Deliberately absent (Phase A scope)

Layer 2 empirical eval, Jalpa adversarial escalation, Prover-Estimator,
human panelists, real auth/RBAC, multi-tenancy enforcement, cold-start
apparatus beyond PERT fields. Each has a trigger condition in Section 12
and Section 15.3 of the plan.
