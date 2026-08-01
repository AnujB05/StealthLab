# V2 — Public Platform: Vision, Verdict, and Build Plan

Upload this back into a fresh conversation to resume V2 planning or
begin building. Written after V1 was scoped but before any V2 work
started — treat this as the discussion record plus a concrete plan,
not yet-started code.

---

## 1. The vision, as described

A two-tab public platform, separate in trust model from the current
private governance tool:

**Tab 1 — problem-in, solution-out chat interface.** A public internet
user describes a problem in natural language, optionally uploads
documents. Backend processes the prompt + docs, searches the knowledge
layer for existing relevant solutions, builds a task decomposition, and
returns an agentic solution in the same interface.

**Tab 2 — a "LessWrong for agentic solutions."** Public page showing
every problem the system has solved, the solution given, and relevant
metrics. Open to the internet. People propose more efficient solutions
and earn a reward for accepted ones. The existing eval/debate mechanism
runs here too (modified), and successful proposals get written into the
knowledge system.

**Open sub-question raised alongside this:** where to source an initial
"agent library" (with or without benchmarks) to seed the system —
addressed in Section 5.

---

## 2. The rigorous verdict

**This is a different product, not an extension of the current one.**
Real, substantial overlap exists in the *engine* — the ontology, the
eval mechanism, the update mechanism. The *trust model* underneath both
is inverted, and that's not a small detail: the whole current
architecture (tenant isolation, provenance rules, "never share company
content across tenants") was built around private, per-company data.
This platform's premise is the opposite — public, shared, adversarial by
default.

**Recommendation, not a compromise:** build V2 as a second product
sharing the same underlying engine (ontology + debate + eval
infrastructure), running its own instance-graph, its own provenance
rules, its own (heavier) trust requirements. Don't force one
architecture to serve both trust models — that mirrors the same
discipline the whole build has followed already (generic machinery once,
instantiated per context).

### Tab 1 — overlap and gap

| | |
|---|---|
| **Overlaps directly** | Retrieval (hybrid search over the knowledge layer) — this is V1 item #3, already queued, no new design needed |
| **Real, unbuilt gap** | *Generative* task decomposition. Current system only ever *curates* (a human writes a `WorkflowSpec`, `Onboarder.seed()` inserts it — zero reasoning). Tab 1 needs an LLM to synthesize a fresh task graph from an arbitrary, never-seen problem, live, per-request. See Section 4 for the precise mechanism gap. |
| **Structural tension** | Current tenancy model assumes private, per-company knowledge that never crosses tenant boundaries. Tab 1's premise — a shared pool of solutions searchable by anyone — is the inverse, not a variation. |

### Tab 2 — overlap and gap

| | |
|---|---|
| **Overlaps strongly** | Scorecard display ≈ existing case-file UI, just currently gated private. `KnowledgeUpdater.apply()` is directly reusable — already built, already live-tested. |
| **Non-obvious, valuable connection** | Every public participant chasing a reward has a documented incentive to make their proposal look better than it is. That is *exactly* the trigger condition already written into the V1 roadmap for the Prover-Estimator asymmetric debate protocol — "a panelist with a documented incentive to mislead." On a bounty platform, that's not an edge case, it's the default condition of every submission. |
| **Genuinely missing** | Identity, reputation, reward/payment infrastructure. Nothing in the current system has any concept of a public user account, let alone one with money or standing attached. |
| **Priority inversion vs. current backlog** | Layer 2 (empirical replay testing) was correctly deferred in the private-company case — Layer 1 + a trusted human approver was an acceptable interim state. On a public, high-volume, adversarially-incentivized platform, an LLM fallacy-check alone is a much weaker filter against someone actively gaming a reward. Layer 2 moves from "later" to "prerequisite for public trust." |

---

## 3. Relationship to V1 — not a separate track

Several V1 items are direct prerequisites or immediately reusable
components for V2, not parallel unrelated work:

- **V1 #3 (knowledge chat interface)** — the retrieval half of Tab 1.
  Build this in V1, reuse it directly.
- **V1 #2 (task decomposition visualization)** — `GET /v1/graph/{id}`
  already exists and is live-tested; it can render a *newly generated*
  Tab 1 decomposition just as easily as an existing seeded one. No
  changes needed to reuse it here.
- **V1 #1 (SLM + graph routing)** — becomes more important at V2 scale,
  not less. Many concurrent public debates multiply LLM cost across all
  four providers simultaneously; cheap-model routing for
  precedent-matched cases is a real cost lever once volume is public
  rather than one company's internal bottleneck rate.

Build order implication: **V1 should land before V2 starts in earnest** —
not as a hard gate on everything, but because two of its three items are
literally inputs to V2's Tab 1.

---

## 4. Task decomposition — precise mechanism comparison

**What the current system does:**
1. A human writes a `WorkflowSpec` by hand — all the thinking happens
   here, offline, once.
2. `Onboarder.seed()` inserts it. Pure ETL, zero reasoning.
3. The only autonomous LLM-driven changes to the graph go through
   `ChangeSet`'s three operations — `UpdateTaskNodeOp`,
   `InvalidateEdgeOp`, `CreateEdgeOp` — and **every one of them requires
   the node(s) involved to already exist**. `KnowledgeUpdater._create_edge`
   explicitly checks both endpoints are real rows before writing
   anything. There is no `CreateTaskNodeOp`.

Net effect: even the one part of the system with real autonomous
reasoning (the debate panel) can only refine or reconnect a structure a
human already built. It cannot introduce a task that didn't exist a
moment ago. This is a seed script plus a change-review process, not a
planning agent — and that was the right shape for what V0/V1 needed.

**What Tab 1 needs:** live, per-request synthesis of a genuinely new
task graph from unstructured input (text + documents) that has never
existed in the system, continuously, for every user — not once, by a
person who already understood the workflow.

**The concrete build items this implies:**
- A new `CreateTaskNodeOp` (and likely `CreateKnowledgeNodeOp`) added to
  the `ChangeSet` vocabulary — currently structurally absent, not just
  unused.
- A decomposition-generation component upstream of the graph: takes
  unstructured input, retrieves related context (reuses V1 #3's
  retrieval), synthesizes a candidate subgraph.
- A validation loop for that generation step — the input space is now
  unbounded, so this needs something like `ChangeSet.validate_ops()`'s
  discipline but for far less constrained output. Likely needs a
  propose-then-critique pattern, which also implies building Jalpa (the
  adversarial debate mode, already designed, never built) sooner than
  its original V1.2 trigger would have required.

---

## 5. Ingestion sources for an initial agent library — verification status

Two genuinely different categories, don't conflate them:

**Benchmark suites** (GAIA, SWE-Bench Verified, OSWorld, τ²-Bench,
WebArena, METR) — methodologically verified but not contamination-free.
SWE-Bench needed an OpenAI-audited "Verified" subset because the
original had quality issues; GAIA's questions have been public since
November 2023, so model memorization is a live risk on any current
score. Useful for *grading*, not to be trusted blindly as ground truth.

**Agent/skill marketplaces** (Skills.sh, SkillsMP, ClawHub, the various
GitHub-aggregated directories) — mostly explicitly low-curation.
SkillsMP's own description: "800,000+ skills scraped from public GitHub
repositories... minimal curation." A few (Agensi) claim real vetting;
most don't.

**A 2025-2026 research paper ("AgentHub") explicitly frames this as an
open, unsolved gap** in the field — infrastructure for discoverable,
verifiable, governable agents doesn't yet exist at the maturity of npm
or Hugging Face's model hub. No single clean source to trust.

**Recommendation, consistent with the system's own design principles:**
never ingest external library content directly as trusted seed content.
Tag it `prior_library` provenance (already the exact mechanism built for
this), and — better — run candidate entries *through* the system's own
Layer 1 (and eventually Layer 2) pipeline before they count as real
content. Use the eval mechanism you already built as the verification
step none of these external sources actually provide.

---

## 6. Scalability audit — current backend, verified against the actual code

Not estimated — checked directly:

| Finding | Evidence |
|---|---|
| **No background job queue.** Debates run synchronously inline in the HTTP request handler. | `app/api/admin.py` line 87: `await orchestrator.run(trigger_id)` blocks the response for the full multi-round, multi-provider debate. |
| **Connection pool capped small.** | `app/db/session.py`: `min_size=1, max_size=10`. |
| **Graph traversal never load-tested.** | `GraphStore.traverse_from`'s own docstring: "NOT LOAD TESTED. Revisit before production traffic." All live verification this session ran against single-digit to low-double-digit row counts. |
| **Tenant isolation is decorative, not enforced.** | Zero query in `graph_store.py` or any `services/*.py` file filters by `tenant_id` — confirmed by direct search. The column is written on every insert and read by nothing. Setting different tenant IDs today would not actually isolate anyone's data. |
| **No auth enforcement.** | `approver_role` exists as an unenforced placeholder field, by original design (Section 12), correctly deferred for a single-company internal tool. |
| **No rate limiting or cost governor.** | Nothing prevents repeated calls to `/v1/admin/scan`, each of which spends real money across four LLM providers concurrently. |

**Framing, not a criticism:** every one of these was a correct, deliberate
deferral for what V0/V1 needed — one company, trusted internal actors,
low request volume. None are bugs. They become day-one requirements only
in the V2 context, which is exactly why V2 is scoped as its own effort
rather than "V1 plus hardening."

---

## 7. V2 build plan — tiered by risk and novelty, not just sequence

### Tier 1 — infrastructure hardening (routine, known patterns, do first)
- Job queue (Redis + RQ, already named in the original stack plan,
  never wired in) — get debates off the request thread
- Connection pool sizing, properly capacity-planned
- Real `WHERE tenant_id = $current` enforcement on every query — the
  column already exists everywhere; this is "finally use it"
- Auth (Clerk/Supabase Auth, the intended path since Section 12)
  resolving to a real tenant per request
- Rate limiting and a cost governor on every LLM-calling endpoint

### Tier 2 — load validation (moderate risk, needs real testing)
- Load-test `GraphStore.traverse_from` at realistic scale (thousands to
  millions of nodes, not single digits). Real possibility, not just a
  formality: this is where the "dedicated graph DB" upgrade trigger,
  hypothetical since the first architecture doc, might fire for real.
- HNSW tuning (`maintenance_work_mem`, `ef_search`) at genuine library-
  ingestion volume.

### Tier 3 — genuinely new capabilities (real R&D, highest effort and risk)
- **Generative decomposition + validation loop** (Section 4) — the
  `CreateTaskNodeOp` gap, the generation component, the propose-then-
  critique validation, likely pulling Jalpa's build-out forward.
- **Identity, reputation, and payments** — a full subsystem, comparable
  in scope to a meaningful chunk of what's already built, zero overlap
  with the current system.
- **Prover-Estimator, implemented for real** — already named as needed
  in V1's roadmap; the underlying protocol (Brown-Cohen/Irving et al.)
  is genuinely subtle to implement with its soundness guarantees intact.
- **A threat model the current system was never built for.** Highest
  *risk* item, not highest effort. Every input trusted so far has come
  from a company's own systems or good-faith employees. The moment Tab 1
  exists, arbitrary public text (including uploaded documents) flows
  directly into the LLM pipeline — prompt injection becomes a live
  concern from day one, not a hypothetical. Nothing in the current
  design defends against this, because nothing needed to. Design this in
  from the start of Tab 1's build, not as a retrofit.
- **Layer 2, no longer deferrable** — already established as
  load-bearing for public trust; still doesn't exist; already flagged as
  substantial work when first scoped.

---

## 8. Net verdict

Buildable, with real confidence — nothing here requires a different
paradigm, the ontology/eval/update core generalizes. But honestly: plan
for this as roughly a second V0-scale effort, not "V1 plus some
hardening." Tier 1 is fast and low-risk. Tier 2 carries genuine technical
uncertainty. Tier 3 is where most real time goes, and the security
posture (prompt injection, adversarial input by default) is the one item
worth having firmly in scope from the first line of code — retrofitting
input-safety onto a system that never had it is a much worse position
than designing for it from the start.

## 9. Open decisions to resolve before starting

- Confirm V1 has actually landed first, or at minimum V1 #2 and #3
  (the direct Tab 1 prerequisites)
- Pick the initial agent-library sourcing strategy (which 2-3 sources
  from Section 5, and confirm the "run through Layer 1 before trusting"
  pipeline is built before any bulk ingestion)
- Decide graph-DB migration timing — before or after Tier 2's load
  testing produces a real number to decide against, rather than
  guessing
- Payment/reward mechanism choice (Stripe Connect vs. alternatives) —
  not analyzed here, needs its own pass
- Content moderation policy for Tab 1/Tab 2 public submissions — a
  policy decision as much as a technical one, worth deciding before
  Tier 3 implementation starts, not during it
