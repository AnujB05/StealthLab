-- Ontology schema (MVP plan, Section 3.1).
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
    CREATE TYPE provenance_source AS ENUM ('company_ingested', 'company_debate', 'prior_library');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE edge_type AS ENUM (
        'REQUIRES', 'PRODUCES', 'TRIGGERED_BY', 'SUPERSEDES',
        'VALIDATED_BY', 'OWNS', 'RESPONSIBLE_FOR'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Embedding dimension 1024 = voyage-3-large default output dimension.
-- voyage-3-large also supports 256/512/2048 via Matryoshka truncation;
-- changing this requires re-embedding the whole corpus, so decide once.

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    node_type   TEXT NOT NULL,
    name        TEXT NOT NULL,
    properties  JSONB NOT NULL DEFAULT '{}',
    embedding   VECTOR(1024),
    provenance  provenance_source NOT NULL DEFAULT 'company_ingested',
    t_valid     TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalid   TIMESTAMPTZ,
    t_created   TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_expired   TIMESTAMPTZ,
    created_by  TEXT
);

CREATE TABLE IF NOT EXISTS task_nodes (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    name                 TEXT NOT NULL,
    description          TEXT,
    io_schema            JSONB NOT NULL DEFAULT '{}',
    skill_ref            TEXT,
    success_criteria     JSONB NOT NULL DEFAULT '{}',
    cost_estimate        NUMERIC,
    latency_estimate_ms  INTEGER,
    -- CPM/PERT three-point estimates (Section 4 apparatus). Populated at
    -- onboarding as a duration prior before real timing data exists.
    pert_optimistic_ms   INTEGER,
    pert_likely_ms       INTEGER,
    pert_pessimistic_ms  INTEGER,
    embedding            VECTOR(1024),
    provenance           provenance_source NOT NULL DEFAULT 'company_ingested',
    t_valid              TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalid            TIMESTAMPTZ,
    t_created            TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_expired            TIMESTAMPTZ,
    created_by           TEXT
);

-- Polymorphic edges. No FK constraints are possible against two possible
-- parent tables; referential integrity is enforced in application code
-- (see services/knowledge_update.py). Deliberate tradeoff, not an oversight.
CREATE TABLE IF NOT EXISTS edges (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    edge_type         edge_type NOT NULL,
    custom_edge_type  TEXT,
    source_id         UUID NOT NULL,
    source_table      TEXT NOT NULL CHECK (source_table IN ('knowledge_nodes', 'task_nodes')),
    target_id         UUID NOT NULL,
    target_table      TEXT NOT NULL CHECK (target_table IN ('knowledge_nodes', 'task_nodes')),
    properties        JSONB NOT NULL DEFAULT '{}',
    provenance        provenance_source NOT NULL DEFAULT 'company_ingested',
    t_valid           TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalid         TIMESTAMPTZ,
    t_created         TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_expired         TIMESTAMPTZ,
    created_by        TEXT
);

-- Episodic layer: non-lossy raw log (Section 3.1).
CREATE TABLE IF NOT EXISTS episodes (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    episode_type  TEXT NOT NULL CHECK (episode_type IN ('document', 'trace', 'debate_transcript')),
    content       TEXT,
    content_ref   TEXT,
    timestamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata      JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS episode_links (
    episode_id    UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    target_id     UUID NOT NULL,
    target_table  TEXT NOT NULL CHECK (target_table IN ('knowledge_nodes', 'task_nodes', 'edges')),
    PRIMARY KEY (episode_id, target_id, target_table)
);

-- Execution traces (Section 6). Separate from episodes: episodes hold the
-- raw payload for audit, traces hold the queryable structured form that
-- trigger detection scans.
CREATE TABLE IF NOT EXISTS traces (
    trace_id         TEXT PRIMARY KEY,
    tenant_id        UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    timestamp        TIMESTAMPTZ NOT NULL,
    task_node_id     UUID NOT NULL REFERENCES task_nodes(id),
    actor_id         TEXT,
    action_type      TEXT NOT NULL CHECK (action_type IN ('invoke_agent', 'execute_tool', 'human_review')),
    outcome          TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'needs_rework')),
    cost             NUMERIC,
    latency_ms       INTEGER,
    parent_trace_id  TEXT,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW rather than ivfflat: ivfflat derives cluster centroids from existing
-- rows, so an index built on an empty table (which is exactly our situation
-- at bootstrap) has permanently degraded recall. HNSW has no training step.
CREATE INDEX IF NOT EXISTS idx_kn_tenant    ON knowledge_nodes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kn_validity  ON knowledge_nodes(t_valid, t_invalid);
CREATE INDEX IF NOT EXISTS idx_kn_embedding ON knowledge_nodes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_kn_fts       ON knowledge_nodes USING gin (to_tsvector('english', name));

CREATE INDEX IF NOT EXISTS idx_tn_tenant    ON task_nodes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tn_validity  ON task_nodes(t_valid, t_invalid);
CREATE INDEX IF NOT EXISTS idx_tn_embedding ON task_nodes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_tn_fts       ON task_nodes USING gin (to_tsvector('english', name || ' ' || COALESCE(description, '')));

CREATE INDEX IF NOT EXISTS idx_edges_source   ON edges(source_id, source_table);
CREATE INDEX IF NOT EXISTS idx_edges_target   ON edges(target_id, target_table);
CREATE INDEX IF NOT EXISTS idx_edges_validity ON edges(t_valid, t_invalid);
CREATE INDEX IF NOT EXISTS idx_edges_tenant   ON edges(tenant_id);

CREATE INDEX IF NOT EXISTS idx_episodes_tenant     ON episodes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_episode_links_target ON episode_links(target_id, target_table);

CREATE INDEX IF NOT EXISTS idx_traces_task    ON traces(task_node_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_traces_outcome ON traces(outcome);
