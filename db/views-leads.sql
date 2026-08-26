-- Saved views over the leads data.
--
-- Paste into the Supabase SQL editor once. After that they appear in the
-- Table Editor sidebar like any other table -- click one and it is already
-- summarised, no SQL to write.
--
-- Safe to re-run: each is CREATE OR REPLACE.
--
-- A view stores no data. It is a saved question, re-answered against the live
-- table every time you open it, so these never go stale and never need a
-- refresh.

-- ---------------------------------------------------------------------------
-- v_leads_by_channel
--
-- Where leads come from, and how much of each channel we can actually trace.
--
-- `linked_pct` is the honest column. It is not a conversion rate -- it is the
-- share of that channel's leads we can follow into Aspire at all. A channel
-- with low linkage is under-measured, not under-performing, and the two look
-- identical if you only read the lead counts.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_leads_by_channel AS
SELECT
    traffic_source,
    lead_type,
    count(*)                                                  AS leads,
    count(aspire_contact_id)                                  AS linked,
    round(100.0 * count(aspire_contact_id) / count(*))        AS linked_pct,
    min(captured_at)::date                                    AS first_lead,
    max(captured_at)::date                                    AS latest_lead
FROM leads
GROUP BY traffic_source, lead_type
ORDER BY leads DESC;

-- ---------------------------------------------------------------------------
-- v_leads_monthly
--
-- Volume over time, split by phone and web.
--
-- Read the earliest month with care: tracking was installed part-way through
-- February 2026, so that month is short rather than quiet.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_leads_monthly AS
SELECT
    to_char(captured_at, 'YYYY-MM')                                   AS month,
    count(*)                                                          AS leads,
    count(*) FILTER (WHERE lead_type = 'Phone Call')                  AS phone,
    count(*) FILTER (WHERE lead_type = 'Web Form')                    AS web,
    count(aspire_contact_id)                                          AS linked,
    round(100.0 * count(aspire_contact_id) / count(*))                AS linked_pct
FROM leads
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- v_lead_source_current
--
-- The current Aspire Lead Source for each lead, AND what it was first seen as.
--
-- This is the view that makes the history design visible. leads.aspire_lead_source
-- is deliberately frozen at first observation; every later change lands in
-- lead_source_history. So "current" is the newest history row if one exists,
-- otherwise the baseline -- and `changed` tells you whether anyone has edited
-- it since we started watching.
--
-- Before this existed, an edit in August silently rewrote what June meant.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_lead_source_current AS
SELECT
    l.id                                       AS lead_id,
    l.captured_at::date                        AS captured,
    l.lead_type,
    l.traffic_source                           AS whatconverts_source,
    l.aspire_contact_id,
    l.aspire_lead_source                       AS first_seen_as,
    COALESCE(h.new_value, l.aspire_lead_source) AS current_value,
    (h.id IS NOT NULL)                         AS changed,
    h.observed_at                              AS changed_at
FROM leads l
LEFT JOIN LATERAL (
    SELECT id, new_value, observed_at
    FROM lead_source_history
    WHERE lead_id = l.id
    ORDER BY observed_at DESC, id DESC
    LIMIT 1
) h ON true
WHERE l.aspire_contact_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- v_untraceable_leads
--
-- The gap, as a working list rather than a percentage.
--
-- Leads we captured but cannot follow into Aspire. Every row is a real enquiry
-- whose outcome is unknown, so this is the ceiling on how good any attribution
-- answer can get. Phone leads dominate it, because a call only becomes an
-- Aspire contact when someone chooses to make one.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_untraceable_leads AS
SELECT
    captured_at::date  AS captured,
    lead_type,
    traffic_source,
    phone,
    email
FROM leads
WHERE aspire_contact_id IS NULL
ORDER BY captured_at DESC;

-- Views inherit the RLS of the tables beneath them, so these stay readable
-- only by the service_role key. Confirm what was created:
SELECT table_name
FROM information_schema.views
WHERE table_schema = 'public'
ORDER BY table_name;
