"""One-time backfill: load the git-tracked JSON state files into Supabase.

MUST run before any script is switched over to db.py. If a migrated script
starts against an empty database it sees zero processed ids, treats already
handled mail as new, and sends a second auto-reply to customers who already
got one. This script is what prevents that.

The interesting work is splitting data/processed-state.json. That one file was
shared by four scripts in four different concurrency groups, two of them on the
same */5 cron, each doing read-modify-write on the whole document. It becomes:

    processed_keys                     the 822 ids, per script
    lead_mappings (table)              the 142 shared lead rows
    automation_state/lead-monitor      email script's own fields
    automation_state/whatconverts-...  WC script's own fields
    automation_state/lead-fuzzy-match  fuzzy matcher's own fields

Safe to re-run: documents are upserted, keys are inserted ignore-duplicates.

    python3 scripts/db_migrate_state.py --dry-run
    python3 scripts/db_migrate_state.py
    python3 scripts/db_migrate_state.py --verify

Phase 0 of docs/architecture/data-platform-plan.md.

HISTORICAL as of 2026-08-09: the migration is complete and the source
files data/processed-state.json and data/roi-sync-state.json have been
deleted. Kept for the record and because the remaining file-based
scripts (GBP pollers, bid-monitor, phone-lead-monitor and friends) reuse
this same shape when their turn comes. Sources that no longer exist are
skipped, so a re-run is harmless but will cover less than it once did.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SHARED_STATE = "data/processed-state.json"

# Scripts that read processed-state.json's processed_ids list.
#
# Every id is marked for BOTH lead scripts rather than classified by format.
# Over-marking is free: a key a script never looks up just sits there. Under-
# marking sends a duplicate auto-reply to a real customer. The asymmetry is the
# whole argument. (For reference the split is 420 numeric WhatConverts ids and
# 402 Outlook ids prefixed AAMk/AQMk, but we do not rely on that holding.)
ID_OWNERS = ["lead-monitor", "whatconverts-lead-monitor"]

# Which fields of the shared document belong to which script, based on who
# actually writes them. Anything with one writer is safe inside that script's
# own document; lead_mappings had several writers, so it became a table.
SHARED_SPLIT = {
    "lead-monitor": [],
    "whatconverts-lead-monitor": ["round_robin_state", "pending_call_attributions"],
    "lead-fuzzy-match": ["dismissed_matches"],
}

# Scripts that keep a processed_ids list inside their own document, and so need
# the full history copied in. Getting this wrong is the expensive mistake: a
# script that starts with an empty list treats every recent lead as new and
# sends a second auto-reply to customers who already got one.
NEEDS_PROCESSED_IDS = ["lead-monitor", "whatconverts-lead-monitor"]

# Files with a single owning script carry over unchanged.
SIMPLE_SOURCES = [
    ("data/roi-sync-state.json",         "whatconverts-roi-sync", ("synced_leads", "dict")),
    ("data/phone-lead-state.json",       "phone-lead-monitor",    ("processed", "dict")),
    ("data/bid-monitor-state.json",      "bid-monitor",           ("seen", "dict")),
    ("data/aspire-mailchimp-state.json", "aspire-mailchimp-sync", None),
    ("data/post-launch-checkins.json",   "post-launch-checkins",  None),
    (".claude/states/ads-guard-state.json", "ads-daily-guard",    None),
]


def _load(rel_path):
    path = os.path.join(REPO, rel_path)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _extract_keys(doc, key_source):
    if not key_source:
        return []
    field, kind = key_source
    value = doc.get(field)
    if kind == "list":
        return [str(k) for k in (value or [])]
    return [str(k) for k in (value or {})]


def plan():
    """Work out everything to write. Returns (documents, key_writes, mappings)."""
    documents = []   # (name, doc, source)
    key_writes = []  # (script, keys, source)
    mappings = {}

    shared = _load(SHARED_STATE)
    if shared:
        ids = [str(i) for i in shared.get("processed_ids", [])]
        mappings = shared.get("lead_mappings", {}) or {}

        for script, fields in SHARED_SPLIT.items():
            doc = {k: shared[k] for k in fields if k in shared}
            if script in NEEDS_PROCESSED_IDS:
                # Both lead scripts get the whole list rather than the subset
                # matching their id format. Over-inclusion costs nothing (a
                # script never looks up an id it does not produce); under-
                # inclusion re-notifies real customers.
                doc["processed_ids"] = list(ids)
            # Counters restart per script. The old totals were a single set of
            # numbers incremented by two scripts at once, so they cannot be
            # attributed; keeping them for reference beats inventing a split.
            doc["stats"] = {"total_leads": 0, "total_spam": 0, "total_calls": 0}
            doc["pre_migration_stats"] = shared.get("stats", {})
            documents.append((script, doc, f"{SHARED_STATE} (split)"))

        for script in ID_OWNERS:
            key_writes.append((script, ids, SHARED_STATE))

    for rel_path, name, key_source in SIMPLE_SOURCES:
        doc = _load(rel_path)
        if doc is None:
            continue
        documents.append((name, doc, rel_path))
        keys = _extract_keys(doc, key_source)
        if keys:
            key_writes.append((name, keys, rel_path))

    return documents, key_writes, mappings


def main():
    dry = "--dry-run" in sys.argv
    verify = "--verify" in sys.argv

    print(f"{'DRY RUN - ' if dry else ''}state backfill from {REPO}\n")
    documents, key_writes, mappings = plan()

    print("  state documents")
    for name, doc, source in documents:
        fields = ", ".join(sorted(k for k in doc if k != "pre_migration_stats")) or "(empty)"
        print(f"      {name:28} {fields}")
        print(f"      {'':28} <- {source}")

    print("\n  processed keys")
    for script, keys, source in key_writes:
        print(f"      {script:28} {len(keys):5} keys  <- {source}")

    print(f"\n  lead_mappings table          {len(mappings):5} rows")

    if dry:
        print("\nDry run only. Nothing written.")
        return 0
    if verify:
        return _verify(documents, key_writes, mappings)

    if not db.is_configured():
        print("\nFAILED: no Supabase credentials. See db.py for setup.")
        return 1

    print("\nWriting...")
    for name, doc, _ in documents:
        db.save_state(name, doc)
        print(f"  state      {name}")
    for script, keys, _ in key_writes:
        db.mark_processed(script, keys, note="backfilled from git")
        print(f"  keys       {script} ({len(keys)})")
    if mappings:
        db.save_lead_mappings(mappings)
        print(f"  mappings   {len(mappings)} rows")

    print("\nBackfill complete. Run --verify before migrating scripts.")
    return 0


def _verify(documents, key_writes, mappings):
    if not db.is_configured():
        print("\nFAILED: no Supabase credentials.")
        return 1

    print("\nVerifying...")
    problems = []

    for name, doc, _ in documents:
        stored = db.load_state(name)
        ok = stored == doc
        if not ok:
            problems.append(f"{name}: stored document differs")
        print(f"  {'OK' if ok else 'MISMATCH':9} state    {name}")

    # Explicit guard. A migrated lead script that loads an empty processed_ids
    # re-sends auto-replies to real customers, and it fails silently: the run
    # succeeds and simply reports "0 processed". Check the count directly
    # rather than relying on the whole-document comparison above.
    planned = {nm for nm, _, _ in documents}
    for name in NEEDS_PROCESSED_IDS:
        # Only meaningful while the source file still exists. Once a script is
        # cut over the file is deleted, so there is nothing to compare against
        # and the database is authoritative. Without this the check reports
        # "expected 0" forever and screams DANGER at a perfectly healthy
        # migration, which is worse than not checking: an alarm that always
        # fires gets ignored on the day it is real.
        if name not in planned:
            n = len(db.load_state(name).get("processed_ids") or [])
            print(f"  {'OK':9} ids      {name} ({n} processed ids, already cut over)")
            continue
        stored = db.load_state(name)
        n = len(stored.get("processed_ids") or [])
        expected = next((len(d.get("processed_ids") or [])
                         for nm, d, _ in documents if nm == name), 0)
        ok = n == expected and n > 0
        if not ok:
            problems.append(
                f"{name}: processed_ids has {n} entries, expected {expected} "
                "- migrating now would re-notify customers"
            )
        print(f"  {'OK' if ok else 'DANGER':9} ids      {name} ({n} processed ids)")

    for script, keys, _ in key_writes:
        missing = db.filter_unprocessed(script, keys)
        if missing:
            problems.append(f"{script}: {len(missing)} of {len(keys)} keys missing")
        print(f"  {'OK' if not missing else 'MISSING':9} keys     {script} ({len(keys)})")

    if mappings:
        stored = db.load_lead_mappings()
        missing = set(map(str, mappings)) - set(stored)
        if missing:
            problems.append(f"lead_mappings: {len(missing)} of {len(mappings)} rows missing")
        print(f"  {'OK' if not missing else 'MISSING':9} mappings ({len(stored)} rows in table)")

    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nAll documents, keys and mappings verified. Safe to migrate scripts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
