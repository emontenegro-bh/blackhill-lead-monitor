#!/usr/bin/env python3
"""Crew Location Report — where are the field crews right now?

Uses Azuga's "Latest Location of Vehicles" endpoint to get every field crew's
LIVE position in a single call — including whether the truck is currently
moving, idling, or stopped — and pushes a summary to Microsoft Teams (via the
existing TEAMS_WEBHOOK_URL) and/or email.

Runs once a day (~4pm Central) from GitHub Actions so Evelin + Denisse have a
"where is everyone" snapshot, with moving trucks showing their real-time
location rather than their last completed stop.

Auth / config (first match wins):
    1. AZUGA_API_KEY env var          (used in CI)
    2. ~/.config/azuga/config.json    (used locally)

Delivery (independent, both optional):
    TEAMS_WEBHOOK_URL  -> POST {"text": ...} to the Teams webhook
    GMAIL_EMAIL + GMAIL_APP_PASSWORD -> email to CREW_LOC_TO
                                        (default: evelin + denisse)

Endpoint: POST /v3/vehicles/latestlocation on https://api.azuga.com/azuga-ws
(Basic auth with the API key). Returns one row per vehicle with live lat/lng,
address, speed, and tripStateId (0=Stopped, 1=Moving, 2=Idling, 3=Speeding).
"""

import base64
import json
import os
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
BASE_URL = "https://api.azuga.com/azuga-ws"
CONFIG_FILE = os.path.expanduser("~/.config/azuga/config.json")

# Field crews only (driver-assigned working trucks). Matched by Azuga trackeeName,
# and used as the display order.
FIELD_CREWS = ["Maint 1", "Maint 2", "Maint 3", "Maint 4", "Land 1", "Irrigation"]

KMH_TO_MPH = 0.621371

# tripStateId -> (icon, verb)
STATE = {
    0: ("📍", "At"),          # Stopped
    1: ("🚚", "Driving near"),  # Moving
    2: ("🟡", "Idling at"),    # Idling
    3: ("🚚", "Driving near"),  # Over speeding (treat as moving)
}


def get_api_key():
    key = os.environ.get("AZUGA_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)["api_key"]
    sys.exit("ERROR: No Azuga API key (set AZUGA_API_KEY or create ~/.config/azuga/config.json)")


def fetch_locations(api_key):
    """One call: live location of all field crews, keyed by trackeeName."""
    enc = base64.b64encode(api_key.encode()).decode()
    body = json.dumps({"trackeeNames": ",".join(FIELD_CREWS)}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v3/vehicles/latestlocation", data=body, method="POST",
        headers={"Authorization": f"Basic {enc}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Azuga HTTP {e.code}: {e.read().decode()[:300]}")
    return {v.get("trackeeName"): v for v in data.get("data", [])}


def clean_addr(addr):
    return (addr or "").replace(", USA", "").strip()


# Company shop / yard — show as "Shop" instead of the full street address.
# Azuga geocodes the yard as either Grants Ln or Corina Dr (both in White Settlement).
SHOP_STREET_HINTS = ("grants ln", "grants lane", "corina dr", "corina drive")


def label_addr(addr):
    a = clean_addr(addr)
    al = a.lower()
    if any(h in al for h in SHOP_STREET_HINTS) and "white settlement" in al:
        return "Shop"
    return a


def fmt_time(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, CENTRAL).strftime("%-I:%M %p")


def driver_name(v):
    name = f"{(v.get('firstName') or '').strip()} {(v.get('lastName') or '').strip()}".strip()
    return name or "unassigned"


def crew_line(name, v):
    if not v:
        return f"⚠️ **{name}**: No live GPS data"

    driver = driver_name(v)
    state_id = v.get("tripStateId", 0)
    icon, verb = STATE.get(state_id, STATE[0])

    # Prefer the live address; fall back to last-known if the device couldn't
    # fetch a fresh fix.
    stored = v.get("storedLocation")
    addr = clean_addr(v.get("address"))
    suffix = ""
    if stored or not addr:
        addr = clean_addr(v.get("lastKnownAddress") or v.get("realLKL_address") or v.get("address"))
        suffix = " (last known)"
    addr = label_addr(addr) or "location unavailable"

    line = f"{icon} **{name}** ({driver}): {verb} {addr}{suffix}"

    # Show speed only when actually moving.
    if state_id in (1, 3):
        mph = round((v.get("speed") or 0) * KMH_TO_MPH)
        if mph > 0:
            line += f" ({mph} mph)"
    return line


def build_message(locations):
    now = datetime.now(CENTRAL)
    lines = [f"**Crew Locations — {now.strftime('%a %b %-d, %-I:%M %p')} CT**", ""]
    for name in FIELD_CREWS:
        lines.append(crew_line(name, locations.get(name)))
    lines.append("")
    lines.append("_Live positions from Azuga GPS. 🚚 = driving, 🟡 = idling, 📍 = stopped._")
    return "\n".join(lines)


def post_to_teams(webhook_url, text):
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def send_email(text):
    sender = os.environ.get("GMAIL_EMAIL", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    # Empty/unset CREW_LOC_TO falls back to the default pair (the workflow sets
    # the env var to "" on scheduled runs, so treat blank as default).
    to_env = os.environ.get("CREW_LOC_TO", "").strip()
    default_to = "evelin@blackhilltx.com,denisse@blackhilltx.com"
    recipients = [a.strip() for a in (to_env or default_to).split(",") if a.strip()]
    if not (sender and pw and recipients):
        return False
    html_body = "<br>".join(line.replace("**", "") for line in text.split("\n"))
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Crew Locations — {datetime.now(CENTRAL).strftime('%a %b %-d')}"
    msg["From"] = formataddr(("Black Hill Assistant", sender))
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text.replace("**", ""), "plain"))
    msg.attach(MIMEText(f"<div style='font-family:sans-serif'>{html_body}</div>", "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(sender, pw)
        s.sendmail(sender, recipients, msg.as_string())
    return True


def main():
    api_key = get_api_key()
    locations = fetch_locations(api_key)

    text = build_message(locations)
    print(text)
    print()

    delivered = []
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            status = post_to_teams(webhook, text)
            delivered.append(f"Teams (HTTP {status})")
        except Exception as e:
            print(f"WARN: Teams post failed: {e}", file=sys.stderr)

    # Email by default unless explicitly disabled.
    if os.environ.get("CREW_LOC_EMAIL", "1") != "0":
        try:
            if send_email(text):
                delivered.append("email")
        except Exception as e:
            print(f"WARN: email failed: {e}", file=sys.stderr)

    if not delivered:
        print("WARN: no delivery channel configured (set TEAMS_WEBHOOK_URL "
              "and/or GMAIL_EMAIL + GMAIL_APP_PASSWORD)", file=sys.stderr)
    else:
        print(f"Delivered via: {', '.join(delivered)}")


if __name__ == "__main__":
    main()
