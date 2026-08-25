-- Phase 1A: the leads table
--
-- Apply by pasting into the Supabase SQL editor. Safe to re-run.
--
-- WHY THIS EXISTS
--
-- Every lead question today re-queries live APIs, so nothing keeps yesterday.
-- The 2026-08-22 drip analysis needed two agents and still could not answer
-- "what was Sam Shipley's Lead Source in June", because Aspire stores only the
-- current value and overwrites the old one. A field edited in August erases
-- what was true in June, and no amount of re-querying recovers it.
--
-- This table is the opposite: append-only, capture-time facts, never updated
-- in place. Rows are written once when a lead arrives and left alone.
--
-- RELATIONSHIP TO lead_mappings
--
-- lead_mappings (161 rows, 2026-02-19 onward) stays exactly as it is. It is a
-- working index the monitors use to answer "have I seen this WhatConverts lead
-- before", and rewriting it would mean touching four live scripts to gain
-- nothing. `leads` is the historical record; lead_mappings is a lookup. They
-- overlap and that is fine -- one is allowed to be lossy, the other is not.

-- ---------------------------------------------------------------------------
-- leads
--
-- One row per lead, from any source. Immutable after insert: if something
-- about the lead changes later, that belongs in lead_source_history below,
-- not in an UPDATE here. The whole value of this table is that a row means
-- "this is what was true when the lead arrived".
--
-- CONTAINS PII: names, emails, phone numbers. RLS is enabled at the bottom
-- with no policies, so only the service_role key can read it. This data must
-- never be written into the repo, which is public.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
    id                bigserial PRIMARY KEY,

    -- Identity. source_id is whatever that system calls its own record:
    -- WhatConverts lead_id, the SharePoint row id for Carlos's phone form,
    -- the Bookings appointment id.
    source_system     text NOT NULL
                      CHECK (source_system IN ('whatconverts', 'phone_form',
                                               'bookings', 'manual')),
    source_id         text NOT NULL,

    -- When the LEAD happened, not when we recorded it. Backfilled rows have a
    -- captured_at far older than their created_at, and that is correct.
    captured_at       timestamptz NOT NULL,

    -- Who.
    name              text,
    email             text,
    phone             text,

    -- How they arrived, as at capture. traffic_source is WhatConverts' own
    -- "source / medium" string, e.g. 'google / cpc'.
    lead_type         text,
    traffic_source    text,
    campaign          text,
    landing_page      text,

    -- What it became. Nullable: plenty of leads never become anything, and
    -- that absence is itself the answer to most attribution questions.
    aspire_contact_id text,

    -- Aspire's Lead Source (contact custom field 34) AS FIRST OBSERVED.
    -- Deliberately not kept current -- see lead_source_history.
    aspire_lead_source           text,
    aspire_lead_source_first_seen timestamptz,

    raw               jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),

    -- One row per lead per system. Makes the writers idempotent: a monitor
    -- re-processing the same lead upserts instead of duplicating, which
    -- matters because these scripts retry.
    UNIQUE (source_system, source_id)
);

CREATE INDEX IF NOT EXISTS leads_captured_idx     ON leads (captured_at DESC);
CREATE INDEX IF NOT EXISTS leads_source_idx       ON leads (traffic_source);
CREATE INDEX IF NOT EXISTS leads_aspire_idx       ON leads (aspire_contact_id)
    WHERE aspire_contact_id IS NOT NULL;
-- Email and phone are how a lead gets matched back to a customer later.
-- Lowercase the email on write; this index assumes it.
CREATE INDEX IF NOT EXISTS leads_email_idx        ON leads (email)
    WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS leads_phone_idx        ON leads (phone)
    WHERE phone IS NOT NULL;

COMMENT ON TABLE leads IS
    'Append-only record of every lead, with capture-time facts. Never UPDATE '
    'a row here; changes go in lead_source_history.';

-- ---------------------------------------------------------------------------
-- lead_source_history
--
-- The actual fix for "nothing keeps yesterday".
--
-- Aspire's Lead Source is a single mutable field. When someone corrects it in
-- August, June's value is gone -- so a question like "what did we believe
-- about this lead at the time" becomes unanswerable, and any historical
-- attribution report silently changes meaning depending on when it is run.
--
-- A row goes in here ONLY when the observed value differs from the last one
-- recorded. Polling daily and writing every time would add ~500 rows a day
-- that all say the same thing; writing only on change means the table stays
-- small and every row is a real event.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_source_history (
    id           bigserial PRIMARY KEY,
    lead_id      bigint NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    observed_at  timestamptz NOT NULL DEFAULT now(),
    old_value    text,
    new_value    text,
    note         text
);

CREATE INDEX IF NOT EXISTS lead_source_history_lead_idx
    ON lead_source_history (lead_id, observed_at DESC);

COMMENT ON TABLE lead_source_history IS
    'One row per CHANGE to a lead''s Aspire Lead Source. Not per observation.';

-- ---------------------------------------------------------------------------
-- Security. Same posture as every other table here: RLS on, no policies, so
-- only the service_role key reads it. This one matters more than most -- it is
-- the first table in this project to hold customer names, emails and phone
-- numbers.
-- ---------------------------------------------------------------------------
ALTER TABLE leads               ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_source_history ENABLE ROW LEVEL SECURITY;

-- Confirm after running: both should be true with 0 policies.
SELECT t.tablename, t.rowsecurity AS rls_enabled, count(p.policyname) AS policies
FROM   pg_tables t
LEFT   JOIN pg_policies p ON p.schemaname = t.schemaname AND p.tablename = t.tablename
WHERE  t.schemaname = 'public' AND t.tablename IN ('leads', 'lead_source_history')
GROUP  BY t.tablename, t.rowsecurity;
