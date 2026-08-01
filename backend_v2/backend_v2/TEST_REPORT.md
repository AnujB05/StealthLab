# Phase A — Test & Verification Report

Everything below actually ran in this session. Commands are exact; where
output is shown, it's the real output, not a paraphrase. Reproduce any of
it by running the same commands against the code in the delivered zip.

## Contents
1. [Environment](#1-environment)
2. [Offline test suite — 38 tests, 4 files](#2-offline-test-suite)
3. [Why live testing was necessary](#3-why-live-testing-was-necessary)
4. [Live integration check 1 — GraphStore, KnowledgeUpdater, bi-temporal](#4-live-integration-check-1)
5. [Live integration check 2 — DebateStateMachine, TriggerDetector](#5-live-integration-check-2)
6. [Live integration check 3 — ingestion, Layer1Evaluator, approval endpoint](#6-live-integration-check-3)
7. [Bugs found and fixed](#7-bugs-found-and-fixed)
8. [False alarms — failures traced to the test, not the code](#8-false-alarms)
9. [Reproduction instructions](#9-reproduction-instructions)
10. [Coverage summary and known gaps](#10-coverage-summary-and-known-gaps)

---

## 1. Environment

No Postgres was available at the start of this session. Installed directly
in the sandbox, not mocked or simulated:

```
apt-get install -y postgresql postgresql-contrib      # → Postgres 16.14
apt-get install -y postgresql-16-pgvector              # → pgvector 0.6.0
service postgresql start
createdb workflow_test
```

Confirmed via `SELECT version()`:
```
PostgreSQL 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu
```

One environment quirk worth knowing: **the Postgres service does not stay
running between separate tool invocations in this sandbox** — it had to be
restarted with `service postgresql start` multiple times over the session.
Data survived each restart; only the running process needed restarting.
If you reproduce this and see `ConnectionRefusedError`, that's why.

## 2. Offline test suite

No database required. Run with `python -m pytest tests/ -q`.

| File | Tests | What they cover |
|---|---|---|
| `test_loop_logic.py` | 14 | JSON extraction from model output, panel heterogeneity enforcement, debate convergence vs. round-cap vs. no-candidates termination, supporter counting and the eligibility gate, malformed-amend handling, agent-failure resilience mid-round, state-machine transition legality, `ChangeSet` structural validation, judge-independence enforcement, Layer 1 fail-closed behavior on judge outage, fallacy-category invention rejection, uncited-proposal zero-groundedness |
| `test_ingest.py` | 5 | Malformed record doesn't fail the batch, missing required fields rejected not crashed, duplicate `trace_id` counted not rejected, FK-violation-shaped errors rejected per-record, non-list `records` payload handled |
| `test_markdown_diff.py` | 10 | Empty change set renders explicitly, each `ChangeSet` op type renders correctly, non-destructive language is present for edge invalidation, node names substitute correctly, Layer 2 absence is always stated explicitly (never silently omitted), recommendation is always labeled advisory, fallacy flags and quotes render, export includes approver and change content |
| `test_panel_agents.py` | 9 | `AnthropicAgent`/`OpenAICompatAgent` correctly extract text from mocked SDK response shapes (including mixed content blocks and `None` content), `_extract_json` against realistic model-output quirks: trailing commentary, leading commentary before a fence, nested objects inside `change_set`, single-quoted pseudo-JSON (must fail), multiple fenced blocks in one response |

**Result, last run:**
```
python -m pytest tests/ -q
...................................... [100%]
38 passed in 1.86s
```

## 3. Why live testing was necessary

Offline tests mock the database; they cannot catch bugs that only exist in
the interaction between application code and a real Postgres connection —
type codec registration, transaction/lock semantics, or timing between two
separate calls. Two bugs in this codebase were specifically of that shape
(§7) and were invisible to all 38 offline tests, which still pass with
both bugs present. That's the reason three separate live scripts exist
rather than trusting the offline suite alone.

## 4. Live integration check 1

`integration_check.py` — `GraphStore`, `KnowledgeUpdater`, bi-temporal correctness.

Seeds a 3-task workflow (intake → extract → review) via the real
`Onboarder`, then:

```
[PASS] onboarding seeds real rows
[PASS] traverse_from finds the 2-hop neighborhood
[PASS] node_exists confirms a real, valid node
[PASS] blast_radius counts dependents
[PASS] apply() returns the new node id
[PASS] old version was closed, not deleted
[PASS] new version carries the change
[PASS] new version carries forward untouched fields
[PASS] new version is currently valid
[PASS] SUPERSEDES edge links new version to old
[PASS] edges into/out of the old node were rewired to the new node
[PASS] no live edges remain pointing at the superseded old node
[PASS] the workflow is still traversable intake -> ... -> review after supersession
[PASS] as_of before the change reflects the pre-supersession graph state
[PASS] as_of before the change does not yet see the new node's edges
[PASS] as_of now (default) sees the new node's edges

ALL CHECKS PASSED against a real Postgres instance.
```

The middle block (`apply()` through `traversable ... after supersession`)
is the part flagged as highest-risk before this session — `_supersede_task`'s
edge-rewiring. Specifically verified: an edge pointing at the *old*
`TaskNode` version gets recreated against the *new* version and the old
edge closed, so a dependent task doesn't silently lose its connection
when the node it depends on is superseded.

## 5. Live integration check 2

`integration_check_2.py` — `DebateStateMachine`, `TriggerDetector`. Neither
had any coverage, offline or live, before this check was written.

```
[PASS] current_state reads the real row
[PASS] transition writes the new state
[PASS] the write actually persisted
[PASS] debate_events recorded the transition
[PASS] illegal transition (IN_DEBATE -> APPROVED) is rejected
[PASS] a rejected transition did not mutate the row
[PASS] scan finds the bottleneck
[PASS] observed error rate is computed correctly
[PASS] a threshold nothing crosses produces no hits
[PASS] min_samples gate suppresses low-volume noise
[PASS] record() persists a trigger row
[PASS] a second scan does not duplicate an open debate's trigger

ALL CHECKS PASSED.
```

Real trace data (15 failures, 5 successes) was inserted and
`TriggerDetector.scan()`'s `GROUP BY`/`HAVING` aggregation was confirmed to
compute the correct 75% error rate and correctly ignore a threshold
nothing crosses and a rule whose `min_samples` gate isn't met.

## 6. Live integration check 3

`integration_check_3.py` — ingestion endpoint, `Layer1Evaluator` combined
with a live `GraphStore`, and the full approval `decide()` endpoint,
covering both the approve and reject paths.

```
[PASS] valid record accepted
[PASS] FK violation rejected per-record, not a crash
[PASS] batch did not abort on first bad record
[PASS] re-sending the same trace_id counts as duplicate, not error
[PASS] the accepted record actually persisted correctly
[PASS] a citation to a real, live node scores full groundedness
[PASS] candidate with a real citation and clean judge passes
[PASS] a citation to a nonexistent node scores zero groundedness
[PASS] nonexistent citation shows up in unresolved_cites
[PASS] candidate with only a fake citation fails eval
[PASS] mixed real/fake citations score partial groundedness
[PASS] decide() returns applied ops
[PASS] decide() renders export markdown
[PASS] the approved change actually landed in the graph
[PASS] approver_role was actually persisted (this was the bug fixed earlier)
[PASS] debate transitioned to APPROVED
[PASS] rejection applies nothing
[PASS] debate transitioned to REJECTED

ALL CHECKS PASSED.
```

The `decide()` block is the most consequential — it exercises the actual
FastAPI route function against real state: reading a scorecard, applying
its change set, writing the approval row, transitioning the debate, and
rendering the export, in one real sequence, for both outcomes.

## 7. Bugs found and fixed

**Bug 1 — JSONB write pattern corrupted connection-level decoding.**
Every JSONB write pre-serialized with `json.dumps()` in Python and cast
`$N::jsonb` in SQL. Diagnosis, in order:
1. `GraphStore.traverse_from` raised a defensive error (`properties`
   decoded as `str`) the first time it ran against real data.
2. Isolated: a literal `SELECT '{"a":1}'::jsonb` decoded correctly with the
   codec registered; a real table column did not.
3. Isolated further: writing via `json.dumps()` + `::jsonb` cast on a
   codec-registered connection, then reading on that *same* connection,
   returned `str`. Writing the identical value via a codec-free connection,
   then reading via a *different*, codec-equipped connection, returned
   `dict` correctly.
4. Root cause: pre-serializing and casting bypasses the codec's encoder,
   which desynchronizes the connection's type resolution for later reads.

Fixed in all five affected files (`app/onboarding/seed.py`,
`app/services/knowledge_update.py`, `app/services/loop.py`,
`app/services/triggers.py`, `app/api/approval.py`) by passing native
Python objects and letting the codec encode them, dropping the manual
`::jsonb` casts.

**Bug 2 — trigger deduplication had a real gap.**
`TriggerDetector.record()` skipped inserting a new trigger if an *open
debate* already existed for that task node — but `record()` never opens
a debate itself; that happens in a separate `LoopOrchestrator` call.
Reproduced live: recording a trigger, then scanning and recording again
before any debate was opened, produced two trigger rows for the same
bottleneck. Confirmed via direct query that `debate_id` was `NULL` on
both. Fixed by checking for an unresolved *trigger* (via `LEFT JOIN
... WHERE d.id IS NULL OR d.state NOT IN (...)`) rather than only an open
debate.

## 8. False alarms

Three failures during this session were investigated and traced back to
the test script, not the application code. Recorded here because "it's
probably a test bug" is exactly the kind of claim that shouldn't be
taken on faith — each was actually checked before being dismissed.

1. **Bi-temporal `as_of` check, first version.** Looked back 30 seconds
   from "now," but the whole test script runs in well under a second, so
   the lookback point predated the data by design, not by bug. Confirmed
   by querying the actual age of the oldest edge (~16 seconds) against a
   30-second lookback. Fixed by capturing a real checkpoint timestamp
   between onboarding and supersession instead of an arbitrary offset.

2. **Trigger dedup, first failure.** The test reused one task node across
   both the state-machine section and the trigger-detection section; the
   debate left open by the first section correctly suppressed a new
   trigger in the second. Confirmed by querying `triggers JOIN debates`
   and finding the leftover `IN_DEBATE` row. Fixed by using separate task
   nodes per section.

3. **Approval endpoint setup, `NOT NULL` violation.** A fallback SQL
   pattern (`INSERT ... (SELECT id FROM triggers LIMIT 1) ...`) assumed a
   failed subquery would return `NULL` gracefully; it raised a
   NOT NULL constraint violation instead, since no trigger existed yet at
   that point in the script. Fixed by creating the trigger directly
   instead of relying on a fallback that never actually worked.

## 9. Reproduction instructions

```bash
# 1. Offline suite - no database needed
pip install -r requirements.txt --break-system-packages
python -m pytest tests/ -q

# 2. Live checks - need a real Postgres with pgvector
createdb workflow_test
psql -d workflow_test -f db/01_ontology.sql
psql -d workflow_test -f db/02_loop.sql
export DATABASE_URL=postgresql://user:pass@localhost/workflow_test
python integration_check.py
python integration_check_2.py
python integration_check_3.py
```

Each `integration_check*.py` is self-contained and expects a fresh (or at
least non-conflicting) database — re-running against a database that
already has data from a prior run can produce unrelated-looking failures
from unique-constraint collisions (see §8.3's category, though that one
was a different root cause). Safest reproduction is `dropdb` / `createdb`
between runs.

## 10. Coverage summary and known gaps

| Item | Verified live | Verified offline only | Not verified at all |
|---|---|---|---|
| 1. Ontology | ✓ | | |
| 2. Ingestion | ✓ | | |
| 3. Vāda debate | | ✓ (engine, SDK parsing, JSON extraction) | **Real LLM API calls** |
| 4. Layer 1 eval | ✓ | | |
| 5. Approval + update | ✓ | | |
| 6. Delivery formatter | | ✓ | |
| 7. Onboarding | ✓ | | |
| State machine | ✓ | | |
| Trigger detection | ✓ | | |

**The one gap this environment cannot close:** whether Claude, Kimi K3
(via Fireworks), and GPT actually produce output `_extract_json` can parse
in practice. No API credentials are available here. Everything *around*
that question — engine logic, SDK response parsing, JSON extraction
against realistic quirks — is tested; the real API behavior itself isn't.
First thing worth running once credentials exist.

**Also not exercised:** vector index behavior at scale, concurrent writers
against `DebateStateMachine`'s row lock, and anything resembling
production load. All three checks above ran against single-digit to
low-double-digit row counts.
