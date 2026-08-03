-- Provenance for publicly-generated content (V2 Tab 1).
--
-- The existing values describe content whose origin is inherently
-- trusted to some degree: ingested from the company's own documents,
-- produced by its own debate, or seeded from a reference library.
-- Content decomposed from an anonymous public submission is none of
-- those, and collapsing it into one of them would erase exactly the
-- distinction a reviewer needs.
--
-- Idempotent: safe to re-run.

DO $$ BEGIN
    ALTER TYPE provenance_source ADD VALUE IF NOT EXISTS 'public_generated';
EXCEPTION WHEN others THEN NULL; END $$;

-- A decomposition proposal, from submission through to approval.
--
-- Proposals are stored rather than applied immediately: nothing
-- generated from untrusted public input enters the shared graph without
-- a human approving it, which is the same discipline the debate loop
-- applies to a different input source.
CREATE TABLE IF NOT EXISTS decompositions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submitter_key        TEXT,                  -- scope key, not identity
    problem              TEXT NOT NULL,
    feasible             BOOLEAN NOT NULL DEFAULT FALSE,
    reasoning            TEXT,
    change_set           JSONB NOT NULL DEFAULT '{"ops": []}',
    structural_problems  JSONB NOT NULL DEFAULT '[]',
    objections           JSONB NOT NULL DEFAULT '[]',
    suspected_manipulation BOOLEAN NOT NULL DEFAULT FALSE,
    input_flagged        BOOLEAN NOT NULL DEFAULT FALSE,
    status               TEXT NOT NULL DEFAULT 'proposed'
                         CHECK (status IN ('proposed', 'approved', 'rejected')),
    approver_id          TEXT,
    decided_at           TIMESTAMPTZ,
    applied_refs         JSONB,                 -- ref -> created node id
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decompositions_status
    ON decompositions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decompositions_submitter
    ON decompositions(submitter_key, created_at DESC);
