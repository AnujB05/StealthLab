-- Generated files pending download (agent-run outputs).
--
-- Downloads go through an opaque server-generated id, never a client-
-- supplied filename in a path. A "GET /files/{name}" style endpoint
-- built from user input is a textbook path-traversal vulnerability;
-- this table is what makes that unnecessary.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS generated_files (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    disk_path      TEXT NOT NULL,
    display_name   TEXT NOT NULL,      -- what the browser shows/saves as
    content_type   TEXT NOT NULL DEFAULT 'application/octet-stream',
    scope_key      TEXT,               -- who generated it, for cleanup/ownership
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    downloaded_at  TIMESTAMPTZ         -- set on first download, informational only
);

CREATE INDEX IF NOT EXISTS idx_generated_files_created
    ON generated_files(created_at DESC);
