# V1 Status — What's Done, What Isn't

Upload this back into a fresh conversation to resume. Updated after items
#2 and #3 were built.

---

## Item #1 — SLM + task-graph efficiency routing

**Status: NOT STARTED. Split into two halves after review — only one is
genuinely blocked.**

- **1a, routing logic** ("does an approved precedent exist for this task
  type, and is blast_radius low? -> cheap path : full debate"). Buildable
  and testable now. Needs a precedent-matching query and a branch. Not
  blocked.
- **1b, actual distillation** (training a small model on resolution
  trajectories). Genuinely blocked, and *not* by API access: it needs
  many completed debates with real approved outcomes. Running local
  models generates volume but only simulated-quality trajectories —
  distilling on those teaches a model to imitate a 7B model's judgement,
  which is not the goal.

**The idea:** not every bottleneck needs the full 4-model debate panel.
Route by precedent:

```
Trigger fires
   -> existing approved precedent for this TaskNode type + low blast_radius?
        yes -> apply via a cheap, distilled SLM, tagged in provenance
               as "precedent-applied" not "freshly deliberated"
        no  -> full Vada debate (current system, unchanged)
```

**Where it plugs into what already exists:**
- `TaskNode.skill_ref` — natural home for "which distilled model handles
  this task type"
- `blast_radius` (already computed, already live-tested) — the routing
  signal
- `scorecards.recommendation` — extend to distinguish
  "adjudicated by full panel" vs. "applied via established precedent"

**Hard dependency:** needs Layer 2 (empirical replay testing) to exist
first — distillation needs real labeled resolution trajectories, and
there are currently zero completed debates to learn from. **Layer 2 does
not exist.** Don't start this before it does.

**Also reopens Section 10:** an SLM *deciding* fits the current
overlay-shaped design. An SLM *executing* the task only makes sense if
the product owns execution — still-unresolved runtime-vs-overlay
question.

---

## Item #2 — Task decomposition visualization

**Status: BUILT.**

- Backend: `GET /v1/graph/{node_id}?depth=N` in `app/api/graph.py`.
  Reuses `GraphStore.traverse_from`. Verified against a real running
  server with real data.
- Frontend: `components/WorkflowGraph.tsx`, rendered on the case-file
  page below the metrics section.
- `app/api/approval.py`'s detail endpoint now also returns
  `task_node_id` so the frontend knows what to center the graph on.

**Design note:** hand-rolled SVG, not React Flow. These graphs are 3-10
nodes and the design language is hand-built CSS; a library would add a
dependency plus its own visual conventions to override. Revisit past
~30 nodes or if pan/zoom becomes necessary.

**Verified:** layout algorithm tested standalone against 6 cases
including a pure cycle (`traverse_from` walks edges bidirectionally and
*can* return cycles — a naive topological sort would hang). Production
build clean.

---

## Item #3 — Knowledge chat / retrieval

**Status: BUILT. Never run with real embeddings.**

**Backend:**
- `app/services/embeddings.py` — Voyage wrapper. First code in the
  project that actually calls Voyage; the `VECTOR(1024)` columns and
  HNSW indexes had existed unused since the first schema.
- `app/services/retrieval.py` — hybrid search (vector + Postgres FTS)
  fused via Reciprocal Rank Fusion, then bounded graph expansion.
- `app/services/chat.py` — grounded answering with citation
  verification against the real graph.
- `app/api/chat.py` — `POST /v1/chat`.
- `scripts/backfill_embeddings.py` — for nodes seeded before embedding
  generation existed.
- `app/onboarding/seed.py` — now embeds during seeding.

**Frontend:**
- `app/archive/page.tsx` — the chat surface.
- `components/GroundedAnswer.tsx` — renders inline citations as
  numbered superscripts, teal when verified against the graph, rust
  when not.
- Nav tabs added to both `/approvals` and `/archive`.

**Design notes:**
- RRF rather than weighted score fusion: cosine similarity and
  `ts_rank` are on incomparable scales, so summing them needs an
  arbitrary normalization constant that silently shifts behavior as
  either distribution changes. RRF only uses rank position.
- Empty retrieval short-circuits *without calling the model* — with no
  context there's nothing to ground against, so any output would be
  exactly the unattributable general knowledge the prompt forbids.
- Citation verification is computed against the database, never judged
  by a model. Same standard as `Layer1Evaluator._groundedness` — reused
  deliberately rather than building a looser second notion of
  "grounded" for chat.
- Unverified-by-default: a citation id in neither the resolved nor
  unresolved list renders as unverified. Over-flagging is a minor
  annoyance; falsely presenting an invented citation as verified is the
  failure this layer exists to prevent.

**What is NOT verified:**
- **No real Voyage call has ever succeeded.** The `voyageai` package
  isn't installed in the dev sandbox and no API key was available.
  Retrieval was verified end-to-end against real Postgres using
  *synthetic* embedding vectors — this proves the SQL, the RRF fusion,
  the graph expansion, and the context rendering are correct, but says
  nothing about real retrieval *quality*.
- **No real chat model call has ever been made** through this path.
  Citation parsing and grounding logic are tested (10 offline tests
  plus 8 standalone parsing assertions), but no actual model output has
  ever flowed through them.
- Graceful degradation *is* verified: with Voyage unavailable, seeding
  logs the failure loudly, still succeeds, and nodes remain usable.

---

## Immediate next steps when resuming

1. **Get a Voyage API key** — this is the single thing blocking
   verification of item #3.
2. `pip install -r requirements.txt` (the `voyageai` package is likely
   not installed in your environment yet)
3. `python scripts/backfill_embeddings.py` — **essential.** Every node
   in any existing database was created before embeddings existed, so
   semantic search is currently blind to all of them with no error to
   indicate why.
4. Test: `POST /v1/chat` with a real question, then check
   `/archive` in the UI.
5. Watch for: does the model actually emit `[task:<uuid>]` citations in
   the expected format? This has never been observed with a real model
   — if it doesn't, the citation prompt in `CHAT_SYSTEM` needs work,
   and the groundedness score will read 0.00 for every answer.

## Test status

- 48 offline tests passing
- 4 live integration checks against real Postgres
  (`integration_check.py` through `integration_check_4.py`)
- Frontend production build clean, 6 routes
