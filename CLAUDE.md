# blackhill-lead-monitor

Everything in `~/projects/CLAUDE.md` applies here. This file holds only what is
true of THIS repo. If something belongs to both, it goes in the parent — the
previous version of this file duplicated 610 lines of it.

**This repo is PUBLIC.** No customer names, addresses, phone numbers or revenue
figures in code, commit messages, PR descriptions or committed reports.

---

## Shape of the automation

51 Python scripts in `scripts/`, 37 workflows in `.github/workflows/`. Both
counts drift — run `ls` for the current number. All recurring work runs in the
cloud and never depends on this laptop.

- Scripts are dual-credential: `~/.config/<service>/config.json` locally, env
  secrets in CI.
- Workflows are cron-scheduled at off-`:00` minutes (GitHub drops exactly-`:00`).
- State lives in **Supabase**, never in the repo. A lint (`lint-no-repo-state.yml`)
  blocks state writes into the tree — that rule exists because committed state
  put ~78 bot commits a day on main and caused a read-modify-write race that
  double-emailed customers.
- Every workflow needs a failure-notification job
  (`.github/workflows/notify-failure.yml`).
- Pushes race state-file bots, so commit steps use a `git pull --rebase` retry
  loop.

## GitHub's scheduler is not reliable here

`schedule:` crons are delivered best-effort and get dropped under load.
Measured 2026-08-24..29 on `lead-monitor.yml`: 98 `workflow_dispatch` runs
against 2 `schedule` runs in the same window.

So the real cadence for anything time-sensitive comes from **cron-job.org**,
an external service that POSTs to the GitHub API on a fixed interval. Seven
workflows currently depend on it:

| Workflow | Trigger it POSTs | Cadence |
|---|---|---|
| `phone-lead-monitor.yml` | `workflow_dispatch` | ~2 min |
| `lead-monitor.yml` (WhatConverts) | `workflow_dispatch` | 5 min |
| `email-lead-monitor.yml` (sales@ mailbox) | `workflow_dispatch` | 5 min |
| `crew-location.yml` | `repository_dispatch` | weekdays ~3:55pm CT |
| `dmarc-monitor.yml` | `workflow_dispatch` | every 2h |
| `ads-weekly-report.yml` | `workflow_dispatch` (backstop when the native Sunday cron drops) | Sundays |
| `bing-weekly-report.yml` | `workflow_dispatch` (same pattern) | Sundays |

Confirmed against live run history 2026-09-13, not just the `.yml` comments —
`email-lead-monitor.yml`'s own header comment currently claims it "has no such
dispatcher," which is stale; every recent run is `workflow_dispatch`. Comments
in these files lag reality; the run history does not.

**Always check the `event` field before believing a cadence** — a workflow's
cron line tells you what was asked for, not what ran it. `gh run list` also
undercounts; use `gh api .../runs` and read `total_count`.

## Run tracking

Scripts record to `automation_runs` via `db.track()`, or `db.track_flat()` for
flat top-level scripts with no `main()` to wrap (both defined in `scripts/db.py`).
`startup-failure-sweep.py` alerts when a script stops checking in — the failure
`notify-failure.yml` structurally cannot see, since it only fires when a workflow
runs and fails.

**Carrying the tracking call is not the same as writing rows.** `run_start()`
swallows a credentials failure on purpose, so a workflow missing
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` records nothing and says nothing —
seo-audit did exactly that for two weeks. If a script shows no runs, suspect its
workflow's `env:` before its code.

---

## Running scripts locally

```
pip install -r requirements.txt          # msal, google-ads
python3 scripts/<name>.py
GRPC_DNS_RESOLVER=native python3 scripts/ads-*.py   # required on this Mac
```

No build step, no test suite. Network-heavy scripts may need the sandbox
disabled.

## Google Ads

Customer `9637062915` holds both `BH_*` and legacy Mean Green campaigns. Mean
Green is Black Hill's own former brand — treat its search terms as own-brand,
never competitor. `umairmg3417@gmail.com` is the sanctioned web team and has
editor access; do not flag their changes as suspicious.

Read-only diagnostics: `ads-diagnostic.py`. Daily guard: `ads-daily-guard.py`.
Weekly report: `ads-weekly-report.py` (Fable 5 commentary, `--dry-run`
available).

## Microsoft Graph permission held back deliberately

Inbox rules (`/mailFolders/inbox/messageRules`) need the application
permission `MailboxSettings.ReadWrite` — a separate grant from the `Mail.*`
scopes the monitors use. Without it Graph returns `ErrorAccessDenied` (403).
**It is deliberately not granted and should stay that way.** As an
*application* permission it would let the service principal write inbox rules
— including forwarding rules — in every mailbox in the tenant, which is the
standard BEC exfiltration move and precisely what user-forwarding was blocked
for during the 2026-08-10 Secure Score work. `setup-dmarc-inbox-rule`
(workflow + script) existed only to create the DMARC filing rule this way,
never once succeeded, and was deleted 2026-08-22 (`e76e859f6`). The rule now
exists, made by hand in Outlook, and is verified filing reports on arrival. If
it ever needs rebuilding it is 30 seconds of clicking, which is not worth
tenant-wide mailbox-settings write. Recover the old script from git history if
you disagree.

Related and already fixed: `dmarc-monitor.py` used to re-read its own archive
daily because Graph's `$search` ignores the folder segment — fixed 2026-08-17
(`d53ed22a7`). See the parent `~/projects/CLAUDE.md` for the general Graph
`$search` gotcha.

---

**Stale sections removed 2026-09-13:** the Proposal Monitor rules (that
pipeline was deleted 2026-06-22/23 and no `*proposal*` script remains — only a
stale `.pyc` in `scripts/__pycache__/`) and the duplicated framework content
now inherited from `~/projects/CLAUDE.md`.
