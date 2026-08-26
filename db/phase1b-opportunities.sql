-- Phase 1B: opportunities
--
-- Paste into the Supabase SQL editor. Safe to re-run.
--
-- WHAT THIS ANSWERS THAT NOTHING ELSE CAN
--
-- Aspire holds the current state of an opportunity. Ask it "what was our win
-- rate in June" and you get today's answer applied to June's records: an
-- opportunity revised in August looks as though it was always that amount, and
-- one still open in June but won since counts as a June win. Every historical
-- revenue report therefore changes meaning depending on the day it runs.
--
-- Two tables. `opportunities` mirrors current state and is UPDATED on each
-- sync, because status genuinely changes and pretending otherwise would be a
-- lie. `opportunity_snapshots` is append-only and records what we saw and when,
-- so the past stays fixed even as the present moves.
--
-- That split is deliberate and different from `leads`, which is append-only
-- throughout. A lead is an event: it happened once, at a moment, and nothing
-- later changes what it was. An opportunity is a process that legitimately
-- moves through states.

-- ---------------------------------------------------------------------------
-- opportunities
--
-- Current state, one row per Aspire opportunity.
--
-- OpportunityType is the field that makes revenue comparisons honest.
-- 'Contract' rows carry ANNUAL value; one-time work does not. Summing them
-- together overstates any channel that happens to win a maintenance contract
-- -- which is how a single $30,759 annual agreement can look like a better
-- month than twelve real installs.
--
-- Aspire dates its own transitions (ProposedDate, WonDate, LostDate), so a
-- single snapshot yields the timeline. We do not have to catch changes as they
-- happen to know when they happened.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id       bigint PRIMARY KEY,          -- Aspire OpportunityID
    opportunity_number   integer,
    name                 text,

    property_id          bigint,
    property_name        text,
    billing_contact_id   text,

    status               text,        -- Won | Lost | Delivered | Open ...
    stage                text,
    opportunity_type     text,        -- Contract = ANNUAL value. Do not mix.
    sales_type           text,
    division             text,

    -- Money. estimated_dollars is the headline figure; won_dollars mirrors it
    -- on Won rows and is populated on Lost and Delivered rows too, so it is
    -- NOT revenue on its own -- always filter status first.
    estimated_dollars    numeric,
    won_dollars          numeric,
    actual_revenue       numeric,
    estimated_margin     numeric,
    actual_margin        numeric,

    -- The timeline, straight from Aspire.
    created_at_aspire    timestamptz,
    proposed_date        timestamptz,
    won_date             timestamptz,
    lost_date            timestamptz,
    complete_date        timestamptz,
    start_date           timestamptz,
    end_date             timestamptz,
    lost_reason          text,

    -- Aspire's own lead source on the opportunity, which is NOT the same field
    -- as the contact-level Lead Source (custom field 34) the rest of this
    -- project uses. Kept separately so the two can be compared rather than
    -- silently conflated.
    aspire_lead_source   text,

    sales_rep            text,
    raw                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS opportunities_status_idx   ON opportunities (status);
CREATE INDEX IF NOT EXISTS opportunities_property_idx ON opportunities (property_id);
CREATE INDEX IF NOT EXISTS opportunities_won_idx      ON opportunities (won_date DESC)
    WHERE won_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS opportunities_type_idx     ON opportunities (opportunity_type);

COMMENT ON TABLE opportunities IS
    'Current state of each Aspire opportunity, refreshed by sync. History of '
    'what changed lives in opportunity_snapshots.';

-- ---------------------------------------------------------------------------
-- opportunity_snapshots
--
-- Append-only. A row is written the first time an opportunity is seen, and
-- thereafter ONLY when its status or its amount has moved.
--
-- Change-only is what keeps this useful. Writing every opportunity on every
-- sync would add thousands of identical rows a week and bury the handful that
-- represent an actual event -- the same reasoning as lead_source_history.
--
-- This is the table that makes "what did we believe in June" answerable, and
-- it is also how a quietly revised estimate becomes visible instead of simply
-- becoming the new truth.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opportunity_snapshots (
    id                bigserial PRIMARY KEY,
    opportunity_id    bigint NOT NULL REFERENCES opportunities(opportunity_id)
                      ON DELETE CASCADE,
    observed_at       timestamptz NOT NULL DEFAULT now(),
    status            text,
    estimated_dollars numeric,
    prev_status       text,
    prev_dollars      numeric,
    note              text
);

CREATE INDEX IF NOT EXISTS opportunity_snapshots_opp_idx
    ON opportunity_snapshots (opportunity_id, observed_at DESC);

COMMENT ON TABLE opportunity_snapshots IS
    'One row per observed CHANGE in status or amount. Not per sync.';

-- ---------------------------------------------------------------------------
-- Security: RLS on, no policies, service_role only. These rows carry customer
-- property names and deal values.
-- ---------------------------------------------------------------------------
ALTER TABLE opportunities          ENABLE ROW LEVEL SECURITY;
ALTER TABLE opportunity_snapshots  ENABLE ROW LEVEL SECURITY;

SELECT t.tablename, t.rowsecurity AS rls_enabled, count(p.policyname) AS policies
FROM   pg_tables t
LEFT   JOIN pg_policies p ON p.schemaname = t.schemaname AND p.tablename = t.tablename
WHERE  t.schemaname = 'public'
  AND  t.tablename IN ('opportunities', 'opportunity_snapshots')
GROUP  BY t.tablename, t.rowsecurity;
