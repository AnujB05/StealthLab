# V2 — Build Status

## What this is

A public platform with two surfaces, built on the engine from V0/V1:

- **Tab 1 (Workbench)** — anyone describes a problem in natural
  language; the system decomposes it into a task workflow, grounded in
  what's already in the shared library. **Built.**
- **Tab 2** — a public page of solved problems where people propose
  better solutions and earn rewards, adjudicated by the same debate and
  eval mechanism. **Not built.** The engine exists; the public surface,
  identity, and rewards do not.

V0/V1 were a *private, per-company* governance tool: ingest a company's
workflow traces, detect bottlenecks, run a debate between AI panelists,
have a human approve the fix, write it back to a bi-temporal knowledge
graph. V2 keeps that engine and inverts the trust model — public input,
shared knowledge, untrusted submitters. Most of the V2-specific work is
that inversion, not new features.

## Orientation for a reviewer

| Doc | What it covers |
|---|---|
| `V2_STATUS.md` (this) | What's actually built, what isn't, what's untested |
| `V2_PLATFORM_PLAN.md` | The full V2 design and its reasoning — read this for *why* |
| `README.md` | The inherited V0/V1 architecture |
| `TEST_REPORT.md` | V0-era verification record, including two real bugs found live |

The frontend is a **separate repo/folder: `frontend_v2`** (Next.js).
Tab 1 lives at `app/workbench/page.tsx`.

**The three things most worth your scrutiny**, since they're where
judgement calls were made rather than obvious implementations:

1. **`app/services/access.py`** — every visibility predicate in the
   system is built here and nowhere else. That constraint is the design:
   V0's `tenant_id` existed on every table and *no query ever filtered
   by it*, so isolation was decorative. Enabling private mode should
   remain a change to one function.
2. **`GENERATIVE_OP_TYPES` in `app/models/change.py`** — the capability
   boundary that makes a prompt-injected model harmless. Prompting and
   pattern-scanning are mitigations; this is the only guarantee.
3. **`app/eval/statistics.py`** — validated against scipy and worked
   textbook examples, not just "it runs". If you change it, keep the
   reference-value tests.

---

## Done: access control foundation (Tier 1, partial)

**Decision:** V2 launches as a fully shared commons — everything public
— with per-node private visibility available later without a migration.

**What was built:**
- `db/03_access.sql` — `visibility` (`public`/`private`) and `owner_id`
  on `knowledge_nodes`, `task_nodes`, `edges`, `debates`. Partial
  indexes on the common public path.
- `app/services/access.py` — `AccessScope` and `visibility_predicate()`.
  **The single place any visibility SQL is written.**
- `app/api/deps.py` — per-request scope resolution, plus a startup guard.
- `GraphStore`, `HybridRetriever`, `ChatService`, and the chat/graph
  endpoints all scoped.

**The design decision that matters:** the predicate is in every query
path *now*, currently permissive. V0's `tenant_id` is the cautionary
case — the column existed on every table from the first schema and no
query ever filtered by it, so isolation was decorative and enabling it
later would have meant auditing every query. Here, enabling private mode
is a change to one function.

**Verified against real Postgres** (`integration_check_v2.py`, 11
checks). The one that matters most: a public node reachable only
*through* a private edge is not exposed. Filtering traversal output
alone would have leaked it — hiding the edge while revealing everything
beyond it. The predicate is applied inside the recursive CTE, not just
to its result.

**Deliberate temporary shortcut, not an oversight:** viewer identity
comes from an unverified `X-Viewer-Id` header. Safe *only* because
everything is currently public, so a forged identity grants nothing that
isn't already world-readable. `require_trustworthy_identity()` runs at
startup and refuses to boot if `private_visibility_enabled` is turned on
while `real_auth_enabled` is off.

---

## Done: rate limiting and cost governance (Tier 1, partial)

**What was built:**
- `db/04_governance.sql` — `rate_limit_events` and `llm_spend`.
- `app/services/governance.py` — `RateLimiter`, `CostGovernor`, cost
  estimation.
- `app/api/deps.py` — `enforce_limits` dependency, wired into
  `/v1/chat`, `/v1/admin/scan`, and `/v1/traces`.

**Two protections, deliberately separate because they fail differently.**
Rate limiting bounds request *frequency*; cost governance bounds actual
*spend*. One `/v1/admin/scan` fans out to a debate panel, a judge, and
Layer 2 simulations — dozens of model calls from one request — so a
frequency cap alone says nothing useful about the resulting bill.

**Fail-closed on infrastructure errors**, in both directions
deliberately: the *checks* deny when the store is unreachable (a limiter
that allows everything while appearing to protect is worse than none),
while *recording* spend fails open (the model call already happened and
was already paid for; raising would discard completed work over a
bookkeeping error).

**A real bug was found and fixed by the concurrency test.** The first
implementation wrapped check-then-insert in a transaction, which is
*not* sufficient: under Postgres's default READ COMMITTED isolation, ten
concurrent requests each see a snapshot without the others' uncommitted
inserts, so all ten read a count of zero and all ten proceed. Verified
failing (10 allowed against a limit of 3), then fixed with a per-key
`pg_advisory_xact_lock`, which serialises one key without blocking
unrelated ones.

**Known limitation, by design:** budget is checked *before* a workload
and recorded *after* each call, so a single expensive workload can
overshoot the cap. Closing that gap would require pre-estimating a
variable-round debate's total cost, which isn't reliably knowable. The
cap bounds sustained spend, not any single request.

**Also known:** the anonymous rate-limit key falls back to the direct
socket IP. Behind a proxy or load balancer that becomes the proxy's IP,
collapsing all anonymous users into one bucket. Real deployment needs a
*trusted* `X-Forwarded-For` chain — deliberately not trusted here,
because an untrusted one is trivially spoofed to bypass limits entirely.

---

## Done: generative decomposition + prompt-injection defence (Tier 3)

Tab 1's core capability, and the highest-*risk* item in the V2 plan.

**The structural gap it closed:** `ChangeSet` had no way to create nodes.
V0/V1 were correct not to need it — the graph was authored by a human
offline, and the debate panel could only refine what already existed.
Tab 1 must invent structure from nothing, on input the system has never
seen, from someone it has no reason to trust.

**What was built:**
- `CreateTaskNodeOp` / `CreateKnowledgeNodeOp`, and local `ref` wiring on
  `CreateEdgeOp` (generated nodes have no ids until the set is applied).
- `app/services/untrusted.py` — sanitising, fencing, pattern scanning.
- `app/services/decomposition.py` — generation plus adversarial critique.
- `app/api/decompose.py` — `POST /v1/decompose`.

**Four defence layers, in ascending order of what they can guarantee.**
The ordering is the design, not a list:

1. *Delimiting* — untrusted text fenced and labelled as data. Defeated by
   an attacker who guesses the fence.
2. *Instruction hierarchy* — system prompt asserts precedence. Not
   enforceable; a persuasive injection can still win.
3. *Pattern scanning* — catches phrasings someone thought of. Novel
   phrasings are precisely what an attacker produces, so this is treated
   as a flag for review, never as a gate that grants safety.
4. *Capability restriction* — **the only real guarantee.** Generated
   change sets may create new nodes and connect them *to each other*, and
   nothing else. A fully hijacked model cannot modify, invalidate, or
   attach to existing graph content, because those operations are not
   reachable from generated input at all.

The design assumption is that layers 1–3 will eventually be defeated, and
that it must not matter when they are. Layer 4 is what makes that true.
The worst case for a fully successful injection is a junk subgraph in
quarantine awaiting human approval.

**Deliberate non-defence:** `sanitize()` does not strip or rewrite
injection attempts. Rewriting untrusted input mangles legitimate text (a
user writing "ignore malformed rows" about their own pipeline) while a
determined attacker rephrases around whatever the filter looks for. Text
passes through intact, flagged, and contained structurally.

**Verified with real attack payloads** — instruction override, role
reassignment, prompt extraction, delimiter injection, privilege claims,
exfiltration — plus false-positive tests on legitimate text using the
same words. The load-bearing test assumes layers 1–3 *already failed* and
the generator is emitting hostile ops, then asserts the capability check
still contains it.

**Judgement calls worth knowing:** critique objections and suspected
manipulation are surfaced to the reviewer but do **not** auto-reject —
letting one model's opinion silently kill a proposal would make it
authoritative over the human. Structural problems *do* block, because a
change set failing the capability check is either malformed or an
attempted escalation.

---

## Done: applying approved decompositions + Tab 1 frontend

**Backend:**
- `db/05_decomposition.sql` — a `public_generated` provenance value and a
  `decompositions` table (proposals persist rather than being applied
  immediately).
- `KnowledgeUpdater.apply_generated()` — resolves local refs to real ids
  inside one transaction, so an edge can reference a node created
  microseconds earlier in the same set.
- `POST /v1/decompose/{id}/decide`, `GET /v1/decompose/pending`,
  `GET /v1/decompose/{id}`.

**The decision worth knowing:** `apply_generated()` re-runs
`validate_generative()` at apply time rather than trusting the stored
proposal. The capability check already ran at generation, so re-running
it looks redundant — it isn't. Validating once would make the database a
trust boundary it was never designed to be: a proposal tampered with in
storage would apply unchecked. Verified against real Postgres by
attempting exactly that escalation and confirming it's refused.

Everything written is tagged `public_generated`, so an anonymous
submission can never be mistaken for a company's own documented fact.

**Frontend (`frontend_v2`):**
- `app/workbench/page.tsx` — Tab 1. Problem in, decomposition out,
  rendered through the existing `WorkflowGraph`.
- `lib/opsToGraph.ts` — converts generated ops (which carry local `ref`
  strings, since the nodes don't exist yet) into renderable graph shape.
  Pure function, tested standalone across 8 cases including
  edges-declared-before-nodes.

**Ordering decision in the UI:** manipulation warnings render *above*
the proposed plan, not below it. A reviewer who reads the steps first has
already been influenced by them before the warning arrives.

**Test status:** 9 live checks in
`integration_check_v2_decomposition.py`, 8 standalone assertions for the
ops-to-graph converter, frontend production build clean at 7 routes.
- **Layer 2 tiers 1 and 2** — Tier 3 (simulated) exists from V1. Tier 1
  needs the execution-ownership decision; Tier 2 is blocked by a data
  gap (`traces` records executions, not policy decisions, so there is no
  logged action-variation for importance sampling to reweight).

---

## Not built (V2 plan, Section 7)

### Tier 1 — remaining
- **Job queue** (Redis + RQ). Debates still run synchronously inside the
  HTTP request handler — fine for one company, wrong for public traffic.
  This is now the most urgent remaining Tier 1 item.
- **Real authentication.** Blocks private visibility, per above.
- **Connection pool sizing** (still `max_size=10`).

### Tier 2 — load validation
- `GraphStore.traverse_from` has still never been load-tested. Note the
  access predicate adds work to the recursive CTE, so V1's (untested)
  performance assumptions are now slightly more optimistic than reality.
- HNSW tuning at real ingestion volume.

### Tier 3 — new capabilities
- **Tab 2** — the public bounty/review surface. Needs identity and
  rewards first.
- **Identity, reputation, payments** — a full subsystem, and the largest
  single remaining piece. Nothing in the codebase has any concept of a
  public user account, let alone one with standing or money attached.
- **Prover-Estimator** — subtle to implement with soundness intact.
  Becomes load-bearing once rewards exist, since every participant then
  has a documented incentive to make their proposal look better than it
  is.

---

## Test status

- 148 offline tests passing
- `integration_check_v2.py` — 11 live checks (access control, including
  that traversal cannot walk *through* a private edge)
- `integration_check_v2_governance.py` — 15 live checks (rate limiting,
  cost governance, and the concurrency race a transaction alone does not
  prevent)
- `integration_check_v2_decomposition.py` — 9 live checks (applying a
  generated decomposition, and two escalation attempts being refused)
- Inherited from V1: `integration_check.py` through
  `integration_check_4.py`

**Never verified against real models.** Every LLM interaction in V2 —
decomposition, critique, chat — has only ever run against scripted mocks
or a fake OpenAI-compatible server. Whether a real model emits parseable
JSON in the expected shape for decomposition is untested, and is the most
likely thing to break first. Local models via Ollama
(`USE_LOCAL_MODELS=true`) are the cheapest way to find out.

## Setup

```bash
# 1. Backend dependencies
pip install -r requirements.txt

# 2. Database — Postgres 15+ with pgvector (Supabase works)
psql -d your_db -f db/01_ontology.sql
psql -d your_db -f db/02_loop.sql
psql -d your_db -f db/03_access.sql        # new in V2 — visibility/ownership
psql -d your_db -f db/04_governance.sql    # new in V2 — rate limits, spend
psql -d your_db -f db/05_decomposition.sql # new in V2 — proposals, provenance
```

All three V2 files are idempotent and additive — safe to run against an
existing V1 database, in order.

Then, to reproduce the live checks:

```bash
export DATABASE_URL=postgresql://...
python integration_check_v2.py               # access control
python integration_check_v2_governance.py    # rate limits, spend, concurrency
python integration_check_v2_decomposition.py # applying a decomposition
```

Each expects a database it can write to; run them against a scratch
database rather than anything you care about.

```bash
# 3. Config
cp .env.example .env    # set DATABASE_URL at minimum

# 4. Run
uvicorn app.main:app --reload      # http://localhost:8000/health
python -m pytest tests/ -q         # 148 tests, no DB or API keys needed

# 5. Demo data (optional but recommended — an empty graph does nothing)
python scripts/bootstrap_demo.py

# 6. Frontend, from the frontend_v2 folder
npm install && npm run dev         # http://localhost:3000
```

**Running without paid API keys:** set `USE_LOCAL_MODELS=true` and point
it at Ollama (`ollama pull llama3.2 qwen2.5 mistral gemma2
mxbai-embed-large`). The panel, judge, and embeddings all route locally.
See `.env.example`. This is also the cheapest way to close the
"never verified against real models" gap noted above.

---

## Stage 2 real-model testing (post-write, update)

First real-model validation pass, against General Compute (hosted
open-weight, OpenAI-compatible), completed after this document was
originally written. Found and fixed six real bugs, none caught by the
148 offline tests at the time, all now covered or directly verified
against real Postgres:

1. **`retrieval.py` lexical search was structurally broken for natural
   questions.** `plainto_tsquery` ANDs every word together, so a
   question like "What does the extraction step depend on?" required
   all three stemmed terms present, which a short node name almost never
   satisfies. Fixed to OR the same stemmed terms instead. Verified
   directly in SQL before touching code.
2. **`chat.py` hardcoded `AnthropicAgent`**, built before local/General
   Compute provider selection existed, never updated when it was added.
   Silently ignored `USE_GENERAL_COMPUTE` entirely. Fixed with a proper
   `default_chat_agent()` factory; two regression tests added.
3. **`CostGovernor.record()` was never called anywhere.** The budget
   check worked and genuinely blocked requests; nothing ever fed it real
   spend, so the cap looked enforced and wasn't. Fixed by threading an
   `on_call` recording hook through every real LLM call site (panel,
   judge, Layer 2, decomposition, chat). Verified end to end: a real
   debate cycle produced real spend rows, and an artificially-lowered
   cap genuinely blocked a subsequent request.
4. **Decomposition's generator/critic calls had no timeout at all**,
   unlike the debate panel's `gather_responses` (120s). A hung
   connection would wait forever with no way to detect it. Fixed with a
   90s timeout; verified against a deliberately hanging mock (fires at
   exactly 90s, not sooner, not never).
5. **The same trace-ID-collision bug, found three separate times**,
   always the same shape: a fixed literal ID with `ON CONFLICT DO
   NOTHING`, silently inserting nothing on any run after the first
   against a persistent (not recreated) database.
   `integration_check_2.py`, `integration_check_4.py` (a separate,
   unrelated hardcoded-connection-string bug there too), and
   `scripts/bootstrap_demo.py` all had it. Fixed by embedding the
   run's own task/entity ID into each trace ID.
6. **General Compute's price table entries were missing**, so real
   calls fell through to a worst-case-provider estimate (Anthropic's
   rate), not dangerous, but made the cost cap trigger at a fraction of
   real available budget. Added real per-model prices.

Also confirmed working correctly under real conditions for the first
time: the capability boundary (two real adversarial decomposition
attempts, both correctly refused by the model itself before any
operation was ever generated), the chat grounding/citation system (a
real answer with verified citations, three real refusals correctly
flagged `grounding 0.00` rather than fabricating an answer), and one
full debate → Layer 1 → approval → bi-temporal graph update cycle,
confirmed directly against the database, not just trusted from the API
response.

**Utility scripts added during this pass** (diagnostic tools, not part
of the running application): `hide_duplicate_seeds.py` (marks redundant
demo-seed duplicates private without deleting anything -- safe to
re-run), `check_scan.py`, `check_retrieval_2.py`, `check_graph_update.py`,
`unblock_demo_task.py`, `list_models.py`. Each is self-contained and
documents what it checks in its own header.

**Test count: 168 offline tests** (was 148 at initial V2 delivery).

---

## Load testing (Tier 2, first real data)

`scripts/generate_scale_data.py` (bulk-insert synthetic branching
workflows via `copy_records_to_table`) and `scripts/benchmark_scale.py`
(times the three hot paths across 100 / 1,000 / 10,000 task tiers)
answer the question this doc has flagged as open since the first
architecture pass.

**Result: nothing fails at 10,000 tasks / 200,000 traces.** No errors,
no timeouts. The "dedicated graph DB" upgrade trigger has not fired.

**But not flat, either.** `lexical_search` and `trigger_scan` (a
full-table aggregate by design) both grow faster than linear -- roughly
44x and 54x latency for a 100x data increase, landing at 177ms and
123ms respectively at the top tier. Not urgent, but a real, honest
signal, not zero cost as data grows.

`traverse_from` at depth=3 was non-monotonic across tiers (faster at
10,000 tasks than at 1,000), most likely graph-structure variance from
the random branching generator rather than a real trend -- reported
plainly rather than smoothed into a cleaner-looking story.

Ran on local sandbox Postgres, not Supabase's hosted infrastructure --
the scaling shape is the meaningful signal here, not the absolute
millisecond figures.

---

## Execution harness (new capability, reopens Section 10 deliberately)

`app/services/execution.py`. First thing in this project that actually
*runs* a task rather than proposing, debating, or evaluating a change to
one -- everything before this assumed the product only observes and
governs execution owned elsewhere.

Deliberately narrow: `SKILL_REGISTRY` is a closed, hand-written Python
dict, not connected to any external marketplace or dynamic-import path.
Running arbitrary third-party skills is a distinct, later decision
needing its own sandboxing design -- a Snyk audit of public skill
marketplaces found 36% had a security flaw, 76 confirmed malicious, so
this is not something to back into as an extension of a convenience
harness.

**The concrete unblock:** traces produced here are real execution
outcomes, not synthetic seeding. Verified live: running a real skill 10
times (9 failures, 1 success) produced real trace rows that
`TriggerDetector.scan()` genuinely found (`observed_value=0.9`) -- this
is what Layer 2's Tier 2 (off-policy evaluation) has been blocked on
since it was first scoped.

9 offline tests, covering the success path, the failure path (a raising
skill produces a real 'failure' trace, not a crash), an unregistered
skill_ref raising clearly rather than silently no-oping, and trace_id
uniqueness (this project has hit that exact collision bug three times
already in other scripts).
