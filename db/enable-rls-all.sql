-- Close the Supabase "Table publicly accessible" (rls_disabled_in_public) alert.
--
-- Context: the Supabase project pre-dated our Phase 0 use of it and still held
-- 7 tables from an abandoned Jan 2026 prototype, two of them carrying imported
-- business data. Our own five Phase 0 tables were created with RLS on from the
-- start (see schema.sql), so the alert was about the prototype tables, which
-- are dropped in drop-prototype-tables.sql.
--
-- Enabling RLS with no policies denies the anon key entirely. The automation
-- is unaffected: every script authenticates with service_role, which bypasses
-- RLS by design. Nothing on this machine reads the prototype tables.
--
-- Safe to re-run. Paste the whole file into the Supabase SQL editor.

-- ---------------------------------------------------------------------------
-- 1. Before: which tables are unprotected right now
-- ---------------------------------------------------------------------------
SELECT 'BEFORE' AS phase,
       tablename,
       rowsecurity AS rls_enabled
FROM   pg_tables
WHERE  schemaname = 'public'
ORDER  BY rowsecurity, tablename;

-- ---------------------------------------------------------------------------
-- 2. Enable RLS on every public table that lacks it
--
-- Written as a loop rather than a fixed list so a table created later, by a
-- prototype nobody remembers, gets covered too. That is exactly how these
-- seven got here.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t record;
    n int := 0;
BEGIN
    FOR t IN
        SELECT tablename
        FROM   pg_tables
        WHERE  schemaname = 'public'
        AND    NOT rowsecurity
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',
                       t.tablename);
        RAISE NOTICE 'RLS enabled on %', t.tablename;
        n := n + 1;
    END LOOP;

    RAISE NOTICE '% table(s) changed', n;
END $$;

-- ---------------------------------------------------------------------------
-- 3. After: everything should read true
-- ---------------------------------------------------------------------------
SELECT 'AFTER' AS phase,
       tablename,
       rowsecurity AS rls_enabled
FROM   pg_tables
WHERE  schemaname = 'public'
ORDER  BY rowsecurity, tablename;

-- ---------------------------------------------------------------------------
-- 4. Any policy that grants access back
--
-- RLS on with a permissive policy for anon is no safer than RLS off, and the
-- advisor does not flag it. This should return zero rows. If it does not,
-- read the qual column before assuming the table is protected.
-- ---------------------------------------------------------------------------
SELECT tablename, policyname, roles, cmd, qual
FROM   pg_policies
WHERE  schemaname = 'public'
ORDER  BY tablename, policyname;
