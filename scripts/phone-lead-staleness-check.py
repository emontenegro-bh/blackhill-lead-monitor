#!/usr/bin/env python3
"""Phone-lead staleness alert (insurance).

Emails Evelin if the phone-lead monitor has not processed a new phone lead in
>= THRESHOLD_BUSINESS_DAYS. Decoupled from Microsoft Graph on purpose: it reads
the monitor's stored state rather than re-reading the workbook, so it still fires
when the monitor's Graph read, the Forms intake, or Power Automate is broken.
Weekends are skipped (no office phone calls Sat/Sun).

State lives in Supabase under the monitor's own key. Until 2026-09-30 this read
data/phone-lead-state.json from the repo instead, a path that stopped existing
when state moved to Supabase -- so every run printed "No phone-lead state file;
nothing to check", exited 0, and reported success. The alert was dead for months
and nobody could tell, because a no-op and a healthy run looked identical. That
is why an unreadable or empty state now alerts instead of returning quietly.

    python3 phone-lead-staleness-check.py            # send if stale (CI)
    python3 phone-lead-staleness-check.py --dry-run  # print only, no email
"""
import os, sys, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

THRESHOLD_BUSINESS_DAYS = 2
DRY = "--dry-run" in sys.argv
# The monitor's state, read-only here. Must match phone-lead-monitor.STATE_NAME.
MONITOR_STATE_NAME = "phone-lead-monitor"
# This script's own alert-dedupe state.
STATE_NAME = "phone-lead-staleness-check"
ALERT_TO = os.environ.get("ALERT_RECIPIENT", "evelin@blackhilltx.com")


def business_days_between(start_date, end_date):
    """Count Mon-Fri days strictly after start_date, up to and including end_date."""
    n = 0
    d = start_date
    while d < end_date:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0-4 = Mon-Fri
            n += 1
    return n


def latest_processed_at(state):
    ts = [v.get("processed_at") for v in state.get("processed", {}).values()
          if isinstance(v, dict) and v.get("processed_at")]
    return max(ts) if ts else None


def send_email(subject, html):
    user = os.environ.get("GMAIL_EMAIL", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        return False, "No Gmail SMTP credentials"
    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ALERT_TO
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as s:
            s.starttls()
            s.login(user, pw)
            s.sendmail(user, [ALERT_TO], msg.as_string())
        return True, "sent"
    except Exception as e:
        return False, str(e)[:200]


def main():
    state = db.load_state(MONITOR_STATE_NAME, default={})
    latest = latest_processed_at(state)
    today = datetime.now(timezone.utc).date()

    # An empty state is not "nothing to check" -- the monitor has been recording
    # leads for months, so a state with no processed entries means the state was
    # lost, renamed, or never written. Silence here is what hid a dead alert for
    # months, so this path shouts instead.
    if not latest:
        subject = "Phone Lead Monitor state is empty or unreadable"
        html = (f"<p>The staleness check read state <b>{MONITOR_STATE_NAME}</b> and found "
                f"no processed phone leads at all.</p>"
                f"<p>That is not the same as a quiet week. It means the monitor's state "
                f"is missing, was renamed, or is not being written. Phone leads may be "
                f"arriving and going nowhere.</p>"
                f"<p>Check the Phone Lead Monitor workflow runs and the "
                f"<b>automation_state</b> row named <b>{MONITOR_STATE_NAME}</b>.</p>")
        print("EMPTY STATE ->", subject)
        if DRY:
            print(f"[dry-run] would email {ALERT_TO}")
            return
        astate = db.load_state(STATE_NAME, default={})
        if astate.get("last_alert_date") == str(today) and astate.get("last_alert_for") == "__empty__":
            print("Already alerted today for empty state; skipping duplicate.")
            return
        ok, info = send_email(subject, html)
        print("email:", ok, info)
        if ok:
            db.save_state(STATE_NAME,
                          {"last_alert_for": "__empty__", "last_alert_date": str(today)})
        return

    last_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    bdays = business_days_between(last_dt.date(), today)
    print(f"Latest processed: {latest} | business days since: {bdays} | threshold: {THRESHOLD_BUSINESS_DAYS}")

    if bdays < THRESHOLD_BUSINESS_DAYS:
        print("Not stale — OK.")
        return

    # De-dupe: only one alert per calendar day per stale lead.
    astate = db.load_state(STATE_NAME, default={})
    if astate.get("last_alert_for") == latest and astate.get("last_alert_date") == str(today):
        print("Already alerted today for this lead; skipping duplicate.")
        return

    subject = f"Phone Lead Monitor quiet for {bdays} business days"
    html = (f"<p>The phone-lead monitor hasn't processed a new phone lead in "
            f"<b>{bdays} business days</b>.</p>"
            f"<p>Last lead processed: <b>{latest}</b>.</p>"
            f"<p>Worth a quick check that Carlos's Phone Lead Intake form is submitting, "
            f"and that Power Automate is still writing rows into "
            f"<b>Phone Lead Responses.xlsx</b>. (If there just haven't been any phone "
            f"calls, ignore this.)</p>")
    print("STALE ->", subject)
    if DRY:
        print(f"[dry-run] would email {ALERT_TO}")
        return
    ok, info = send_email(subject, html)
    print("email:", ok, info)
    if ok:
        db.save_state(STATE_NAME,
                      {"last_alert_for": latest, "last_alert_date": str(today)})


if __name__ == "__main__":
    with db.track("phone-lead-staleness-check"):
        main()
