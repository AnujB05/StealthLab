-- V2 access control (public commons, with private mode designed in).
--
-- The decision: V2 launches as a fully shared commons — every node
-- visible to everyone — with per-node private visibility available
-- later without a migration.
--
-- The important part is NOT that these columns exist. It's that every
-- query is written against them from day one, currently permissive.
-- V0's `tenant_id` is the cautionary case: the column existed on every
-- table from the first schema and *no query ever filtered by it*, so
-- "we have multi-tenancy" was decorative. Flipping that on later meant
-- auditing every query in the codebase. Here, flipping private mode on
-- is a change to one predicate builder, because the predicate is
-- already in every query path.
--
-- Idempotent: safe to re-run.

DO $$ BEGIN
    CREATE TYPE visibility_level AS ENUM ('public', 'private');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- owner_id is TEXT rather than a FK to a users table: V2's identity
-- subsystem doesn't exist yet (Tier 3), and a FK to a table that hasn't
-- been designed would either block this work or force a throwaway schema.
-- TEXT holds whatever identity scheme lands later without a migration.

ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS visibility visibility_level NOT NULL DEFAULT 'public',
    ADD COLUMN IF NOT EXISTS owner_id TEXT;

ALTER TABLE task_nodes
    ADD COLUMN IF NOT EXISTS visibility visibility_level NOT NULL DEFAULT 'public',
    ADD COLUMN IF NOT EXISTS owner_id TEXT;

ALTER TABLE edges
    ADD COLUMN IF NOT EXISTS visibility visibility_level NOT NULL DEFAULT 'public',
    ADD COLUMN IF NOT EXISTS owner_id TEXT;

-- Debates and their artifacts inherit visibility from the work they
-- concern. A private problem's deliberation must not be public.
ALTER TABLE debates
    ADD COLUMN IF NOT EXISTS visibility visibility_level NOT NULL DEFAULT 'public',
    ADD COLUMN IF NOT EXISTS owner_id TEXT;

-- Partial indexes: the overwhelmingly common query is "public content",
-- so index that path specifically rather than the whole column.
CREATE INDEX IF NOT EXISTS idx_kn_public ON knowledge_nodes(id)
    WHERE visibility = 'public' AND t_invalid IS NULL;
CREATE INDEX IF NOT EXISTS idx_tn_public ON task_nodes(id)
    WHERE visibility = 'public' AND t_invalid IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_public ON edges(id)
    WHERE visibility = 'public' AND t_invalid IS NULL;

-- Owner lookups matter once private content exists.
CREATE INDEX IF NOT EXISTS idx_kn_owner ON knowledge_nodes(owner_id)
    WHERE owner_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tn_owner ON task_nodes(owner_id)
    WHERE owner_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_debates_owner ON debates(owner_id)
    WHERE owner_id IS NOT NULL;
