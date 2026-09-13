#!/usr/bin/env python3
"""Repair calendar entries the events monitor already put on Evelin's calendar.

The monitor delivers events as emailed iCalendar invitations, never through a
calendar-write API. That is a one-way door only if you forget the UID: a
METHOD:REQUEST carrying a UID Outlook has already seen, with a higher SEQUENCE,
UPDATES that entry in place, and a METHOD:CANCEL on the same UID removes it.
So every entry the monitor created it can also correct or withdraw.

`event_key` is deterministic - sha1 of org|date|title - so a UID can be
recomputed from what is visible on the calendar entry itself, which is how the
existing mess is addressable at all. Verified against a real sent invite before
this script was written: org "Unlisted (info.bisnow.com)", 2026-10-06, "Texas
halted data center grid connections..." recomputes to 2f0fdd06224dc967, matching
the UID in the .ics the monitor mailed on 2026-09-07.

Reads a JSON manifest (see MANIFEST_DEFAULT) and sends one email per entry, each
carrying exactly ONE calendar part - the same one-invite-per-message rule that
events-monitor.py follows, and for the same reason: a mail client renders at most
one invitation per message and silently demotes the rest to file attachments.

    python3 events-fixup.py --dry-run          # print what would be sent
    python3 events-fixup.py                    # send

Needs GMAIL_EMAIL / GMAIL_APP_PASSWORD, which live only in CI. Run it with
`gh workflow run events-fixup.yml`, or locally with the password in the env.
"""

import argparse
import importlib.util
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_DEFAULT = os.path.join(SCRIPT_DIR, "events-fixup-manifest.json")

# events-monitor.py is not importable by name (the hyphen), and copying its UID
# maths here would let the two drift apart - at which point a cancel silently
# addresses an event that does not exist. Load the real module instead.
_spec = importlib.util.spec_from_file_location(
    "events_monitor", os.path.join(SCRIPT_DIR, "events-monitor.py"))
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)

RECIPIENTS = em.RECIPIENTS
LOCAL_TZ = em.LOCAL_TZ


def esc(s):
    return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def uid_for(entry):
    """Explicit uid wins; otherwise recompute it from org|date|title exactly as
    the monitor did when it first sent the invite."""
    if entry.get("uid"):
        return entry["uid"]
    b = entry["uid_basis"]
    return f"{em.event_key(b)}@blackhilltx.com"


def stamps(entry):
    """DTSTART/DTEND lines.

    A cancel must carry the ORIGINAL start, verbatim, including the floating
    local time the old invites wrongly used - the point is to match the entry
    sitting on the calendar, not to correct it. A reissue carries the corrected
    start, stamped UTC.
    """
    if entry.get("dtstart_raw"):
        return f"DTSTART:{entry['dtstart_raw']}", f"DTEND:{entry['dtend_raw']}"
    if entry.get("all_day"):
        d = datetime.fromisoformat(entry["date_iso"])
        return (f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}")
    start = datetime.fromisoformat(entry["start"]).replace(tzinfo=LOCAL_TZ)
    end = datetime.fromisoformat(entry["end"]).replace(tzinfo=LOCAL_TZ)
    return (f"DTSTART:{start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S')}Z",
            f"DTEND:{end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S')}Z")


def build_ics(entry, organizer):
    cancel = entry["action"] == "cancel"
    dt_start, dt_end = stamps(entry)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Black Hill//Events Monitor//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:CANCEL" if cancel else "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid_for(entry)}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        # A client ignores an update whose SEQUENCE is not higher than the one it
        # already holds. The originals all went out at SEQUENCE:0.
        f"SEQUENCE:{entry.get('sequence', 1)}",
        "STATUS:CANCELLED" if cancel else "STATUS:CONFIRMED",
        dt_start, dt_end,
        f"ORGANIZER;CN=Black Hill Events Monitor:mailto:{organizer}",
    ]
    lines += [f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
              f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{r}" for r in RECIPIENTS]
    lines.append(f"SUMMARY:{esc(entry['summary'])}")
    if not cancel:
        lines += [
            f"LOCATION:{esc(entry.get('location') or 'See registration link')}",
            f"DESCRIPTION:{esc(entry.get('description', ''))}",
            f"CATEGORIES:{entry.get('categories', 'Networking')}",
            "TRANSP:TRANSPARENT", "X-MICROSOFT-CDO-BUSYSTATUS:FREE",
        ]
        if entry.get("alarm_trigger"):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"DESCRIPTION:{esc(entry['summary'])}",
                      entry["alarm_trigger"], "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines)


def body_for(entry):
    if entry["action"] == "cancel":
        head = "Removing a calendar entry the events monitor should never have created."
    else:
        head = "Correcting a calendar entry the events monitor got wrong."
    parts = [head, "", entry["summary"], ""]
    if entry.get("start"):
        parts.append(datetime.fromisoformat(entry["start"]).strftime("%A, %B %-d at %-I:%M %p"))
    if entry.get("location"):
        parts.append(entry["location"])
    if entry.get("why"):
        parts += ["", f"What was wrong: {entry['why']}"]
    if entry.get("description"):
        parts += ["", entry["description"]]
    parts += ["", "Black Hill Events Monitor"]
    return "\n".join(parts)


def send(entry, organizer, pw):
    subject = (f"Cancelled: {entry['summary']}" if entry["action"] == "cancel"
               else f"Updated: {entry['summary']}")
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject[:180]
    msg["From"] = formataddr(("Black Hill Events Monitor", organizer))
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(body_for(entry), "plain"))

    method = "CANCEL" if entry["action"] == "cancel" else "REQUEST"
    part = MIMEBase("text", "calendar", method=method, name="event.ics")
    part.set_payload(build_ics(entry, organizer))
    encoders.encode_base64(part)
    name = re.sub(r"[^A-Za-z0-9]+", "-", entry["summary"]).strip("-")[:50] or "event"
    part.add_header("Content-Disposition", "attachment", filename=f"{name}.ics")
    msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(organizer, pw)
        s.sendmail(organizer, RECIPIENTS, msg.as_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=MANIFEST_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.manifest) as f:
        entries = json.load(f)["entries"]

    organizer = os.environ.get("GMAIL_EMAIL", "noreply@blackhilltx.com").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if args.dry_run:
        for e in entries:
            print(f"\n=== {e['action'].upper()}  uid={uid_for(e)}")
            print(build_ics(e, organizer))
        print(f"\n[dry-run] {len(entries)} message(s) would be sent to "
              f"{', '.join(RECIPIENTS)}")
        return 0

    if not pw:
        print("GMAIL_APP_PASSWORD not set; nothing sent.", file=sys.stderr)
        return 1

    for e in entries:
        send(e, organizer, pw)
        print(f"{e['action']}: {e['summary'][:70]}  (uid {uid_for(e)})")
    print(f"Sent {len(entries)} fixup message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
