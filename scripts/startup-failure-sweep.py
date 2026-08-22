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
