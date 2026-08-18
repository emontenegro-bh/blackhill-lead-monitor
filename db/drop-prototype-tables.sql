-- Drop the seven tables left behind by an abandoned prototype (2026-01-28).
--
-- These were not created by this repo's automation. They came from a
-- multi-tenant dashboard experiment that loaded two spreadsheets and was then
-- abandoned; the data has not changed since the day it was imported. Aspire is
-- the system of record for all of it, so this copy is a stale fork with no
-- reader: nothing in any Black Hill repo queries these tables.
--
-- They are dropped rather than merely locked down because a table that nobody
-- reads and nobody maintains is a liability that only grows. Row level
-- security was enabled on them 2026-08-18 to close a Supabase
-- rls_disabled_in_public alert; deleting them ends the exposure instead of
-- gating it.
--
-- A full backup (JSON + CSV, all seven tables) was taken 2026-08-18 to
-- ~/Desktop/supabase-prototype-backup-2026-08-18/ and row counts verified
-- against the live tables before this ran. That backup lives on the Desktop,
-- deliberately not in this repo, because this repo is public.
--
-- THIS IS DESTRUCTIVE AND CANNOT BE UNDONE FROM WITHIN SUPABASE.
-- Confirm the backup directory exists before running.

-- ---------------------------------------------------------------------------
-- 1. Last look before deleting: row counts should match the backup manifest
--    (contracts 0, insights 0, job_entries 612, properties 812, tenants 1,
--     uploads 2, users 1).
-- ---------------------------------------------------------------------------
SELECT 'contracts'   AS tbl, count(*) FROM contracts
UNION ALL SELECT 'insights',    count(*) FROM insights
UNION ALL SELECT 'job_entries', count(*) FROM job_entries
UNION ALL SELECT 'properties',  count(*) FROM properties
UNION ALL SELECT 'tenants',     count(*) FROM tenants
UNION ALL SELECT 'uploads',     count(*) FROM uploads
UNION ALL SELECT 'users',       count(*) FROM users
ORDER BY tbl;

-- ---------------------------------------------------------------------------
-- 2. Drop them.
--
-- One statement so the order does not matter: these tables reference each
-- other by tenant_id / property_id / contract_id, and dropping them
-- individually in the wrong order fails on the foreign keys.
--
-- CASCADE only reaches dependents of these seven. The five automation tables
-- declare no foreign keys at all, to these or to anything else, so they cannot
-- be caught by it.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS
    insights,
    job_entries,
    contracts,
    properties,
    uploads,
    users,
    tenants
CASCADE;

-- ---------------------------------------------------------------------------
-- 3. Confirm what survives: exactly the five automation tables, all with row
--    level security on and no policies.
-- ---------------------------------------------------------------------------
SELECT t.tablename,
       t.rowsecurity AS rls_enabled,
       count(p.policyname) AS policies
FROM   pg_tables t
LEFT   JOIN pg_policies p
       ON p.schemaname = t.schemaname AND p.tablename = t.tablename
WHERE  t.schemaname = 'public'
GROUP  BY t.tablename, t.rowsecurity
ORDER  BY t.tablename;
