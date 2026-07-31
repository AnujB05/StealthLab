# Docket — the v0 approval UI

Connects directly to the Phase A backend. Three routes: `/approvals` (the
docket — pending scorecards), `/approvals/[id]` (the case file — full
argument, evidence, objections, transcript, and the approve/reject
ruling), and `/` (redirects to the docket).

## What was actually verified here

`npm run build` — production build, TypeScript type-checking, and static
generation all confirmed clean. One thing this sandbox genuinely
couldn't check: `next/font/google` needs to reach `fonts.googleapis.com`
at build time, and that domain isn't reachable from this environment.
Isolated by temporarily stripping font loading and confirming everything
*else* compiled — it did, cleanly, all three routes. The font-loading
layout has been restored and is what's shipped; it just hasn't built
successfully in *this* sandbox specifically. It will on Vercel, which has
normal internet access. Worth running `npm run build` yourself once,
first thing, rather than taking that on faith.

One dependency note: `npm install` initially resolved a Next.js version
with a published critical CVE (cache poisoning / RCE-adjacent, per `npm
audit`). Bumped to the patched release before writing any app code
against it — check `npm audit` yourself after `npm install` if you add
or change dependencies later. The remaining `npm audit` findings are in
`sharp`, which only matters for `next/image`; this app doesn't use it.

## Setup

```bash
npm install
cp .env.local.example .env.local
# edit .env.local if the API isn't on localhost:8000
npm run dev
```

Requires the backend running with `FRONTEND_ORIGIN` in its `.env` set to
match wherever this runs (`http://localhost:3000` for local dev — that's
already the backend's default).

## The API contract this depends on

`lib/api.ts` is the single source of truth for the shapes this app
expects — every type in it mirrors a real backend model or SQL row, not
a guess. If the backend's response shape changes, this is the one file
to update; nothing else in the app touches the API directly.

| Call | Backend route | Used by |
|---|---|---|
| `api.listPending()` | `GET /v1/approvals/pending` | docket page |
| `api.getDetail(id)` | `GET /v1/approvals/{id}` | case file page — this route didn't exist before this session; the list endpoint alone doesn't return enough to review responsibly (no fallacy flags, no change set, no transcript) |
| `api.decide(id, ...)` | `POST /v1/approvals/{id}` | the approve/reject buttons |
| `api.runScan()` | `POST /v1/admin/scan` | the "Run scan" button — see below |

## The gap this surfaces, worth knowing before you rely on the demo

Before this session, nothing in the backend ever called
`TriggerDetector.scan()` or `LoopOrchestrator.run()` — the loop existed
as code but nothing invoked it automatically. For v0 there's still no
scheduler; `POST /v1/admin/scan` is a manual trigger, meant to be called
from a cron job, a dashboard button (which is what "Run scan" on the
docket page is), or `curl`, until real scheduling is worth building.

**On a fresh database, "Run scan" will correctly find nothing** — there's
no trace data yet for anything to cross a threshold on. Run the
backend's `python scripts/bootstrap_demo.py` first (seeds a demo
workflow and trace data specifically shaped to trigger); only then does
"Run scan" have a bottleneck to find.

**It also needs all four LLM provider API keys configured in the
backend's `.env`** (Anthropic, Fireworks, OpenAI, Google — the fourth
exists specifically so the judge has a model family independent of the
panel). Without them, "Run scan" will find the trigger but fail to run
the debate, and will say so in its error message rather than failing
silently.

## Deployment

Vercel is the natural fit — this is what the project's stack was chosen
around (`next/font/google` needing real network access is itself a sign
this wants a real hosting environment, not a sandbox). Since this lives
in a monorepo alongside `backend/`, one non-default setting is needed:
when importing the project, set **Root Directory** to `frontend` in
Vercel's project configuration screen — without it, Vercel tries to
build the repo root, finds no `package.json` there, and fails. Then set
`NEXT_PUBLIC_API_BASE_URL` to the deployed backend's URL in Vercel's
environment variable settings, and set `FRONTEND_ORIGIN` in the backend
to the resulting `*.vercel.app` URL so CORS allows it.

## What's deliberately not here yet

Auth (the approver-id field is free text — matches the backend's
unenforced `approver_role` placeholder, not a real login), the debate
transcript view doesn't yet render citations inline against the graph,
and Layer 2 metrics have no UI since Layer 2 doesn't exist until v1.1.
None of these block a working v0 demo; all three are real next steps.
