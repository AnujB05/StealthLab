# Technical Deep Dive: Workflow Debate Platform

Companion to the repos. This document shows the actual mechanisms, the actual bugs found and fixed, and an honest account of what's novel versus what isn't, rather than describing the system at a marketing level.

**Repos:** `backend` (V0), `backend_v1`, `backend_v2` (+ matching frontends). All three run `pip install -r requirements.txt && pytest tests/ -q` with zero external dependencies for the test suite itself.

---

## 1. The core loop

Bottleneck detected in a company's workflow → structured debate among heterogeneous AI models → argument-quality and (where possible) empirical evaluation → human approval → bi-temporal write to the knowledge graph, fully auditable back to who argued for it.

The differentiator is this loop, not the knowledge graph. Knowledge graphs and process-mining tools already exist from well-funded competitors (Celonis, UiPath, and similar). A structured, auditable, multi-party debate that resolves into an approved, versioned, non-destructive change does not, as far as we could establish researching the space directly.

## 2. The ontology: bi-temporal, not just versioned

Every fact in the graph carries two independent timelines, not one:

```sql
t_valid     TIMESTAMPTZ NOT NULL,  -- when this was true in the world
t_invalid   TIMESTAMPTZ,           -- NULL = still true
t_created   TIMESTAMPTZ NOT NULL,  -- when the system learned it
t_expired   TIMESTAMPTZ            -- NULL = not yet superseded
```

Updates are invalidate-and-append, never in-place. `KnowledgeUpdater._supersede_task` closes the old row's window, writes a new one, links them with a `SUPERSEDES` edge, and rewires every edge that pointed at the old version forward to the new one, verified live against real Postgres: an edge dependent on a superseded node does not silently orphan.

Point-in-time reconstruction follows directly: querying with `as_of` returns exactly the graph state at that moment, tested by capturing a real checkpoint between two writes and confirming the query at that checkpoint sees neither the pre-existing future state nor a stale artifact of the test's own timing.

## 3. The debate protocol

Modeled on the Nyaya dialectic tradition rather than an ad hoc multi-agent loop:

- **Vada** (cooperative): the default mode. Panelists propose, amend, or pass, fixed round-robin, terminating when a full round produces no movement or a hard cap is hit.
- **Jalpa** (adversarial): a dedicated agent attacks the leading candidate before it reaches evaluation.
- **Vitanda** (destructive refutation): flagged and structurally down-weighted, refutation without a proposed alternative does not count as a real contribution.
- **Nirnaya** (adjudication): the independent evaluator, enforced via `enforce_independence()`, which checks the judge shares no model family with any panelist, not just a different provider account.

Heterogeneity is enforced, not assumed: `assert_heterogeneous()` checks model *family* rather than model name, so two versions of the same base model (e.g. Llama 3.1 and 3.2) correctly fail the check, since they share pretraining lineage and therefore correlated blind spots.

## 4. Evaluation: Layer 1 (argument quality)

Grounded in the classical Nyaya fallacy taxonomy (hetvabhasa), five types, checked deterministically where possible rather than left entirely to LLM judgment:

- Groundedness is *computed*, not judged: every citation in an argument is checked against the real graph via `node_exists()`. An uncited claim scores 0.0. A citation to a real but different node is caught, not just a missing citation.
- The judge fails **closed**: if the evaluation call errors, the candidate is marked failed, never silently passed. Tested directly (`test_layer1_fails_closed_when_judge_is_unavailable`).

## 5. Evaluation: Layer 2 (empirical, where evidence exists)

Real statistical machinery, validated against known-correct references rather than only checked for whether it runs:

- **Welch's t-test**, not Student's t, because it doesn't assume equal variance, which matters when a candidate change affects consistency as well as average performance. Validated to `1e-9` against `scipy.stats.ttest_ind(equal_var=False)`.
- **Sample size**: $n \approx \dfrac{2(z_{\alpha/2}+z_\beta)^2\sigma^2}{\delta^2}$, returns exactly 16 for the canonical $\sigma=\delta$, $\alpha=0.05$, power$=0.80$ case, the standard textbook worked example.
- **Sequential testing** with an O'Brien-Fleming alpha-spending boundary, so checking results early doesn't inflate the false-positive rate. Tested for the defining property directly: an early boundary is extremely strict (`< 0.001` at 25% information), the final boundary approaches nominal alpha.
- **Benjamini-Hochberg** correction across metrics tested simultaneously, reproduces a standard worked textbook example exactly, and preserves the caller's input order rather than sorted order, an easy bug to ship silently.

Only the weakest of three possible evidence tiers is implemented (Tier 3: an LLM estimating a counterfactual, explicitly labeled "model opinion, not measurement" everywhere it surfaces, never presented as fact). Tiers 1 (shadow deployment) and 2 (off-policy evaluation) are honestly blocked, one on an unresolved product decision (does this system execute workflows or only observe them), the other on a genuine data-model gap: the trace schema records executions, not the policy decisions behind them, so there's no logged action-variation for importance sampling to reweight. This is documented in the code, not glossed over.

## 6. V2: inverting the trust model

V0/V1 assumed private, single-company data. V2 assumes public, anonymous, sometimes adversarial input. Three specific pieces worth the technical detail:

**Access control as a single predicate.** Every visibility check in the system flows through one function (`visibility_predicate()`). This was a direct lesson from the earlier version: a `tenant_id` column had existed on every table since the first schema and no query had ever filtered by it, so isolation was decorative. Centralizing the predicate means enabling private visibility later is a change to one function, not an audit of the whole codebase. Verified against the specific leak a naive implementation ships: a public node reachable only *through* a private edge. Filtering traversal output alone would expose it; the predicate is applied inside the recursive CTE itself, confirmed by constructing exactly that chain (`public → private → public`) and checking an anonymous viewer reaches neither the private node nor the public one behind it.

**A real concurrency bug, found by testing for it specifically.** The first rate-limiter implementation wrapped check-then-insert in a database transaction, which reads as sufficient and isn't: under Postgres's default isolation level, concurrent requests each see a snapshot without the others' uncommitted inserts, so ten simultaneous requests against a limit of three were all allowed. Reproduced deliberately with `asyncio.gather` across ten concurrent calls, fixed with a per-key advisory lock, reverified under the same load.

**Prompt-injection defense as a structural guarantee, not a prompting technique.** The riskiest new capability in V2 is generative task decomposition: a member of the public describes a problem and the system invents new graph structure from it, the first point in the entire system where untrusted text reaches an LLM directly. Four layers, in explicit order of what they can actually guarantee:

1. Delimiting untrusted text, defeated by a fence-guessing attacker.
2. An instruction-hierarchy system prompt, not enforceable.
3. Pattern scanning for known attack phrasings, catches nothing novel by construction.
4. **Capability restriction**, the only real guarantee: generated content may create new nodes and connect them to each other, and nothing else. It cannot modify, invalidate, or attach to anything that already exists, because those operations are not reachable from generated input at all, checked structurally via an explicit allowlist (`GENERATIVE_OP_TYPES`), not by trusting the model to behave.

The design assumption is that layers 1-3 will eventually be defeated and it must not matter when they are. Tested accordingly: the load-bearing test constructs a generator that is *already* fully hijacked and emitting hostile operations, then asserts the capability check still contains it. The same check runs again at approval time, not just at generation, so a proposal tampered with in storage between the two still cannot escalate, verified against real Postgres by attempting exactly that.

## 7. What was actually found by testing, not just written

Eight real defects, all found by running real code against a real database or under real concurrent load rather than only against mocks:

1. Every JSONB write pre-serialized values in Python and cast them in SQL, which silently corrupted how the connection decoded JSON on *subsequent* reads once a custom type codec was registered, affecting five files across the codebase before being traced to its root cause and fixed everywhere.
2. Trigger deduplication checked for an already-open debate, not an already-recorded trigger, allowing duplicate triggers for the same bottleneck in the gap between the two.
3. A variance calculation was numerically unstable on near-constant data, exactly what deterministic replay metrics look like, replaced with a stable two-pass calculation.
4. A pricing-relevant field used relative delta, undefined at a zero baseline, meaning the best possible outcome (0% to 100% success) reported as no value at all.
5. Layer 2 was fully built and fully unit-tested, and never actually reachable from the live API, the orchestrator was constructed without the argument that enables it. Found by checking the call site directly, not by assuming the wiring matched the tests.
6. The frontend never rendered Layer 2 results even after the backend returned them, a staleness gap from sequencing the two builds separately.
7. The rate limiter's concurrency race, described above.
8. A Next.js version with a published critical CVE was caught via `npm audit` before any application code was written against it.

## 8. Honest novelty assessment

Not everything here is novel, and claiming otherwise would undercut the parts that are:

| Piece | Assessment |
|---|---|
| Knowledge graph + bottleneck detection | Not novel. Celonis, UiPath, and several funded competitors do this well already. |
| Structured multi-party debate → eval → approval → auditable update | The actual differentiator. No direct competitor found running this exact closed loop. |
| Distillation of small models for narrow tasks | Sound engineering, industry-standard practice, not a differentiator on its own. |
| Bi-temporal, non-destructive update semantics | A known technique from temporal-database literature, applied deliberately here, not invented here. |
| The capability-boundary defense against prompt injection | A specific, structural design choice, tested against a deliberately hijacked model, distinct from prompting-only defenses common elsewhere. |

## 9. Test coverage

148 offline tests (no database, no network, no API keys) covering debate convergence and termination, statistical functions against reference values, citation verification, the capability boundary under simulated hijack, rate-limit and cost logic in isolation.

Seven scripts run the same code against a real, disposable Postgres instance rather than mocks: bi-temporal traversal and update correctness, the debate state machine's transactional behavior, the full loop from trigger to persisted evaluation, the distinction between "found nothing wrong" and "every model call silently failed," access control including the leak case above, rate limiting under real concurrency, and applying an approved public decomposition including both escalation attempts being refused.

**What has not been tested:** any interaction with a real frontier model. Every test in the project runs against a scripted response or a small local model, since paid API access was unavailable during development. This is the single largest known unknown and is documented as such rather than implied to be solved.

## 10. What's queued, not hidden

Job queue for public-scale traffic (debates currently run synchronously in the request handler), real authentication (a placeholder header currently, safe only because nothing is private yet), the public review-and-reward surface, identity and payments, the Prover-Estimator asymmetric debate protocol (needed once rewards create a documented incentive to mislead), and the stronger two tiers of empirical evaluation, one blocked on a product decision, one on a data-model gap, both documented precisely rather than left vague.
