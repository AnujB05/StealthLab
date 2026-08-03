-- Rate limiting and LLM cost governance (V2).
--
-- Postgres-backed rather than in-memory: an in-memory counter resets on
-- restart and is per-process, so with two workers a "10 per hour" limit
-- silently becomes 20. Redis would be the eventual home (it arrives with
-- the job queue), but adding a second datastore for this alone is
-- premature, and Postgres is correct at current scale.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS rate_limit_events (
    id          BIGSERIAL PRIMARY KEY,
    scope_key   TEXT NOT NULL,          -- 'viewer:alice' | 'ip:1.2.3.4' | 'anon'
    endpoint    TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The hot query is "count events for this key+endpoint since T".
CREATE INDEX IF NOT EXISTS idx_rate_lookup
    ON rate_limit_events(scope_key, endpoint, occurred_at DESC);

-- Every LLM call, recorded after the fact.
--
-- `estimated_cost` is exactly that: derived from token counts and a
-- static price table, not from provider billing. It will drift from the
-- real invoice as prices change, so it is a guardrail against runaway
-- spend, not an accounting record.
CREATE TABLE IF NOT EXISTS llm_spend (
    id             BIGSERIAL PRIMARY KEY,
    scope_key      TEXT,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    operation      TEXT NOT NULL,       -- 'debate' | 'chat' | 'layer2' | 'embedding'
    estimated_cost NUMERIC NOT NULL DEFAULT 0,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_spend_window ON llm_spend(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_spend_scope  ON llm_spend(scope_key, occurred_at DESC);
