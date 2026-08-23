#!/usr/bin/env python3
"""Phone-lead staleness alert (insurance).

Emails Evelin if the phone-lead monitor has not processed a new phone lead in
>= THRESHOLD_BUSINESS_DAYS. Decoupled from Microsoft Graph on purpose: it reads
only the committed state file, so it still fires even if the monitor's Graph read
is failing. Weekends are skipped (no office phone calls Sat/Sun).

    python3 phone-lead-staleness-check.py            # send if stale (CI)
    python3 phone-lead-staleness-check.py --dry-run  # print only, no email
"""
import json, os, sys, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

THRESHOLD_BUSINESS_DAYS = 2
DRY = "--dry-run" in sys.argv
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(REPO_ROOT, "data", "phone-lead-state.json")
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
    if not os.path.exists(STATE_FILE):
        print("No phone-lead state file; nothing to check.")
        return
    state = json.load(open(STATE_FILE))
    latest = latest_processed_at(state)
    if not latest:
        print("No processed leads recorded; skipping.")
        return
    last_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    today = datetime.now(timezone.utc).date()
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
