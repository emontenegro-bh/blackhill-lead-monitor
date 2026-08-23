#!/usr/bin/env python3
"""Catch GitHub Actions runs that die BEFORE any job starts.

THE BLIND SPOT THIS CLOSES
    Every workflow here ends with a `notify-failure` job gated on `if: failure()`.
    That job is itself a job -- so when a run's conclusion is `startup_failure`
    (GitHub never parsed/queued the workflow, zero jobs created), the notifier
    never runs and nothing emails. The only signal is GitHub's own subscription
    email to whoever is watching the repo. That is how the 2026-07-25 12:20-12:35
    UTC Actions outage went unalerted across two repos.

    This sweep runs on a schedule and reports startup failures after the fact.
    It is deliberately RETROSPECTIVE: during an Actions outage the sweep itself
    may fail to start, but the failed runs stay in the API, so the next healthy
    sweep still finds and reports them.

AUTH
    lead-monitor is public, so its runs are readable with no token at all.
    The other repos are private and need `WATCHER_PAT` -- a fine-grained PAT with
    read-only Actions + Metadata on them. Without it this still sweeps the public
    repos and says plainly which ones it could not check, rather than pretending
    the fleet is clean. Locally it falls back to `gh auth token`.

DEDUPE
    GitHub's scheduler can run this hours late, so the lookback window is wide
    (LOOKBACK_HOURS) and already-reported run IDs are remembered in Supabase
    (automation_state, 'startup-failure-sweep'). State is only written when
    something new is found, so the usual quiet sweep writes nothing at all.
"""
import json
import os
import smtplib
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

TO_EMAIL = "evelin@blackhilltx.com"
OWNER = "emontenegro-bh"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
STATE_NAME = "startup-failure-sweep"

# Wide enough that GitHub scheduler lag (observed 1.5-2.5h on these repos)
# cannot slide a failure out of the window before the sweep looks.
LOOKBACK_HOURS = 8
KEEP_DAYS = 7            # prune remembered run IDs older than this
MAX_PAGES = 5            # backstop; a window this wide never holds 500 failures

# ---------------------------------------------------------------------------
# Liveness: which scripts must check in, and how long silence is allowed.
#
# The GitHub sweep above answers "did a run fail to start". This answers a
# question GitHub cannot: "did a run simply never happen". notify-failure.yml
# only fires when a workflow runs and fails, so a dropped cron, a
# startup_failure, or a workflow auto-disabled for repo inactivity is
# invisible to it. That is how the 2026-07-25 outage passed unnoticed.
#
# Written out rather than parsed from the cron on purpose. A derived threshold
# that derives wrong disables an alarm silently; a wrong number here shows up
# in a diff. Day-of-week crons are the trap: events-monitor runs Mon and Thu,
# so its real worst case is the Thursday-to-Monday gap, not 24h.
#
# Thresholds are deliberately loose. This repo's own sweep docstring records
# GitHub scheduler lag of 1.5-2.5h, so a tight bound would alarm on lateness
# rather than absence, and an alert that cries wolf is worse than none.
EXPECTED_CADENCE_HOURS = {
    # Both of these are cron */5, and they do NOT behave the same. Measured
    # 2026-08-22 over ~10h: whatconverts-lead-monitor held a 5-minute median
    # with a 5-minute max, while lead-monitor ran a 26-minute median and a
    # 43-minute max. GitHub throttles scheduled workflows under load and does
    # not say so. 3h leaves ~4x headroom over the worst gap actually seen;
    # revisit once a full week of automation_runs exists, because ten hours
    # is not enough to have seen the real tail.
    "lead-monitor": 3,
    "whatconverts-lead-monitor": 2,
    # cron at :15/:45 and :17/:47 -- twice hourly.
    "whatconverts-roi-sync": 3,
    "phone-lead-monitor": 3,
    "startup-failure-sweep": 3,
    # cron 23 */2 -- every two hours.
    "dmarc-monitor": 6,
    # cron 0 0,6,12,18 -- four times daily.
    "api-health-monitor": 14,
    # daily.
    "lead-fuzzy-match": 30,
    "aspire-mailchimp-backfill": 30,
    "bid-monitor": 30,
    # cron 13 12 * * 1,4 -- Monday and Thursday. Thu -> Mon is 96h.
    "events-monitor": 100,
    # Weekdays ~20:55Z. Counted in BUSINESS hours (see WEEKDAY_ONLY), so this
    # 26 catches one missed weekday report rather than waiting out the 72-hour
    # Friday-to-Monday gap a wall-clock threshold would have to tolerate.
    "crew-location": 26,
}

# Scripts that only run Mon-Fri. Their silence is measured in business hours,
# so the weekend does not count against them and a single missed weekday still
# alerts the next day.
WEEKDAY_ONLY = {"crew-location"}

# Scripts wrapped in db.track() but NOT expected on a schedule go here, so the
# liveness check stays silent about them instead of reporting a permanent
# overdue. Empty today; kept as the documented place to put one.
ON_DEMAND_SCRIPTS = set()

# KNOWN BLIND SPOTS -- scheduled work this check cannot see, because the script
# is not wrapped in db.track(). Listed rather than silently omitted, so the
# heartbeat's "all clear" is honest about its own coverage. Audited 2026-08-22.
#
#   phone-lead-monitor --reconcile  daily 13:07 -- the reconcile branch sits
#                                 outside the db.track() wrapper, and a check
#                                 keyed on the script name is satisfied by the
#                                 30-minute monitor runs even if reconcile has
#                                 stopped entirely.
#   ads-daily-guard.py            daily 12:07 -- flat script, no main().
#   phone-lead-staleness-check.py weekdays 13:23 -- itself an alerter, so its
#                                 silence is doubly invisible.
#   gbp-scheduled-poster.py       Mon/Wed/Fri 16:00
#   aspire-material-audit.py      Mon 13:00 + 14:00
#   seo-health-weekly.py          Mon 13:30
#   seo-audit.py                  Mon 14:00 -- concluded `cancelled` on 6 of
#                                 its last 12 scheduled runs. `cancelled` does
#                                 not trigger if: failure(), so those silent
#                                 no-ops never notified anyone.
#   ads-weekly-report.py          Sun 15:07
#   qs-recheck.py                 annual, Apr 1 -- not worth a threshold.
UNTRACKED_SCHEDULED = 9
#
# crew-location.py was the tenth and the worst of them, fixed 2026-08-23: it is
# now tracked, checked in business hours, and carries a late backstop schedule.
# It stays the template for the rest -- an external trigger with no safety net
# fails completely silently, because no workflow ever starts and if: failure()
# has nothing to fire on.

HEARTBEAT_DAYS = 7

# `public` repos need no token; `private` ones are skipped (loudly) without one.
REPOS = [
    ("blackhill-lead-monitor", "public"),
    ("blackhill-ap-automation", "private"),
    ("blackhill-time-tracker", "private"),
    ("blackhill-ops-dashboard", "private"),
]


def _token():
    """PAT for private repos. Env in CI, `gh auth token` locally."""
    for var in ("WATCHER_PAT", "GH_PAT"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _api(url, token):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bh-startup-failure-sweep",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def load_state():
    # No try/except. The file version returned an empty reported-map on any
    # read error, which silently disarms the dedupe: every startup failure
    # still inside the 8-hour window gets re-reported as if it were new. That
    # turns a storage problem into a stream of duplicate alarms, which is the
    # fastest way to teach someone to ignore this mailbox. db.load_state()
    # raises instead, and notify-failure.yml reports it.
    return db.load_state(STATE_NAME, default={"reported": {}})


def save_state(state):
    db.save_state(STATE_NAME, state)


def sweep_repo(repo, since_iso, token):
    """Startup failures in `repo` since `since_iso`. Raises on API failure.

    Filters with `status=startup_failure` SERVER-side. Do not switch this to
    fetching recent runs and filtering in Python: lead-monitor runs every 5 min,
    so one 100-run page covers only ~8h and silently drops anything older --
    that bug hid all four of the 2026-07-25 failures during testing.
    """
    out, page = [], 1
    while page <= MAX_PAGES:
        url = (f"https://api.github.com/repos/{OWNER}/{repo}/actions/runs"
               f"?status=startup_failure&created=%3E%3D{since_iso}"
               f"&per_page=100&page={page}")
        runs = _api(url, token).get("workflow_runs", []) or []
        out.extend({
            "repo": repo,
            "id": r["id"],
            "name": r.get("name") or r.get("display_title") or "(unnamed)",
            "created_at": r.get("created_at"),
            "event": r.get("event"),
            "url": r.get("html_url"),
        } for r in runs)
        if len(runs) < 100:
            break
        page += 1
    else:
        print(f"  {repo}: NOTE hit the {MAX_PAGES}-page cap; "
              f"reporting the first {len(out)}")
    return out


def send_html(subject, html):
    """Send one HTML mail. Returns True if it went out."""
    sender = os.environ.get("GMAIL_EMAIL")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not (sender and password):
        print("WARNING: Gmail creds unset; skipping email.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Assistant", sender))
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(sender, password)
        s.sendmail(sender, [TO_EMAIL], msg.as_string())
    return True


def business_hours_between(start, end):
    """Hours between two instants, ignoring Saturday and Sunday.

    Exists so a weekday-only job can use one honest threshold. Measured in wall
    time, crew-location's Friday-to-Monday gap is 72h while its Monday-to-
    Tuesday gap is 24h, so a flat threshold has to be set above 72 -- and then
    a report that dies on Tuesday goes unreported until Friday. Counting only
    weekdays makes both gaps 24h, so 26h catches a single missed weekday
    without ever firing over a weekend.
    """
    if end <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur < end:
        midnight = (cur + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        chunk_end = min(end, midnight)
        if cur.weekday() < 5:          # Mon-Fri
            total += (chunk_end - cur).total_seconds() / 3600
        cur = chunk_end
    return total


def check_liveness(now):
    """Scripts that should have checked in by now and have not.

    Returns (overdue, seen_count). `overdue` entries carry the script, hours
    of silence, and its allowance, so the email can show the margin rather
    than just asserting lateness.
    """
    watched = [s for s in EXPECTED_CADENCE_HOURS if s not in ON_DEMAND_SCRIPTS]
    latest = db.latest_run_times(watched)

    # How long has the table been collecting at all? A script with no rows is
    # indistinguishable from one whose wrapper shipped after its last run, and
    # on the day tracking went live that describes every daily and weekly
    # script here. Alerting on "never" before the table is older than the
    # script's own cadence would fire a guaranteed false alarm on deploy day,
    # which is the fastest way to teach someone to ignore this email.
    #
    # Measured per script in the SAME units as that script's threshold. Mixing
    # them is not hypothetical: comparing 27 wall hours against crew-location's
    # 26-business-hour allowance un-suppressed it on a Sunday, when it had not
    # yet had a single working day in which to run.
    table_start = db.table_started_at()

    overdue = []
    for script in watched:
        allowed = EXPECTED_CADENCE_HOURS[script]
        stamp = latest.get(script)
        if stamp is None:
            if table_start is not None:
                age = (business_hours_between(table_start, now)
                       if script in WEEKDAY_ONLY
                       else (now - table_start).total_seconds() / 3600)
                unit = " business h" if script in WEEKDAY_ONLY else "h"
                if age < allowed:
                    print(f"  {script}: no rows yet, but tracking is only "
                          f"{age:.1f}{unit} old vs {allowed}h cadence "
                          f"-- not yet a fault")
                    continue
            overdue.append({"script": script, "silent_h": None, "allowed_h": allowed})
            continue
        last = datetime.fromisoformat(stamp)
        if script in WEEKDAY_ONLY:
            silent = business_hours_between(last, now)
        else:
            silent = (now - last).total_seconds() / 3600
        if silent > allowed:
            overdue.append({"script": script, "silent_h": silent,
                            "allowed_h": allowed,
                            "weekday_only": script in WEEKDAY_ONLY})
    return overdue, len(latest)


def send_liveness_alert(overdue):
    rows = ""
    for o in sorted(overdue, key=lambda x: -(x["silent_h"] or 1e9)):
        silent = "never recorded a run" if o["silent_h"] is None \
            else f'{o["silent_h"]:.1f}h ago'
        rows += (f'<tr><td><code>{o["script"]}</code></td><td>{silent}</td>'
                 f'<td>{o["allowed_h"]}h</td></tr>')
    n = len(overdue)
    html = f"""<h3>{n} script{"s" if n != 1 else ""} did not check in</h3>
    <p>These wrote no row to <code>automation_runs</code> inside their expected
    window. This is the failure <code>notify-failure.yml</code> cannot see: it
    only fires when a workflow <i>runs and fails</i>, so a dropped cron, a
    <code>startup_failure</code>, or a workflow auto-disabled for repo
    inactivity produces no alert at all.</p>
    <table border="1" cellpadding="6" cellspacing="0">
    <tr><th>Script</th><th>Last check-in</th><th>Allowed silence</th></tr>
    {rows}</table>
    <p>Check the workflow is still <b>active</b> first
    (<code>gh api repos/emontenegro-bh/blackhill-lead-monitor/actions/workflows</code>)
    -- GitHub disables scheduled workflows in repos with no recent commits, and
    it does so quietly.</p>"""
    return send_html(f"[ALERT] {n} script(s) stopped checking in", html)


def maybe_send_heartbeat(state, now):
    """Weekly proof of life. Mutates and returns True if sent.

    A silent monitor and a dead monitor look identical, which is the whole
    problem automation_runs was built to fix -- one level up. Without this,
    the absence of alerts cannot be distinguished from the absence of a sweep.
    """
    last = state.get("last_heartbeat")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).days < HEARTBEAT_DAYS:
                return False
        except ValueError:
            pass  # unparseable stamp: send, and overwrite it with a good one

    since = (now - timedelta(days=HEARTBEAT_DAYS)).isoformat()
    tally = db.run_counts_since(since)
    watched = [s for s in EXPECTED_CADENCE_HOURS if s not in ON_DEMAND_SCRIPTS]
    latest = db.latest_run_times(watched)

    rows = ""
    for script in sorted(watched):
        t = tally.get(script, {})
        ok, err = t.get("ok", 0), t.get("error", 0)
        stamp = latest.get(script)
        age = "never" if not stamp else \
            f'{(now - datetime.fromisoformat(stamp)).total_seconds()/3600:.1f}h ago'
        flag = "" if stamp and ok else ' style="background:#fde8e8"'
        rows += (f'<tr{flag}><td><code>{script}</code></td><td>{ok}</td>'
                 f'<td>{err}</td><td>{age}</td></tr>')

    total = sum(v.get("ok", 0) + v.get("error", 0) for v in tally.values())
    errs = sum(v.get("error", 0) for v in tally.values())
    html = f"""<h3>Automation heartbeat, last {HEARTBEAT_DAYS} days</h3>
    <p><b>{total:,} runs recorded, {errs} error{"s" if errs != 1 else ""}</b>
    across {len(tally)} scripts.</p>
    <table border="1" cellpadding="6" cellspacing="0">
    <tr><th>Script</th><th>OK</th><th>Errors</th><th>Last run</th></tr>
    {rows}</table>
    <p>This email exists so silence means something. Every other alert here is
    silent-unless-broken, which cannot distinguish "nothing is wrong" from
    "the thing that checks is itself dead". If a week passes with no
    heartbeat, the monitoring stopped, not the problems.</p>
    <p><b>Coverage:</b> {len(watched)} scripts are checked here.
    {UNTRACKED_SCHEDULED} other scheduled scripts are <i>not</i> -- they do not
    write to <code>automation_runs</code>, so this all-clear says nothing about
    them. See UNTRACKED_SCHEDULED in <code>startup-failure-sweep.py</code> for
    the list. Of those still uncovered, <code>phone-lead-staleness-check.py</code>
    is the one to do next: it is itself an alerter, so its silence is doubly
    invisible.</p>"""

    if send_html(f"Automation heartbeat: {total:,} runs, {errs} errors", html):
        state["last_heartbeat"] = now.isoformat()
        return True
    return False


def send_email(found, unchecked):
    sender = os.environ.get("GMAIL_EMAIL")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not (sender and password):
        print("WARNING: Gmail creds unset; skipping email.")
        return False

    by_repo = {}
    for f in found:
        by_repo.setdefault(f["repo"], []).append(f)

    rows = ""
    for repo in sorted(by_repo):
        for f in sorted(by_repo[repo], key=lambda x: x["created_at"]):
            rows += (f'<tr><td>{repo}</td><td>{f["name"]}</td>'
                     f'<td>{f["created_at"]}</td><td>{f["event"]}</td>'
                     f'<td><a href="{f["url"]}">run {f["id"]}</a></td></tr>')

    note = ""
    if unchecked:
        note = ("<p><b>Not checked</b> (no WATCHER_PAT): "
                + ", ".join(unchecked) + "</p>")

    n = len(found)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f'[ALERT] {n} workflow run{"s" if n != 1 else ""} '
                      f"failed at startup (no jobs ran)")
    msg["From"] = formataddr(("Black Hill Assistant", sender))
    msg["To"] = TO_EMAIL
    body = f"""<h3>Runs that failed before any job started</h3>
    <p>These runs got conclusion <b>startup_failure</b>, meaning GitHub never
    queued any job. Their workflow's own <code>notify-failure</code> job could
    not fire, so this sweep is the only alert for them.</p>
    <table border="1" cellpadding="6" cellspacing="0">
    <tr><th>Repo</th><th>Workflow</th><th>Created (UTC)</th><th>Trigger</th>
    <th>Run</th></tr>{rows}</table>
    {note}
    <p>Several repos failing inside one short window almost always means a
    GitHub Actions incident, not a bug here. A single repo failing repeatedly
    points at that workflow's YAML or a bad dispatch.</p>"""
    plain = "\n".join(f'{f["repo"]} {f["name"]} {f["created_at"]} {f["url"]}'
                      for f in found)
    msg.attach(MIMEText(f"Startup failures:\n{plain}", "plain"))
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(sender, password)
        s.sendmail(sender, TO_EMAIL, msg.as_string())
    print(f"Alert sent for {n} startup failure(s).")
    return True


def main():
    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    token = _token()
    if not token:
        print("NOTE: no WATCHER_PAT/gh token; private repos will be skipped.")

    state = load_state()
    reported = state.get("reported", {})

    # --- Liveness and heartbeat -------------------------------------------
    # Run before the GitHub sweep, because that sweep has early returns and a
    # sys.exit() path. Putting these after them means a GitHub API outage also
    # silences the check that watches for scripts going quiet, which is the
    # one failure the two are least likely to share a cause with.
    dirty = False
    try:
        overdue, checked_in = check_liveness(now)
        print(f"Liveness: {checked_in}/{len(EXPECTED_CADENCE_HOURS)} scripts "
              f"checked in, {len(overdue)} overdue.")

        # Re-alert at most daily per script. Overdue is a standing condition,
        # not an event: this sweep runs twice an hour, so alerting on every
        # pass would send 48 identical emails a day about one dead cron and
        # guarantee the next real one gets filtered.
        alerted = state.get("liveness_alerted", {})
        fresh = []
        for o in overdue:
            prev = alerted.get(o["script"])
            if prev:
                try:
                    if (now - datetime.fromisoformat(prev)).total_seconds() < 86400:
                        continue
                except ValueError:
                    pass
            fresh.append(o)

        if fresh and send_liveness_alert(fresh):
            for o in fresh:
                alerted[o["script"]] = now.isoformat()
            dirty = True
        # Clear the flag once a script starts reporting again, so its next
        # outage alerts immediately instead of waiting out a stale 24h window.
        still = {o["script"] for o in overdue}
        for name in [k for k in alerted if k not in still]:
            del alerted[name]
            dirty = True
        state["liveness_alerted"] = alerted

        if maybe_send_heartbeat(state, now):
            print("Heartbeat sent.")
            dirty = True
    except Exception as e:
        # Never let the liveness extras take down the startup-failure sweep,
        # which is the older and more load-bearing job of the two.
        print(f"WARNING: liveness/heartbeat step failed: {e}")

    if dirty:
        save_state(state)

    found, unchecked, errors = [], [], []
    for repo, visibility in REPOS:
        if visibility == "private" and not token:
            unchecked.append(repo)
            print(f"  {repo}: SKIPPED (private, no token)")
            continue
        try:
            hits = sweep_repo(repo, since_iso, token)
        except Exception as e:
            # Flag-and-continue: one unreadable repo must not hide the others.
            detail = f"{type(e).__name__}: {e}"
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404):
                detail += " (token lacks access to this repo?)"
            errors.append(f"{repo}: {detail}")
            unchecked.append(repo)
            print(f"  {repo}: ERROR {detail}")
            continue
        new = [h for h in hits if str(h["id"]) not in reported]
        print(f"  {repo}: {len(hits)} startup failure(s) in window, "
              f"{len(new)} new")
        found.extend(new)

    if errors and len(errors) == len(REPOS):
        # Nothing could be checked at all -- fail loudly rather than report "clean".
        sys.exit("ERROR: every repo failed to sweep:\n  " + "\n  ".join(errors))

    if not found:
        print(f"No new startup failures since {since_iso}.")
        return

    if send_email(found, unchecked):
        stamp = now.isoformat()
        for f in found:
            reported[str(f["id"])] = stamp
        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
        state["reported"] = {k: v for k, v in reported.items() if v >= cutoff}
        state["last_alert"] = stamp
        save_state(state)
        print(f"State updated ({len(state['reported'])} ids remembered).")


if __name__ == "__main__":
    with db.track("startup-failure-sweep"):
        main()
