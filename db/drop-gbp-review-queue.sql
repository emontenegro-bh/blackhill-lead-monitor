-- Drop the GBP review reply queue (2026-08-20).
--
-- Evelin retired the review draft-and-approve automation: she does not use it.
-- Both producers are gone in the same commit that adds this file
-- (scripts/gbp-review-monitor.py, scripts/gbp-reply-poller.py), their two
-- workflows are deleted and disabled on GitHub, and db.py no longer carries
-- the gbp_queue_* helpers. Nothing reads or writes this table any more.
--
-- The table is dropped rather than left in place because it holds reviewer
-- names and full review text with no reader and no maintainer, and Google
-- Business Profile is the system of record for every row in it.
--
-- All 67 rows (60 pending, 7 responded) plus the gbp-review-monitor state
-- document were exported 2026-08-20 to
-- ~/.claude/archive/2026-08-20-gbp-review-system/data/, alongside the retired
-- scripts and workflows. That archive is outside this repo on purpose,
-- because this repo is public and the payloads carry reviewer names.
--
-- Note on the 60 pending rows: they were never a work queue in any live sense.
-- Five reviews answered on 2026-04-19 sat there because the old file queue
-- copied to responded/ without deleting the original, and the monitor had been
-- crashing on every new review since 2026-08-10. Do not treat them as an
-- outstanding to-do list; check the live GBP API for which reviews actually
-- lack a reply.
--
-- THIS IS DESTRUCTIVE AND CANNOT BE UNDONE FROM WITHIN SUPABASE.
-- Confirm the archive directory exists before running.

-- ---------------------------------------------------------------------------
-- 1. Last look before deleting. Should read 60 pending, 7 responded.
-- ---------------------------------------------------------------------------
SELECT status, count(*) FROM gbp_review_queue GROUP BY status ORDER BY status;

-- ---------------------------------------------------------------------------
-- 2. Drop it. No CASCADE: this table declares no foreign keys and nothing
--    references it, so a plain drop is enough and will fail loudly if that
--    assumption ever stopped being true.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS gbp_review_queue;

-- The monitor also kept a state document listing every review id it had ever
-- seen (67 of them). Its only reader was the script deleted alongside this.
DELETE FROM automation_state WHERE name = 'gbp-review-monitor';

-- ---------------------------------------------------------------------------
-- 3. Confirm what survives: the four remaining automation tables, all with row
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
