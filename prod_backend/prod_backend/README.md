# Workflow Debate Platform

Detect a bottleneck in a company's workflows → debate the fix among
heterogeneous AI models → evaluate the fix rigorously → apply it only
after a human approves, fully auditable. V2 adds a public-facing half:
anyone can describe a problem in plain language and get back a proposed
task decomposition, quarantined behind a structural safety boundary
until a human approves it too.

Full design reasoning: `V2_PLATFORM_PLAN.md`. Current build status and
every real bug found so far: `V2_STATUS.md`. Budgeted real-model testing
protocol: `TESTING_PLAN.md`. Dense technical writeup with real code/math:
`TECHNICAL_DEEP_DIVE.md`.

## Setup

```bash
# 1. Dependencies
pip install -r requirements.txt

# 2. Database — Postgres 15+ with pgvector (Supabase works). Run in order:
psql -d your_db -f db/01_ontology.sql
psql -d your_db -f db/02_loop.sql
psql -d your_db -f db/03_access.sql
psql -d your_db -f db/04_governance.sql
psql -d your_db -f db/05_decomposition.sql

# 3. Config
cp .env.example .env
# set DATABASE_URL at minimum

# 4. Run
uvicorn app.main:app --reload      # http://localhost:8000/health
python -m pytest tests/ -q         # 168 tests, no DB or API keys needed

# 5. Demo data (an empty graph does nothing)
python scripts/bootstrap_demo.py

# 6. Frontend, from the matching frontend_v2 folder
npm install && npm run dev         # http://localhost:3000
```

**Model providers**, checked in this order in `app/debate/panel.py`:

| Setting | Cost | Use |
|---|---|---|
| `USE_LOCAL_MODELS=true` | Free | Ollama, structural smoke-testing |
| `USE_GENERAL_COMPUTE=true` | Cheap | Hosted open-weight, real reasoning quality |
| (neither set) | Paid | Anthropic + Fireworks/Kimi + OpenAI + Google, the originally-designed roster |

Full model-selection env vars are documented in `.env.example`.

## How it works

**Ontology.** Two connected graphs — `knowledge_nodes` and `task_nodes` —
bi-temporal: every row tracks `t_valid`/`t_invalid` (when a fact was true
in the world) separately from `t_created`/`t_expired` (when the system
learned it). Updates are invalidate-and-append, never in-place, so any
past graph state is exactly reconstructable. `provenance` tags every row
by origin (`company_ingested`, `company_debate`, `prior_library`,
`public_generated`), so nothing external is ever mistaken for earned
company fact.

**Debate protocol** (`app/debate/`). Modeled on the Nyaya dialectic:
*Vada* (cooperative default — propose/amend/pass, round-robin until a
round produces no movement or a cap is hit), *Nirnaya* (an independent
judge, enforced in code to share no model family with the panel).
Heterogeneity is enforced too — `assert_heterogeneous` checks model
*family*, not name, so two versions of the same base model correctly
fail the check.

**Evaluation** (`app/eval/`). Layer 1: deterministic — every citation in
an argument is checked against the real graph (`node_exists`), an
uncited claim scores 0.0, the judge fails *closed* on error. Layer 2:
real statistics — Welch's t-test (not Student's, doesn't assume equal
variance), sequential testing with an O'Brien-Fleming alpha-spending
boundary, Benjamini-Hochberg correction across metrics — validated
against `scipy`'s own reference implementations, not just "runs without
erroring". Only the weakest of three possible evidence tiers is
implemented (simulated replay, labelled everywhere as a model's opinion,
never as measurement); the other two are honestly documented as blocked
in `V2_STATUS.md`.

**Access control** (`app/services/access.py`). One function builds every
visibility predicate in the system — nothing else is permitted to write
`visibility = ...` into a query. Launches as a fully public commons;
private visibility is schema-ready but disabled until real
authentication exists (the app refuses to boot if you enable one without
the other).

**Governance** (`app/services/governance.py`). Rate limiting and an LLM
spend cap, both fail *closed* on infrastructure errors, checked and
recorded together under a Postgres advisory lock (a plain transaction is
not sufficient here — verified live: it let 10 concurrent requests
through a limit of 3 before the lock was added).

**Generative decomposition + injection defense** (`app/services/
decomposition.py`, `untrusted.py`). The one place untrusted public text
reaches an LLM directly. Four defense layers, in ascending order of what
they actually guarantee — delimiting, an instruction-hierarchy prompt,
and pattern scanning are all *mitigations*, assumed to eventually fail.
The real guarantee is structural: generated content may only create new
graph nodes and connect them to each other, never modify or attach to
anything that already exists (`GENERATIVE_OP_TYPES` in
`app/models/change.py`). Re-validated at *apply* time too, not just at
generation, so a proposal tampered with in storage between the two still
can't escalate.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/traces` | Ingest execution traces |
| POST | `/v1/admin/scan` | Detect bottlenecks, run the full debate loop |
| GET | `/v1/approvals/pending` | List scorecards awaiting a decision |
| POST | `/v1/approvals/{id}` | Approve or reject |
| GET | `/v1/graph/{node_id}` | Subgraph for visualization |
| POST | `/v1/chat` | Grounded Q&A over the knowledge graph |
| POST | `/v1/decompose` | Tab 1: problem in, proposed workflow out |
| GET | `/v1/decompose/pending` | List public proposals awaiting a decision |
| GET | `/v1/decompose/{id}` | Full detail on one proposal |
| POST | `/v1/decompose/{id}/decide` | Approve or reject a public decomposition |
| GET | `/health` | Liveness check |

## Testing

```bash
python -m pytest tests/ -q          # 168 tests, offline, no DB needed
```

Plus 7 scripts against a real (disposable) Postgres, because several
real bugs in this project were only ever findable that way:

```bash
export DATABASE_URL=postgresql://...
python integration_check.py               # bi-temporal graph + KnowledgeUpdater
python integration_check_2.py             # DebateStateMachine + TriggerDetector
python integration_check_3.py             # ingestion, Layer 1, approval endpoint
python integration_check_4.py             # real-vs-silent debate failure diagnostics
python integration_check_v2.py            # access control, incl. the private-edge leak case
python integration_check_v2_governance.py # rate limiting + the concurrency race
python integration_check_v2_decomposition.py  # applying a decomposition + 2 escalation attempts
```

## Not built

Job queue (debates run synchronously in the request handler — fine for
one company's occasional use, wrong under public traffic), real
authentication, the public Tab 2 review/reward surface, identity and
payments, Prover-Estimator (needed once rewards create a real incentive
to mislead), and the two stronger tiers of Layer 2 evidence. Each is
detailed, with why, in `V2_STATUS.md`.
