-- Black Hill automation database - Phase 0 schema
--
-- Apply once by pasting into the Supabase SQL editor
-- (Dashboard -> SQL Editor -> New query -> Run).
-- Safe to re-run: every statement is IF NOT EXISTS / idempotent.
--
-- Phase 0 scope is deliberately narrow: move the JSON state blobs out of git
-- and start recording run history. The business tables (leads, ads_daily,
-- job_time) land in Phase 1/2 once this plumbing is proven.
--
-- See docs/architecture/data-platform-plan.md in the framework repo.

-- ---------------------------------------------------------------------------
-- automation_state
--
-- Drop-in replacement for data/*.json. One row per state file, whole document
-- in a jsonb column. This is intentionally a straight swap of WHERE the state
-- lives, not a restructure of WHAT it holds, so the 9 migrating scripts keep
-- their existing load_state()/save_state() shape and the change stays small.
-- Phase 1 pulls the real facts out into proper columns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automation_state (
    name       text PRIMARY KEY,
    state      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE automation_state IS
    'One row per legacy JSON state file. Replaces data/*.json in git.';

-- ---------------------------------------------------------------------------
-- automation_runs
--
-- One row per script execution. Gives us the failure history that
-- notify-failure.yml structurally cannot see: it never fires on
-- startup_failure, which is why the 2026-07-25 GitHub outage passed silently.
-- A run that starts and never gets a finished_at is itself the signal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automation_runs (
    id                bigserial PRIMARY KEY,
    script            text NOT NULL,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'ok', 'error')),
    records_processed integer,
    error_text        text,
    run_env           text  -- 'cloud' (GitHub Actions) or 'local'
);

CREATE INDEX IF NOT EXISTS automation_runs_script_started_idx
    ON automation_runs (script, started_at DESC);

-- Find hung or crashed runs: started, never finished.
CREATE INDEX IF NOT EXISTS automation_runs_unfinished_idx
    ON automation_runs (started_at DESC)
    WHERE finished_at IS NULL;

COMMENT ON TABLE automation_runs IS
    'One row per script execution. Unfinished rows = crashed or hung runs.';

-- ---------------------------------------------------------------------------
-- processed_keys
--
-- Replaces the ever-growing processed_ids / seen / processed arrays.
-- This is the table that kills the ~8,900 "Update processed state" commits a
-- month: dedup becomes an indexed lookup instead of rewriting a 96 KB file.
--
-- Note the composite primary key. Two scripts may legitimately process the
-- same upstream id, so keys are only unique within a script.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processed_keys (
    script       text NOT NULL,
    key          text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    note         text,
    PRIMARY KEY (script, key)
);

CREATE INDEX IF NOT EXISTS processed_keys_script_time_idx
    ON processed_keys (script, processed_at DESC);

-- 'lead' | 'spam' | 'call' | NULL. Not used for counters (per-script state
-- documents already give each script a single writer). This is here for the
-- Phase 1 leads table, which needs to know what each processed id turned into.
ALTER TABLE processed_keys ADD COLUMN IF NOT EXISTS kind text;

CREATE INDEX IF NOT EXISTS processed_keys_kind_idx
    ON processed_keys (script, kind, processed_at DESC)
    WHERE kind IS NOT NULL;

COMMENT ON TABLE processed_keys IS
    'Idempotency ledger. Replaces processed_ids arrays in the JSON state files.';

-- ---------------------------------------------------------------------------
-- lead_mappings
--
-- This is the table that fixes the actual production bug.
--
-- lead_mappings used to be a field inside the shared processed-state.json.
-- whatconverts-roi-sync.py loaded the whole document, replaced that one field,
-- and wrote the entire document back. Anything lead-monitor.py or
-- whatconverts-lead-monitor.py had written in the meantime was erased, and
-- both of those run on the same */5 cron in separate concurrency groups.
-- Erased ids look unprocessed on the next run, which means a second auto-reply
-- goes to a customer who already got one.
--
-- As its own table, each mapping is an independent row. Concurrent writers
-- touch different rows and cannot clobber each other.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_mappings (
    wc_lead_id        text PRIMARY KEY,
    aspire_contact_id text,
    opp_number        integer,
    lead_type         text,
    service           text,
    traffic_source    text,
    lead_date         text,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    extra             jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS lead_mappings_updated_idx
    ON lead_mappings (updated_at DESC);

CREATE INDEX IF NOT EXISTS lead_mappings_source_idx
    ON lead_mappings (traffic_source);

COMMENT ON TABLE lead_mappings IS
    'Shared lead index, one row per WhatConverts lead. Was a field inside the '
    'shared state document, where whole-document writes clobbered concurrent updates.';

-- ---------------------------------------------------------------------------
-- Security
--
-- Every table is written by server-side scripts using the service_role key,
-- which bypasses row level security. We still enable RLS and add no policies,
-- so the public anon key cannot read these tables even if it leaks.
-- Do not add a permissive policy without a specific reason.
-- ---------------------------------------------------------------------------
ALTER TABLE automation_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation_runs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE processed_keys   ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_mappings    ENABLE ROW LEVEL SECURITY;
