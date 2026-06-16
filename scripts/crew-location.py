#!/usr/bin/env python3
"""Crew Location Report — where are the field crews right now?

Pulls today's Azuga GPS trips for each field crew, derives each truck's
current / last-known location, and pushes a summary to Microsoft Teams
(via Power Automate inbound webhook) and/or email.

Designed to run once a day (~2:40pm Central) from GitHub Actions so Evelin
has a "where is everyone" snapshot before 3pm.

Auth / config (first match wins):
    1. AZUGA_API_KEY env var          (used in CI)
    2. ~/.config/azuga/config.json    (used locally)

Delivery (independent, both optional):
    TEAMS_WEBHOOK_URL  -> POST {"text": ...} to the Power Automate flow
    GMAIL_EMAIL + GMAIL_APP_PASSWORD -> email to CREW_LOC_TO (default evelin@blackhilltx.com)

Azuga rate limit is 1 request/minute, so this sleeps 65s between vehicle
calls. With 6 field crews + 1 roster call the run takes ~7 minutes.
"""

import base64
import json
import os
import smtplib
import sys
import time
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

# Field crews only (driver-assigned working trucks). Matched by Azuga trackeeName.
FIELD_CREWS = ["Maint 1", "Maint 2", "Maint 3", "Maint 4", "Land 1", "Irrigation"]

# Seconds to wait between Azuga calls (rate limit is 1/min). Override for local
# testing against a cached/mock layer if ever needed.
RATE_LIMIT_SLEEP = int(os.environ.get("AZUGA_SLEEP", "65"))


def get_api_key():
    key = os.environ.get("AZUGA_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)["api_key"]
    sys.exit("ERROR: No Azuga API key (set AZUGA_API_KEY or create ~/.config/azuga/config.json)")


def azuga_get(api_key, path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    enc = base64.b64encode(api_key.encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {enc}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("Azuga rate limit hit (429) — increase AZUGA_SLEEP")
        raise RuntimeError(f"Azuga HTTP {e.code}: {e.read().decode()[:300]}")


def today_bounds_ms():
    """UTC epoch-ms bounds for the current Central-time calendar day."""
    now = datetime.now(CENTRAL)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from_ms = int(midnight.timestamp() * 1000)
    return from_ms, from_ms + 86_400_000


def fmt_time(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, CENTRAL).strftime("%-I:%M %p")


def human_gap(minutes):
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def clean_addr(addr):
    return (addr or "").replace(", USA", "").strip() or "unknown location"


def resolve_vehicle_ids(api_key):
    """Map field-crew names -> vehicleId + driver, from the live roster."""
    data = azuga_get(api_key, "/v3/vehicle")
    trackees = data.get("data", {}).get("trackees", {})
    by_name = {}
    for vid, v in trackees.items():
        name = (v.get("trackeeName") or "").strip()
        by_name[name.lower()] = {
            "vehicle_id": vid,
            "name": name,
            "driver": (v.get("fullName") or "").strip() or "(unassigned)",
        }
    out = []
    for crew in FIELD_CREWS:
        info = by_name.get(crew.lower())
        if info:
            out.append(info)
        else:
            out.append({"vehicle_id": None, "name": crew, "driver": "(not found)"})
    return out


def crew_status(api_key, crew, now_ms):
    """Return a status dict for one crew from today's trips."""
    if not crew["vehicle_id"]:
        return {**crew, "state": "no_vehicle", "summary": "Vehicle not found in Azuga roster"}

    from_ms, to_ms = today_bounds_ms()
    data = azuga_get(api_key, "/v3/trip", {
        "vehicleId": crew["vehicle_id"],
        "fromDate": from_ms,
        "toDate": to_ms,
    })
    trips = data.get("data", []) or []
    if not trips:
        return {**crew, "state": "idle",
                "summary": "No GPS movement today (truck idle at yard or not in service)"}

    trips.sort(key=lambda t: t["tsTime"])
    last = trips[-1]
    miles = round(sum((t.get("distanceTravelled") or 0) for t in trips), 1)
    last_addr = clean_addr(last.get("teAddress"))
    parked_ms = last["teTime"]
    parked_for = (now_ms - parked_ms) / 60000  # minutes since last trip ended

    if parked_for < 8:
        # Trip just closed; truck is moving or stopped only momentarily.
        state = "moving"
        summary = (f"En route near {last_addr} "
                   f"(last update {fmt_time(parked_ms)}, {len(trips)} stops, {miles} mi today)")
    else:
        state = "parked"
        summary = (f"At {last_addr} — parked {human_gap(parked_for)} "
                   f"(since {fmt_time(parked_ms)}; {len(trips)} stops, {miles} mi today)")
    return {**crew, "state": state, "summary": summary,
            "last_addr": last_addr, "parked_since": fmt_time(parked_ms),
            "stops": len(trips), "miles": miles}


ICON = {"moving": "🚚", "parked": "📍", "idle": "💤", "no_vehicle": "⚠️"}


def build_message(statuses):
    now = datetime.now(CENTRAL)
    header = f"**Crew Locations — {now.strftime('%a %b %-d, %-I:%M %p')} CT**"
    lines = [header, ""]
    for s in statuses:
        icon = ICON.get(s["state"], "•")
        driver = s["driver"]
        lines.append(f"{icon} **{s['name']}** ({driver}): {s['summary']}")
    lines.append("")
    lines.append("_Source: Azuga GPS. Location = where the truck last parked, "
                 "which at this hour is effectively its current position._")
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
    recipients = [a.strip() for a in os.environ.get(
        "CREW_LOC_TO", "evelin@blackhilltx.com,denisse@blackhilltx.com").split(",") if a.strip()]
    if not (sender and pw and recipients):
        return False
    html_body = "<br>".join(
        line.replace("**", "") for line in text.split("\n")
    )
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
    now_ms = int(datetime.now(CENTRAL).timestamp() * 1000)

    crews = resolve_vehicle_ids(api_key)  # 1 Azuga call
    statuses = []
    for i, crew in enumerate(crews):
        if i > 0:
            time.sleep(RATE_LIMIT_SLEEP)  # respect 1 req/min
        try:
            statuses.append(crew_status(api_key, crew, now_ms))
        except Exception as e:
            statuses.append({**crew, "state": "no_vehicle",
                             "summary": f"Lookup failed: {e}"})

    text = build_message(statuses)
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

    # Email by default unless explicitly disabled (reliable backup, and the
    # only channel until the Teams webhook secret is configured).
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
