-- Debate / eval / approval loop (MVP plan, Sections 5, 7, 8, 9).
-- Idempotent: safe to re-run.

DO $$ BEGIN
    CREATE TYPE debate_state AS ENUM (
        'OPEN', 'IN_DEBATE', 'PENDING_EVAL', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Trigger records (Section 5). One row per detected bottleneck.
CREATE TABLE IF NOT EXISTS triggers (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    task_node_id   UUID NOT NULL REFERENCES task_nodes(id),
    rule_name      TEXT NOT NULL,          -- which threshold rule fired
    metric_name    TEXT NOT NULL,          -- 'error_rate' | 'cost' | 'cycle_time'
    observed_value NUMERIC NOT NULL,
    threshold      NUMERIC NOT NULL,
    sample_size    INTEGER NOT NULL,
    detail         JSONB NOT NULL DEFAULT '{}',
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    debate_id      UUID                    -- set once a debate is opened
);

CREATE TABLE IF NOT EXISTS debates (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    trigger_id    UUID NOT NULL REFERENCES triggers(id),
    state         debate_state NOT NULL DEFAULT 'OPEN',
    round_number  INTEGER NOT NULL DEFAULT 0,
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ,
    termination_reason TEXT                -- 'converged' | 'round_cap' | 'no_candidates'
);

-- Append-only transition log (Section 7.1). Never updated, only inserted.
CREATE TABLE IF NOT EXISTS debate_events (
    id          BIGSERIAL PRIMARY KEY,
    debate_id   UUID NOT NULL REFERENCES debates(id) ON DELETE CASCADE,
    from_state  debate_state,
    to_state    debate_state NOT NULL,
    reason      TEXT,
    actor       TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS debate_turns (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debate_id     UUID NOT NULL REFERENCES debates(id) ON DELETE CASCADE,
    round_number  INTEGER NOT NULL,
    speaker_id    TEXT NOT NULL,
    speaker_kind  TEXT NOT NULL CHECK (speaker_kind IN ('agent', 'human')),
    speaker_role  TEXT,                  -- unenforced placeholder (Section 12), human panelists only
    model_used    TEXT,                   -- recorded, not just intended (Section 7)
    action        TEXT NOT NULL CHECK (action IN ('propose', 'amend', 'pass')),
    candidate_id  UUID,
    content       TEXT NOT NULL,
    cites         JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS candidates (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debate_id     UUID NOT NULL REFERENCES debates(id) ON DELETE CASCADE,
    summary       TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    change_set    JSONB NOT NULL DEFAULT '[]',  -- machine-applicable ops (models/change.py)
    supporters    JSONB NOT NULL DEFAULT '[]',  -- speaker_ids who proposed or amended
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scorecards (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debate_id          UUID NOT NULL REFERENCES debates(id) ON DELETE CASCADE,
    candidate_id       UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    layer1_passed      BOOLEAN NOT NULL,
    fallacy_flags      JSONB NOT NULL DEFAULT '[]',
    constructive       BOOLEAN NOT NULL,
    groundedness_score NUMERIC NOT NULL,
    unresolved_cites   JSONB NOT NULL DEFAULT '[]',
    -- Layer 2 fields, unpopulated in v0 (Section 8.2). Present now so the
    -- schema doesn't need migrating when Layer 2 lands at v1.1.
    layer2_tier        INTEGER,
    layer2_metrics     JSONB,
    -- Metering data for the pricing model (Section 13). Populated by Layer 2.
    value_delivered    NUMERIC,
    blast_radius       INTEGER NOT NULL DEFAULT 0,
    reversible         BOOLEAN NOT NULL DEFAULT TRUE,
    recommendation     TEXT NOT NULL,     -- advisory only, never binding
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scorecard_id  UUID NOT NULL REFERENCES scorecards(id),
    candidate_id  UUID NOT NULL REFERENCES candidates(id),
    approver_id   TEXT NOT NULL,
    approver_role TEXT,                  -- unenforced placeholder (Section 12); RBAC lands at v1.4
    decision      TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    note          TEXT,
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at    TIMESTAMPTZ,
    applied_ops   JSONB                   -- what actually got written, for audit
);

CREATE INDEX IF NOT EXISTS idx_triggers_task    ON triggers(task_node_id);
CREATE INDEX IF NOT EXISTS idx_triggers_debate  ON triggers(debate_id);
CREATE INDEX IF NOT EXISTS idx_debates_state    ON debates(state);
CREATE INDEX IF NOT EXISTS idx_events_debate    ON debate_events(debate_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_turns_debate     ON debate_turns(debate_id, round_number);
CREATE INDEX IF NOT EXISTS idx_candidates_debate ON candidates(debate_id);
CREATE INDEX IF NOT EXISTS idx_scorecards_debate ON scorecards(debate_id);
