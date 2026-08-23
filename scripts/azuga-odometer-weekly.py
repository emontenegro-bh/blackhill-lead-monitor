#!/usr/bin/env python3
"""Weekly Azuga Odometer Report — fleet mileage snapshot for Black Hill Landscaping.

Pulls current odometer readings for every trackee from the Azuga API, compares
them against last week's readings to show miles driven, and flags anomalies
(no reading, a reading that went DOWN, or the vehicle's two odometer fields
disagreeing by a wide margin).

Auth / config (first match wins):
    1. AZUGA_API_KEY env var           (used in CI)
    2. ~/.config/azuga/config.json     (used locally)

Endpoint (verified live, do not change without re-testing):
    POST https://api.azuga.com/azuga-ws/v3/trackees?limit=100&offset=0
    Body: {}   Headers: Authorization: Basic <base64(api_key)>, Content-Type: application/json
    GET on this path returns HTTP 400 — it must be POST.
    Rate limit: 1 request/minute per API key. This script makes exactly ONE call.

Unit assumption: Azuga's docs say odometer values are in kilometers, but this
has NOT been confirmed against the real trucks. See ODO_UNITS_ARE_KM below —
flip that one constant if it turns out to be wrong. The report always prints
both the raw value and the converted miles value so a wrong assumption is
visible rather than silently baked into "miles driven."

Usage:
  python3 scripts/azuga-odometer-weekly.py             # normal run (saves + emails)
  python3 scripts/azuga-odometer-weekly.py --dry-run   # build + save report, print, send nothing
"""

import base64
import json
import os
import signal
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo

# --- Global timeout guard (matches sibling weekly scripts) ---
SCRIPT_TIMEOUT = 120


def _timeout_handler(signum, frame):
    print(f"ERROR: Script timed out after {SCRIPT_TIMEOUT}s", file=sys.stderr)
    sys.exit(1)


signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(SCRIPT_TIMEOUT)

CENTRAL = ZoneInfo("America/Chicago")

# --- Config ---
BASE_URL_DEFAULT = "https://api.azuga.com/azuga-ws"
CONFIG_FILE = os.path.expanduser("~/.config/azuga/config.json")

# Recipient comes from a variable (overridable via env), never hardcoded
# inline in a function body.
TO_EMAIL = os.environ.get("AZUGA_ODOMETER_TO", "evelin@blackhilltx.com")

# Azuga docs claim odometer values are in kilometers. NOT yet confirmed
# against the real trucks — flip this one constant if that turns out wrong.
ODO_UNITS_ARE_KM = True
KM_TO_MILES = 0.621371

# Anomaly thresholds
MISMATCH_PCT_THRESHOLD = 20.0  # vehicleDeviceCurrentodoReading vs vehicleSupportedOdoValue

# A tracker that has not phoned home in this many days has a frozen odometer
# reading rather than a current one. Trucks park over weekends and holidays,
# so this needs enough slack to avoid false alarms.
STALE_CONTACT_DAYS = 14

# Vehicles known to be parked on purpose. They still get the not-reporting
# flag (their reading genuinely is not current), but it is worded as expected
# rather than as a problem to chase, so the report does not nag every week
# about something already known. Remove a name here the moment it goes back
# into service. (Sales 2, the 2014 Ford Taurus, is parked and unused, not
# sold -- confirmed by Evelin 2026-08-22.)
KNOWN_IDLE_VEHICLES = {"Sales 2"}

# Mail folder in the destination mailbox that reports are filed into. Created
# on first run if absent, the same way dmarc-monitor.py handles its own
# folder. Delivering straight into the folder means the report never lands in
# the inbox, so no Outlook rule is needed and none can silently break.
MAIL_FOLDER = "Fleet Reports"

# Repo root (works both locally and on GitHub Actions)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
REPORT_DIR = os.path.join(REPO_ROOT, ".claude", "reports", "fleet", "odometer", "weekly")
HISTORY_FILE = os.path.join(REPORT_DIR, "odometer-history.json")
WEEKS_TO_KEEP = 52


def log(msg):
    ts = datetime.now(CENTRAL).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def now_ms():
    """Current time as epoch milliseconds UTC (Azuga's time format)."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def epoch_ms_to_date(ms):
    """Format an Azuga epoch-ms timestamp as a Central-time YYYY-MM-DD date."""
    if not ms:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CENTRAL).strftime("%Y-%m-%d")


def to_miles(raw_value):
    """Convert a raw odometer reading to miles per ODO_UNITS_ARE_KM."""
    if raw_value is None:
        return None
    return raw_value * KM_TO_MILES if ODO_UNITS_ARE_KM else raw_value


# ============================================================
# AZUGA API
# ============================================================

def get_api_key_and_base_url():
    """Dual-credential: env var in CI, config file locally."""
    key = os.environ.get("AZUGA_API_KEY", "").strip()
    if key:
        return key, BASE_URL_DEFAULT
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg["api_key"], cfg.get("base_url", BASE_URL_DEFAULT)
    sys.exit("ERROR: No Azuga API key (set AZUGA_API_KEY or create ~/.config/azuga/config.json)")


def fetch_trackees(api_key, base_url):
    """Single call: POST /v3/trackees?limit=100&offset=0 with body {}.

    GET on this endpoint returns HTTP 400 — must be POST. Rate-limited to
    1 request/minute per key, and this script only ever makes this one call.
    """
    enc = base64.b64encode(api_key.encode()).decode()
    req = urllib.request.Request(
        f"{base_url}/v3/trackees?limit=100&offset=0",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Basic {enc}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Azuga HTTP {e.code}: {e.read().decode()[:300]}")
    return body


# ============================================================
# PROCESSING
# ============================================================

def primary_reading(t):
    """Pick the odometer field to trust based on odoSource.

    'Vehicle'    -> vehicleSupportedOdoValue
    'User input' -> vehicleDeviceCurrentodoReading (note the lowercase 'odo')
    anything else -> whichever of the two is present
    """
    source = t.get("odoSource")
    vehicle_val = t.get("vehicleSupportedOdoValue")
    device_val = t.get("vehicleDeviceCurrentodoReading")
    if source == "Vehicle":
        return vehicle_val
    if source == "User input":
        return device_val
    return vehicle_val if vehicle_val is not None else device_val


def process_trackees(data):
    """Filter, compute derived fields, and flag anomalies (delta added later)."""
    rows = []
    for t in data:
        trackee_type_id = t.get("trackeeTypeId")
        raw_primary = primary_reading(t)
        has_real_odometer = raw_primary is not None

        # Exclude non-truck trackees (equipment/assets) unless they carry a
        # real odometer value anyway.
        if trackee_type_id != 1 and not has_real_odometer:
            continue

        vehicle_val = t.get("vehicleSupportedOdoValue")
        device_val = t.get("vehicleDeviceCurrentodoReading")
        device_raw = t.get("vehicleDeviceOdoReading")

        row = {
            "trackeeId": t.get("trackeeId"),
            # NOTE: despite the field being commonly referred to as
            # "trackeeName" elsewhere, the live /v3/trackees response uses
            # plain "name" (verified 2026-08-22). trackeeName is absent.
            "name": t.get("name") or f"Trackee {t.get('trackeeId')}",
            "make": t.get("make"),
            "model": t.get("model"),
            "year": t.get("year"),
            "odo_source": t.get("odoSource"),
            "raw_primary": raw_primary,
            "miles_primary": to_miles(raw_primary),
            "device_raw": device_raw,
            "device_raw_miles": to_miles(device_raw),
            "last_contact_ms": t.get("lastContactDate"),
            "flags": [],
        }

        # Anomaly: no odometer value at all.
        if raw_primary is None:
            row["flags"].append("NO ODOMETER VALUE")

        # Anomaly: the tracker has gone quiet. A dead device keeps reporting
        # its last-known odometer forever, which is indistinguishable from a
        # truck that simply did not move -- both show a 0-mile delta. Without
        # this flag the report is silently wrong. (Verified 2026-08-22:
        # "Sales 2", a 2014 Ford Taurus, last contacted 2026-03-07.)
        known_idle = row["name"] in KNOWN_IDLE_VEHICLES
        row["known_idle"] = known_idle
        last_contact = t.get("lastContactDate")
        if last_contact:
            stale_days = (now_ms() - last_contact) / 86_400_000
            if stale_days > STALE_CONTACT_DAYS:
                if known_idle:
                    row["flags"].append(
                        f"IDLE (expected): parked, no contact in "
                        f"{stale_days:.0f} days (last "
                        f"{epoch_ms_to_date(last_contact)}). Reading is "
                        f"last-known, not current."
                    )
                else:
                    row["flags"].append(
                        f"NOT REPORTING: no contact in {stale_days:.0f} days "
                        f"(last {epoch_ms_to_date(last_contact)}) -- reading "
                        f"is frozen, not a real current odometer"
                    )
        elif not known_idle:
            row["flags"].append("NOT REPORTING: no lastContactDate returned")

        # Anomaly: the two source fields disagree by more than the threshold.
        if vehicle_val not in (None, 0) and device_val not in (None, 0):
            denom = max(abs(vehicle_val), abs(device_val))
            pct = abs(vehicle_val - device_val) / denom * 100 if denom else 0
            if pct > MISMATCH_PCT_THRESHOLD:
                row["flags"].append(
                    f"ODOMETER MISMATCH: vehicleDeviceCurrentodoReading "
                    f"({device_val:,.1f}) vs vehicleSupportedOdoValue "
                    f"({vehicle_val:,.1f}) differ by {pct:.0f}%"
                )

        rows.append(row)

    rows.sort(key=lambda r: r["name"])
    return rows


def apply_deltas(rows, previous_readings):
    """Fill in miles-driven-since-last-report using the prior snapshot, and
    flag any vehicle whose reading decreased."""
    for row in rows:
        prev = previous_readings.get(row["name"]) if previous_readings else None
        row["delta_raw"] = None
        row["delta_miles"] = None
        if prev is not None and row["raw_primary"] is not None and prev.get("raw") is not None:
            delta_raw = row["raw_primary"] - prev["raw"]
            row["delta_raw"] = delta_raw
            row["delta_miles"] = to_miles(delta_raw)
            if delta_raw < 0:
                row["flags"].append(
                    f"DECREASED since last report: {row['raw_primary']:,.1f} now vs "
                    f"{prev['raw']:,.1f} previously ({row['delta_miles']:,.1f} mi)"
                )
    return rows


# ============================================================
# HISTORY (state file — week-over-week deltas)
# ============================================================

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def latest_previous_readings(history):
    """Readings keyed by trackeeName from the most recent prior snapshot."""
    if not history:
        return None
    last_entry = history[-1]
    return last_entry.get("readings", {})


def save_history(history, rows, generated_at_ms):
    entry = {
        "date": datetime.now(CENTRAL).strftime("%Y-%m-%d"),
        "generated_at_ms": generated_at_ms,
        "readings": {
            row["name"]: {
                "raw": row["raw_primary"],
                "odo_source": row["odo_source"],
                "trackeeId": row["trackeeId"],
            }
            for row in rows
        },
    }
    history = history + [entry]
    history = history[-WEEKS_TO_KEEP:]
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    return history


# ============================================================
# REPORT
# ============================================================

def unit_label():
    return "km (assumed)" if ODO_UNITS_ARE_KM else "mi (assumed already miles)"


def fmt_num(v, decimals=1):
    return f"{v:,.{decimals}f}" if v is not None else "—"


def build_report(rows, generated_at_ms, is_first_run):
    now = datetime.now(CENTRAL)
    date_str = now.strftime("%B %d, %Y")
    generated_str = (
        datetime.fromtimestamp(generated_at_ms / 1000, tz=timezone.utc)
        .astimezone(CENTRAL)
        .strftime("%Y-%m-%d %I:%M %p CT")
        if generated_at_ms
        else "unknown"
    )

    flagged = [r for r in rows if r["flags"]]

    lines = []
    lines.append(f"# Weekly Azuga Odometer Report — {date_str}")
    lines.append("")
    lines.append(f"Data generated by Azuga at: {generated_str}")
    lines.append(
        f"Unit assumption: Azuga stores odometer readings in **{unit_label()}**, "
        f"per `ODO_UNITS_ARE_KM = {ODO_UNITS_ARE_KM}` in the script "
        f"(unconfirmed against real trucks — flip that constant if it's wrong)."
    )
    lines.append("")

    if is_first_run:
        lines.append("_First run — no prior week's data to compare, so \"miles since last report\" is blank for everyone._")
        lines.append("")

    if flagged:
        lines.append(f"## Flags ({len(flagged)})")
        lines.append("")
        for r in flagged:
            for flag in r["flags"]:
                lines.append(f"- **{r['name']}**: {flag}")
        lines.append("")
    else:
        lines.append("## Flags")
        lines.append("")
        lines.append("None — all vehicles reporting clean odometer data.")
        lines.append("")

    lines.append("## Fleet Odometer Readings")
    lines.append("")
    lines.append(
        "| Vehicle | Make/Model/Year | Odo Source | Raw Reading | Miles (converted) | "
        "Device Raw (vehicleDeviceOdoReading) | Miles Since Last Report | Flags |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---|")
    for r in rows:
        vehicle_desc = " ".join(str(x) for x in (r["year"], r["make"], r["model"]) if x) or "—"
        flags_str = "; ".join(r["flags"]) if r["flags"] else "—"
        lines.append(
            f"| {r['name']} | {vehicle_desc} | {r['odo_source'] or '—'} | "
            f"{fmt_num(r['raw_primary'])} | {fmt_num(r['miles_primary'])} | "
            f"{fmt_num(r['device_raw'])} | {fmt_num(r['delta_miles'])} | {flags_str} |"
        )
    lines.append("")

    total_vehicles = len(rows)
    total_miles_delta = sum(r["delta_miles"] for r in rows if r["delta_miles"] is not None)
    lines.append(
        f"_{total_vehicles} vehicles reporting. "
        f"Total fleet miles driven since last report: {fmt_num(total_miles_delta)} mi._"
    )

    return "\n".join(lines)


def build_html(report_md):
    """Minimal markdown->HTML for the email body (mirrors sibling scripts)."""
    html = ["<html><body style='font-family:sans-serif;font-size:14px'>"]
    in_table = False
    for line in report_md.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            html.append(f"<h2>{stripped[2:]}</h2>")
        elif stripped.startswith("## "):
            html.append(f"<h3>{stripped[3:]}</h3>")
        elif stripped.startswith("|"):
            if not in_table:
                html.append("<table style='border-collapse:collapse;width:100%;font-size:12px'>")
                in_table = True
                is_header = True
            else:
                is_header = False
            if set(stripped.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
                continue  # separator row
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            tag = "th" if is_header else "td"
            style = "border:1px solid #ccc;padding:4px 8px;text-align:left"
            row_html = "".join(f"<{tag} style='{style}'>{c}</{tag}>" for c in cells)
            html.append(f"<tr>{row_html}</tr>")
        else:
            if in_table:
                html.append("</table>")
                in_table = False
            if stripped.startswith("- "):
                html.append(f"<p style='margin:2px 0'>{stripped}</p>")
            elif stripped.startswith("_") and stripped.endswith("_") and len(stripped) > 1:
                html.append(f"<p style='color:#666;font-style:italic'>{stripped.strip('_')}</p>")
            elif stripped:
                html.append(f"<p>{stripped}</p>")
    if in_table:
        html.append("</table>")
    html.append("</body></html>")
    return "\n".join(html)


# ============================================================
# EMAIL
# ============================================================

def graph_token():
    """App-only token. Same client-credentials flow dmarc-monitor.py uses."""
    tenant = os.environ.get("MS_TENANT_ID", "")
    client = os.environ.get("MS_CLIENT_ID", "")
    secret = os.environ.get("MS_CLIENT_SECRET", "")
    if not (tenant and client and secret):
        return None
    data = urllib.parse.urlencode({
        "client_id": client,
        "client_secret": secret,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())["access_token"]


def graph(token, method, path, body=None):
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}


def ensure_mail_folder(token, mailbox):
    """Find, or create on first run, the folder reports are filed into."""
    res = graph(token, "GET",
                f"/users/{mailbox}/mailFolders?$top=100&$select=id,displayName")
    for f in res.get("value", []):
        if f["displayName"].lower() == MAIL_FOLDER.lower():
            return f["id"]
    created = graph(token, "POST", f"/users/{mailbox}/mailFolders",
                    {"displayName": MAIL_FOLDER})
    log(f"Created mail folder '{MAIL_FOLDER}'")
    return created["id"]


def file_report_to_folder(subject, html):
    """Create the report as a already-read message directly inside the report
    folder.

    Deliberately NOT sendMail: a sent message lands in the inbox and then
    depends on an Outlook rule to move it, and a rule that gets disabled or
    reordered silently puts fleet reports back in front of Evelin. Creating
    the message in the folder means it is never in the inbox in the first
    place, and there is no rule to break.

    Returns True if the report was filed.
    """
    try:
        token = graph_token()
    except Exception as e:
        log(f"Graph token failed ({e}); falling back to SMTP.")
        return False
    if not token:
        log("No MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET set; falling back to SMTP.")
        return False

    mailbox = os.environ.get("MS_USER_EMAIL") or TO_EMAIL
    try:
        folder_id = ensure_mail_folder(token, mailbox)
        graph(token, "POST", f"/users/{mailbox}/mailFolders/{folder_id}/messages", {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": mailbox}}],
            "from": {"emailAddress": {"address": mailbox}},
            "isRead": True,
        })
        log(f"Report filed into '{MAIL_FOLDER}' in {mailbox} (inbox untouched).")
        return True
    except Exception as e:
        log(f"Graph filing failed ({e}); falling back to SMTP.")
        return False


def send_email(subject, plain, html):
    sender = os.environ.get("GMAIL_EMAIL", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not sender or not password:
        log("No GMAIL_EMAIL / GMAIL_APP_PASSWORD configured. Report saved but email not sent.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Assistant", sender))
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [TO_EMAIL], msg.as_string())
        log(f"Email sent to {TO_EMAIL}: {subject}")
        return True
    except Exception as e:
        log(f"Email failed: {e}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    log("Weekly Azuga Odometer Report starting")

    api_key, base_url = get_api_key_and_base_url()

    try:
        body = fetch_trackees(api_key, base_url)
    except Exception as e:
        log(f"ERROR: Azuga API call failed: {e}")
        sys.exit(1)

    data = body.get("data", [])
    generated_at_ms = body.get("generatedAtInMillis")
    log(f"Fetched {len(data)} trackees from Azuga")

    rows = process_trackees(data)
    log(f"{len(rows)} vehicles included in report after filtering")

    history = load_history()
    previous_readings = latest_previous_readings(history)
    is_first_run = previous_readings is None
    rows = apply_deltas(rows, previous_readings)

    flagged_count = sum(1 for r in rows if r["flags"])
    log(f"{flagged_count} vehicles flagged")

    report_md = build_report(rows, generated_at_ms, is_first_run)
    html_report = build_html(report_md)

    # Save markdown report
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_file = os.path.join(REPORT_DIR, f"{datetime.now(CENTRAL).strftime('%Y-%m-%d')}.md")
    with open(report_file, "w") as f:
        f.write(report_md)
    log(f"Report saved: {report_file}")

    # Update history (state file) so next week has something to diff against
    save_history(history, rows, generated_at_ms)
    log(f"History updated: {HISTORY_FILE}")

    subject_prefix = "⚠️ FLAGS" if flagged_count else "✅ OK"
    subject = f"[Fleet Odometer] {subject_prefix} — {datetime.now(CENTRAL).strftime('%B %d, %Y')}"

    if "--dry-run" in sys.argv:
        print("=" * 70)
        print("DRY RUN - report built and saved successfully, email NOT sent")
        print("=" * 70)
        print(report_md)
        sys.exit(0)

    # Preferred path: file the report straight into the mail folder via Graph,
    # so it never appears in the inbox. SMTP is the fallback if Graph creds
    # are missing or the call fails, so a delivery problem never means the
    # report silently vanishes.
    if not file_report_to_folder(subject, html_report):
        send_email(subject, report_md, html_report)

    if flagged_count:
        log(f"RESULT: {flagged_count} vehicles flagged")
    else:
        log("RESULT: All vehicles reporting clean odometer data")


if __name__ == "__main__":
    main()
