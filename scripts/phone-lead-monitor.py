#!/usr/bin/env python3
"""Phone Lead Monitor - Turns Microsoft Forms phone-intake responses into CRM leads.

Carlos / Trini answer the office phone and fill out the internal "Phone Lead Intake"
Microsoft Form while the caller is on the line. Form responses auto-sync to an Excel
workbook in Evelin's OneDrive. This script polls that workbook and, for each new row:
  1. Parses the caller's details + which staffer took the call (Record-name stamp)
  2. Auto-assigns an owner with the SAME rules as the web-form monitor
     (irrigation -> Denisse, commercial maintenance -> Evelin, else round-robin)
  3. Creates the contact in Aspire (Lead Source "Phone Call " only as a fallback;
     WhatConverts is authoritative and its attribution is never overwritten)
  4. Creates contact + deal in HubSpot (source phone_call)
  5. Notifies the assigned owner by email + posts a Teams card, exactly like web leads

Auth: Microsoft Graph app-only (client credentials) via the dedicated
"Black Hill Phone Lead Reader" app registration (Files.Read.All, read-only).
Reads ~/.config/phone-lead-reader/config.json locally, or MS_* env secrets in CI.

Usage:
    python3 phone-lead-monitor.py            # Process new phone leads
    python3 phone-lead-monitor.py --dry-run  # Parse + assign, NO CRM writes / emails
    python3 phone-lead-monitor.py --test     # Test Graph auth + locate the workbook
    python3 phone-lead-monitor.py --list     # Show parsed rows (no writes)
    python3 phone-lead-monitor.py --status   # Show processing stats
"""

import base64, json, logging, os, re, signal, smtplib, subprocess, sys, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import lead_source_map

signal.alarm(120) if hasattr(signal, "alarm") else None

# Dedicated env names (PHONE_MS_*) so this never collides with the Booking
# monitor's Microsoft app secrets, which use the shared MS_* names.
CLOUD_MODE = bool(os.environ.get("PHONE_MS_CLIENT_SECRET"))
DRY_RUN = "--dry-run" in sys.argv

CONFIG_DIR = os.path.expanduser("~/.config/phone-lead-reader")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# State moved to Supabase; see the State section below.
LOG_PATH = os.path.join(CONFIG_DIR, "phone-lead-monitor.log") if not CLOUD_MODE else None
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASPIRE_SYNC = os.path.join(SCRIPT_DIR, "aspire-api-sync.py")
HUBSPOT_SYNC = os.path.join(SCRIPT_DIR, "hubspot-sync.py")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# --- WhatConverts attribution ---
# WhatConverts is the only system that knows where a call actually came from: it
# sees which tracking number was dialled. The intake form never asks the caller.
WC_CONFIG_FILE = os.path.expanduser("~/.config/whatconverts/config.json")
WC_API_BASE = "https://app.whatconverts.com/api/v1"

# How far before the form submission to look for the matching call. Wide enough to
# absorb Carlos filling the form after hanging up plus the Power Automate -> Excel
# -> poll lag; tight enough that a repeat caller months later cannot inherit an old
# call's attribution. The forward allowance covers a form submitted mid-call, before
# WhatConverts has closed out the call record.
WC_MATCH_WINDOW_HOURS = 48
WC_MATCH_FORWARD_HOURS = 2

# --- Owner assignment (mirrors whatconverts-lead-monitor.py) ---
OWNER_EVELIN_HUBSPOT_ID = "88710208"
OWNER_DENISSE_HUBSPOT_ID = "162535167"
HUBSPOT_TO_ASPIRE_OWNER = {"88710208": 6, "162535167": 5}
OWNER_MAP = {
    "88710208": ("Evelin", "evelin@blackhilltx.com", "Evelin Montenegro"),
    "162535167": ("Denisse", "denisse@blackhilltx.com", "Denisse Montenegro"),
}

TEST_NAME_PREFIXES = ("test",)

# Friendly names for known submitter logins (the "Record name" / Responders' Email
# stamp), so notifications read "taken by Carlos" instead of the raw address.
SUBMITTER_NAME_MAP = {
    "branchadmin@blackhilltx.com": "Carlos",
}

_log_handlers = [logging.StreamHandler(sys.stderr)]
if LOG_PATH:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    _log_handlers.append(logging.FileHandler(LOG_PATH))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=_log_handlers)
log = logging.getLogger("phone-lead-monitor")


# --- Config ---

def load_config():
    if CLOUD_MODE:
        return {
            "microsoft": {
                "client_id": os.environ["PHONE_MS_CLIENT_ID"],
                "tenant_id": os.environ["PHONE_MS_TENANT_ID"],
                "client_secret": os.environ["PHONE_MS_CLIENT_SECRET"],
                "user_email": os.environ.get("PHONE_MS_USER_EMAIL", "evelin@blackhilltx.com"),
            },
            "form": {
                # Power Automate writes each new Forms response into this workbook's
                # table in real time (the Forms-linked "Open in Excel" file only
                # sync-updates when a human opens it, so it can't be polled headless).
                "file_name": os.environ.get("PHONE_FORM_FILE", "Phone Lead Responses"),
                "worksheet": os.environ.get("PHONE_FORM_SHEET", "Sheet1"),
            },
        }
    if not os.path.exists(CONFIG_FILE):
        log.error(f"Config not found: {CONFIG_FILE}")
        return None
    with open(CONFIG_FILE) as f:
        return json.load(f)


# --- Auth (app-only client credentials) ---

def get_token(config):
    try:
        import msal
    except ImportError:
        log.error("msal not installed. Run: pip3 install msal")
        return None
    ms = config.get("microsoft", {})
    secret = ms.get("client_secret", "")
    if not secret or secret == "PASTE_SECRET_VALUE_HERE":
        log.error("No client_secret configured. Paste the app secret into "
                  f"{CONFIG_FILE} (or set MS_CLIENT_SECRET).")
        return None
    app = msal.ConfidentialClientApplication(
        ms["client_id"],
        authority=f"https://login.microsoftonline.com/{ms['tenant_id']}",
        client_credential=secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if result and "access_token" in result:
        return result["access_token"]
    log.error(f"Auth failed: {result.get('error_description', 'unknown') if result else 'no result'}")
    return None


# --- Graph API ---

def graph_request(endpoint, token):
    url = f"{GRAPH_BASE}{endpoint}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            return json.loads(body), e.code
        except Exception:
            return {"error": body or str(e)}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def find_workbook(token, user_email, file_name):
    """Locate the Forms Excel workbook in the user's OneDrive. Returns item id or None.

    Forms drops the responses workbook at the OneDrive root. We list root children
    (the /search endpoint rejects app-only tokens), paging through if needed.
    """
    items = []
    endpoint = f"/users/{user_email}/drive/root/children?$select=id,name,file&$top=200"
    while endpoint:
        resp, status = graph_request(endpoint, token)
        if status != 200:
            log.error(f"Workbook listing failed: {status} - {resp}")
            return None
        items.extend(resp.get("value", []))
        nxt = resp.get("@odata.nextLink", "")
        endpoint = nxt.split("graph.microsoft.com/v1.0", 1)[-1] if nxt else None
    # Prefer an exact .xlsx match on the configured name
    candidates = [it for it in items
                  if (it.get("name", "").lower().endswith(".xlsx")
                      and file_name.lower() in it.get("name", "").lower())]
    if not candidates:
        candidates = [it for it in items if it.get("name", "").lower().endswith(".xlsx")]
    if not candidates:
        log.error(f"No .xlsx workbook matching '{file_name}' found in {user_email}'s OneDrive.")
        return None
    item = candidates[0]
    log.info(f"Workbook: {item.get('name')} (id: {item['id']})")
    return item["id"]


def read_rows(token, user_email, item_id, worksheet):
    """Read the used range of the worksheet. Returns list-of-lists (incl. header row)."""
    ws = urllib.parse.quote(worksheet)
    endpoint = (f"/users/{user_email}/drive/items/{item_id}"
                f"/workbook/worksheets('{ws}')/usedRange(valuesOnly=true)?$select=values")
    resp, status = graph_request(endpoint, token)
    if status != 200:
        # Fall back to the first worksheet if the named one isn't found
        wl, wstatus = graph_request(
            f"/users/{user_email}/drive/items/{item_id}/workbook/worksheets?$select=name", token)
        if wstatus == 200 and wl.get("value"):
            first = wl["value"][0]["name"]
            log.warning(f"Worksheet '{worksheet}' not read ({status}); using '{first}'.")
            ws = urllib.parse.quote(first)
            endpoint = (f"/users/{user_email}/drive/items/{item_id}"
                        f"/workbook/worksheets('{ws}')/usedRange(valuesOnly=true)?$select=values")
            resp, status = graph_request(endpoint, token)
    if status != 200:
        log.error(f"Failed to read worksheet values: {status} - {resp}")
        return []
    return resp.get("values", []) or []


# --- Row parsing ---

def _find_col(headers, *keyword_sets):
    """Return index of the first header matching ANY keyword set (all keywords present)."""
    lowered = [str(h or "").strip().lower() for h in headers]
    for kws in keyword_sets:
        for i, h in enumerate(lowered):
            if all(k in h for k in kws):
                return i
    return -1


def build_column_map(headers):
    """Map our logical fields to column indexes, tolerant of Evelin's wording."""
    cmap = {
        "id": _find_col(headers, ["id"]),
        "completion": _find_col(headers, ["completion"], ["completion time"]),
        # Record-name stamp = who submitted (Carlos / Trini). Exact single-word headers.
        "taken_by_name": next((i for i, h in enumerate(headers)
                               if str(h or "").strip().lower() == "name"), -1),
        "taken_by_email": next((i for i, h in enumerate(headers)
                                if str(h or "").strip().lower() == "email"), -1),
        # Caller fields (multi-word, so they never collide with the stamp columns)
        "caller_name": _find_col(headers, ["caller", "name"], ["caller", "first"]),
        "phone": _find_col(headers, ["phone"]),
        "caller_email": _find_col(headers, ["caller", "email"]),
        "address": _find_col(headers, ["property"], ["address"], ["street"]),
        "service": _find_col(headers, ["service"]),
        "notes": _find_col(headers, ["looking"], ["provide"], ["additional"], ["notes"]),
    }
    return cmap


def _cell(row, idx):
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    v = row[idx]
    return "" if v is None else str(v).strip()


def parse_name(name):
    parts = (name or "").strip().split(None, 1)
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def _parse_address_string(raw):
    """Split a free-text address into street/city/state/zip (best effort)."""
    result = {"address": "", "city": "", "state": "", "zip": ""}
    if not raw:
        return result
    import re
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 3:
        result["address"] = parts[0]
        result["city"] = parts[1]
        state_zip = parts[-1].split()
        if state_zip:
            result["state"] = state_zip[0]
        if len(state_zip) > 1:
            result["zip"] = state_zip[-1]
        return result
    if len(parts) == 2:
        result["address"] = parts[0]
        state_zip = parts[1].split()
        if state_zip:
            result["state"] = state_zip[0]
        if len(state_zip) > 1:
            result["zip"] = state_zip[-1]
        return result
    zip_match = re.search(r'\b(\d{5}(?:-\d{4})?)\s*$', raw)
    if zip_match:
        result["zip"] = zip_match.group(1)
        raw = raw[:zip_match.start()].strip().rstrip(",")
    state_match = re.search(r'\b([A-Z]{2})\s*$', raw)
    if state_match:
        result["state"] = state_match.group(1)
        raw = raw[:state_match.start()].strip().rstrip(",")
    result["address"] = raw
    return result


def parse_row(row, cmap):
    """Turn a spreadsheet row into a normalized phone-lead dict."""
    raw_addr = _cell(row, cmap["address"])
    addr = _parse_address_string(raw_addr)
    caller_name = _cell(row, cmap["caller_name"])
    first, last = parse_name(caller_name)
    taken_by = _cell(row, cmap["taken_by_name"]) or _cell(row, cmap["taken_by_email"]) or "Office"
    taken_by = SUBMITTER_NAME_MAP.get(taken_by.strip().lower(), taken_by)
    return {
        "response_id": _cell(row, cmap["id"]),
        "first_name": first,
        "last_name": last,
        "name": caller_name,
        "phone": _cell(row, cmap["phone"]),
        "email": _cell(row, cmap["caller_email"]),
        "address": addr["address"] or raw_addr,
        "city": addr["city"],
        "state": addr["state"] or "TX",
        "zip": addr["zip"],
        "service_interest": _cell(row, cmap["service"]) or "General Inquiry",
        "notes": _cell(row, cmap["notes"]),
        "taken_by": taken_by,
        "completion": _cell(row, cmap["completion"]),
    }


# --- Assignment ---

def assign_lead_owner(lead, state):
    """Irrigation -> Denisse, Commercial Maintenance -> Evelin, else round-robin."""
    service = (lead.get("service_interest", "") or "").lower()
    notes = (lead.get("notes", "") or "").lower()
    if "irrigation" in service or "sprinkler" in service or "irrigation" in notes:
        return OWNER_DENISSE_HUBSPOT_ID
    if "commercial" in service and "maint" in service:
        return OWNER_EVELIN_HUBSPOT_ID
    rr = state.setdefault("round_robin_state", {"last_index": -1})
    owners = [OWNER_EVELIN_HUBSPOT_ID, OWNER_DENISSE_HUBSPOT_ID]
    next_index = (rr.get("last_index", -1) + 1) % len(owners)
    rr["last_index"] = next_index
    return owners[next_index]


# --- WhatConverts attribution lookup ---

def _wc_credentials():
    """(token, secret) from CI env or the local config, or (None, None)."""
    token, secret = os.environ.get("WC_API_TOKEN"), os.environ.get("WC_API_SECRET")
    if token and secret:
        return token, secret
    try:
        with open(WC_CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get("api_token"), cfg.get("api_secret")
    except Exception:
        return None, None


def _wc_get(token, secret, endpoint, params):
    url = f"{WC_API_BASE}{endpoint}?" + urllib.parse.urlencode(params)
    cred = base64.b64encode(f"{token}:{secret}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {cred}", "Accept": "application/json"})
    # Deliberately shorter than the WhatConverts monitor's 45s. This whole script
    # runs under signal.alarm(120), and a slow WhatConverts must not eat the budget
    # the Aspire/HubSpot writes and notifications still need. Timing out here costs
    # attribution on one lead; timing out the run loses the lead.
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _phone_digits(value):
    """Last 10 digits, so '+18179362560' and '817-936-2560' compare equal."""
    return re.sub(r"\D", "", value or "")[-10:]


def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def wc_attribution(lead):
    """Resolve this call's real Lead Source from WhatConverts. Returns (value, note).

    Returns (None, "") when there is no matching call, when the match is
    direct/unknown traffic, or when WhatConverts cannot be reached -- the caller
    then falls back to the "Phone Call " catch-all.

    WHY THIS IS A PULL, NOT A PUSH

    The reverse arrangement raced and lost data. whatconverts-lead-monitor.py learns
    the source seconds after the call ends and tries to stamp it on an Aspire contact
    that does not exist yet. That contact is created from THIS form, so it does not
    appear until Carlos submits a row and the poll picks it up, which can be hours or
    days after the call -- longer than the 24h the attribution used to survive.
    Contacts 2788, 2815 and 2798 lost real Google Organic / GBP / Google Ads
    attribution that way in Aug 2026.

    Here both facts already exist. The call is in WhatConverts and the contact is
    being written right now, so there is nothing to wait for and nothing to expire.
    """
    token, secret = _wc_credentials()
    if not (token and secret):
        log.warning("  WhatConverts credentials unavailable; using Phone Call fallback")
        return None, ""
    digits = _phone_digits(lead.get("phone"))
    if not digits:
        return None, ""

    when = _parse_ts(lead.get("completion")) or datetime.now(timezone.utc)
    earliest = when - timedelta(hours=WC_MATCH_WINDOW_HOURS)
    latest = when + timedelta(hours=WC_MATCH_FORWARD_HOURS)

    # The API filters by calendar date, so ask for whole days and apply the real
    # window below.
    leads, page = [], 1
    try:
        while True:
            data = _wc_get(token, secret, "/leads", {
                "start_date": earliest.date().isoformat(),
                "end_date": latest.date().isoformat(),
                "leads_per_page": 250,
                "page_number": page,
            })
            batch = data.get("leads", []) if isinstance(data, dict) else []
            leads.extend(batch)
            if page >= (data or {}).get("total_pages", 1) or not batch:
                break
            page += 1
    except Exception as e:
        log.warning(f"  WhatConverts lookup failed ({e}); using Phone Call fallback")
        return None, ""

    matches = []
    for wc in leads:
        if _phone_digits(wc.get("phone_number") or wc.get("caller_number")) != digits:
            continue
        ts = _parse_ts(wc.get("date_created"))
        if ts and earliest <= ts <= latest:
            matches.append((ts, wc))
    if not matches:
        log.info(f"  WhatConverts: no call matching {lead.get('phone')} in the "
                 f"{WC_MATCH_WINDOW_HOURS}h before this form; using Phone Call fallback")
        return None, ""

    # Nearest call at or before the form submission -- the one Carlos just took.
    ts, wc = max(matches, key=lambda m: m[0])
    value = lead_source_map.from_whatconverts(wc.get("lead_source"), wc.get("lead_medium"))
    if not value:
        log.info(f"  WhatConverts: WC #{wc.get('lead_id')} is direct/unknown "
                 f"({wc.get('lead_source')!r}/{wc.get('lead_medium')!r}); "
                 f"using Phone Call fallback")
        return None, ""
    log.info(f"  WhatConverts: WC #{wc.get('lead_id')} -> Lead Source '{value}'")
    return value, f"WC #{wc.get('lead_id')} | {value}"


# --- CRM writes ---

def _run_sync(script_path, lead):
    try:
        result = subprocess.run(
            ["python3", script_path, "--lead-json", json.dumps(lead)],
            capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
        return {"success": False, "message": f"No output. stderr: {result.stderr[-300:]}"}
    except Exception as e:
        return {"success": False, "message": str(e)[:200]}


def create_aspire_contact(lead, aspire_owner_id):
    # WhatConverts decides. Only when it has no attributable call for this number do
    # we fall back to naming the channel.
    resolved, wc_note = wc_attribution(lead)
    note = (f"Phone lead taken by {lead.get('taken_by', 'Office')}. "
            f"Service requested: {lead.get('service_interest', 'General Inquiry')}.")
    if wc_note:
        note += f" {wc_note}"

    payload = {
        "first_name": lead["first_name"],
        "last_name": lead["last_name"],
        "email": lead.get("email", ""),
        "phone": lead.get("phone", ""),
        "message": lead.get("notes", ""),
        "source": "phone_call",
        "address": lead.get("address", ""),
        "city": lead.get("city", ""),
        "state": lead.get("state", ""),
        "zip": lead.get("zip", ""),
        "service_interest": lead.get("service_interest", ""),
        "_assigned_aspire_owner_id": aspire_owner_id,
        # A source WhatConverts actually resolved is authoritative and should
        # correct whatever is on the contact. The "Phone Call " catch-all is only a
        # guess about a call we could not attribute, so it defers to any real value
        # already there rather than overwriting it.
        "lead_source_aspire": resolved or lead_source_map.PHONE_CALL,
        "lead_source_only_if_empty": resolved is None,
        "attribution_note": note,
    }
    return _run_sync(ASPIRE_SYNC, payload)


def create_hubspot_contact(lead, hubspot_owner_id):
    payload = {
        "first_name": lead["first_name"],
        "last_name": lead["last_name"],
        "email": lead.get("email", ""),
        "phone": lead.get("phone", ""),
        "message": lead.get("notes", ""),
        "address": lead.get("address", ""),
        "city": lead.get("city", ""),
        "zip": lead.get("zip", ""),
        "service_interest": lead.get("service_interest", ""),
        "source": "phone_call",
        "traffic_source": "phone_call",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "_assigned_hubspot_owner_id": hubspot_owner_id,
    }
    return _run_sync(HUBSPOT_SYNC, payload)


# --- Notifications ---

def _send_via_gmail_smtp(to_emails, subject, html_body, from_name="Black Hill Landscaping"):
    gmail_user = os.environ.get("GMAIL_EMAIL", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (gmail_user and gmail_pass):
        gmail_config = os.path.expanduser("~/.config/gmail-sender/config.json")
        if os.path.exists(gmail_config):
            with open(gmail_config) as f:
                creds = json.load(f)
            gmail_user = gmail_user or creds.get("email", "")
            gmail_pass = gmail_pass or creds.get("app_password", "")
    if not (gmail_user and gmail_pass):
        return False, "No Gmail SMTP credentials"
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{gmail_user}>"
    msg["To"] = ", ".join(to_emails)
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_emails, msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)


def _status_line(label, result):
    action = result.get("action", "error")
    url = result.get("contact_url", "")
    if action == "created" and url.startswith("http"):
        return f'Added to {label} - <a href="{url}">View Contact</a>'
    if action == "exists":
        return f"Already in {label}"
    if result.get("success"):
        return f"Added to {label}"
    return f"{label} ERROR: {result.get('message', 'unknown')[:120]}"


def notify_owner(lead, owner_name, owner_email, aspire_result, hubspot_result):
    name = lead.get("name", "").strip() or "Unknown"
    service = lead.get("service_interest", "General Inquiry")
    address = " ".join(p for p in [lead.get("address", ""), lead.get("city", ""),
                                   lead.get("state", ""), lead.get("zip", "")] if p).strip() or "Not provided"
    html = f"""<div style="font-family: Arial, sans-serif; max-width: 600px;">
<h2 style="color: #115E00; margin-bottom: 4px;">New Phone Lead Assigned to {owner_name}</h2>
<p style="color: #666; margin-top: 0;">Black Hill Landscaping</p>
<hr style="border: 1px solid #C8A951;">
<table style="width: 100%; border-collapse: collapse;">
<tr><td style="padding: 8px; font-weight: bold; width: 150px;">Assigned To</td><td style="padding: 8px; font-weight: bold; color: #115E00;">{owner_name}</td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Name</td><td style="padding: 8px;">{name}</td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Phone</td><td style="padding: 8px;"><a href="tel:{lead.get('phone', '')}">{lead.get('phone', 'Not provided')}</a></td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Email</td><td style="padding: 8px;"><a href="mailto:{lead.get('email', '')}">{lead.get('email') or 'Not provided'}</a></td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Address</td><td style="padding: 8px;">{address}</td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Service</td><td style="padding: 8px;">{service}</td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Source</td><td style="padding: 8px;">Phone Call (taken by {lead.get('taken_by', 'Office')})</td></tr>
</table>
<div style="background: #f5f5f5; padding: 12px; margin: 16px 0; border-left: 4px solid #C8A951;">
<strong>What they're looking for:</strong><br>{(lead.get('notes') or '(none noted)')[:400]}
</div>
<p style="font-size: 13px; color: #888;">{_status_line('Aspire', aspire_result)}<br>{_status_line('HubSpot', hubspot_result)}</p>
</div>"""
    ok, err = _send_via_gmail_smtp(owner_email, f"New Phone Lead Assigned: {name} - {service}", html)
    if ok:
        log.info(f"  Owner notification sent to {owner_email}")
    else:
        log.warning(f"  Owner email failed: {err}")


def notify_teams(lead, owner_id, aspire_result, hubspot_result):
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        return
    _, owner_email, owner_full = OWNER_MAP.get(str(owner_id), ("Team", "evelin@blackhilltx.com", "Team"))
    name = lead.get("name", "").strip() or "Unknown"
    address = " ".join(p for p in [lead.get("address", ""), lead.get("city", ""),
                                   lead.get("state", ""), lead.get("zip", "")] if p).strip() or "Not provided"
    mention_text = f"<at>{owner_full}</at>"
    aspire_url = aspire_result.get("contact_url", "")
    aspire_txt = f"[View in Aspire]({aspire_url})" if aspire_url.startswith("http") else aspire_result.get("action", "N/A")
    hs_url = hubspot_result.get("contact_url", "")
    hs_txt = f"[View in HubSpot]({hs_url})" if hs_url.startswith("http") else hubspot_result.get("action", "N/A")
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4",
            "body": [
                {"type": "Container", "style": "emphasis", "items": [
                    {"type": "TextBlock", "text": f"New Phone Lead: {name}",
                     "weight": "Bolder", "size": "Medium", "color": "Good"}]},
                {"type": "TextBlock", "text": f"Assigned to: {mention_text}",
                 "weight": "Bolder", "spacing": "Small"},
                {"type": "FactSet", "facts": [
                    {"title": "Phone", "value": lead.get("phone", "Not provided")},
                    {"title": "Email", "value": lead.get("email") or "Not provided"},
                    {"title": "Address", "value": address},
                    {"title": "Service", "value": lead.get("service_interest", "General Inquiry")},
                    {"title": "Taken by", "value": lead.get("taken_by", "Office")},
                    {"title": "Source", "value": "Phone Call"},
                ]},
                {"type": "TextBlock",
                 "text": f"**What they're looking for:** {(lead.get('notes') or '(none noted)')[:300]}",
                 "wrap": True, "spacing": "Medium"},
                {"type": "TextBlock", "text": f"Aspire: {aspire_txt} | HubSpot: {hs_txt}",
                 "size": "Small", "isSubtle": True, "spacing": "Small"},
            ],
            "msteams": {"entities": [{"type": "mention", "text": mention_text,
                                      "mentioned": {"id": owner_email, "name": owner_full}}]},
        }}]}
    try:
        data = json.dumps(card).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info(f"  Teams notification sent ({resp.status})")
    except Exception as e:
        log.warning(f"  Teams notification failed (non-fatal): {e}")


# --- State ---
#
# State lives in Supabase. It used to be data/phone-lead-state.json, committed
# to a public repo on every run, and its `processed` map carries the caller's
# NAME along with who took the call and who it was assigned to. That was the
# last real customer data left in the public repo.
#
# This script is the sole writer of its own document, so there is no
# concurrent-write hazard here. See docs/architecture/data-platform-plan.md.

STATE_NAME = "phone-lead-monitor"
DEFAULT_STATE = {
    "processed": {},
    "round_robin_state": {"last_index": -1},
    "stats": {"created": 0, "errors": 0, "total_runs": 0},
}


def load_state():
    # Fails closed. An empty `processed` map would re-create Aspire and HubSpot
    # contacts for every lead already handled, so a failed read must stop the
    # run and let notify-failure alert rather than silently start from scratch.
    return db.load_state(STATE_NAME, default=json.loads(json.dumps(DEFAULT_STATE)))


def save_state(state):
    db.save_state(STATE_NAME, state)


# --- Main ---

def _load_rows(config, token):
    ms = config["microsoft"]
    form = config.get("form", {})
    user_email = ms.get("user_email", "evelin@blackhilltx.com")
    item_id = find_workbook(token, user_email, form.get("file_name", "Phone Lead Intake"))
    if not item_id:
        return None, None
    values = read_rows(token, user_email, item_id, form.get("worksheet", "Sheet1"))
    if not values or len(values) < 2:
        log.info("No response rows in workbook yet.")
        return [], None
    headers = values[0]
    cmap = build_column_map(headers)
    missing = [k for k in ("id", "caller_name", "service") if cmap.get(k, -1) < 0]
    if missing:
        log.error(f"Could not locate required columns {missing}. Headers seen: {headers}")
        return None, None
    log.info(f"Column map: { {k: (headers[v] if 0 <= v < len(headers) else None) for k, v in cmap.items()} }")
    rows = [parse_row(r, cmap) for r in values[1:] if any(str(c).strip() for c in r)]
    return rows, cmap


def process(config, token, state):
    rows, _ = _load_rows(config, token)
    if rows is None:
        sys.exit(1)
    if not rows:
        return

    # Bulk-reprocess guard: on a fresh/empty state with many rows, only act on the
    # last 48h so we never backfill old rows into the CRM after a state reset.
    if not state["processed"] and len(rows) > 5:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        keep = []
        for r in rows:
            recent = True
            try:
                recent = datetime.fromisoformat(
                    r["completion"].replace("Z", "+00:00")) >= cutoff
            except Exception:
                recent = True  # unparseable -> treat as recent
            if recent:
                keep.append(r)
            else:
                state["processed"][r["response_id"]] = {"skipped": "state_reset_guard"}
        if len(keep) < len(rows):
            log.warning(f"State empty and {len(rows)} rows present; only processing "
                        f"{len(keep)} from last 48h, marking the rest processed.")
        rows = keep

    new_count = 0
    for lead in rows:
        rid = lead["response_id"]
        if not rid or rid in state["processed"]:
            continue
        if lead["name"].strip().lower().startswith(TEST_NAME_PREFIXES) and not lead["phone"]:
            state["processed"][rid] = {"skipped": "test"}
            continue

        hubspot_owner = assign_lead_owner(lead, state)
        aspire_owner = HUBSPOT_TO_ASPIRE_OWNER[hubspot_owner]
        owner_name, owner_email, _ = OWNER_MAP[hubspot_owner]
        log.info(f"Phone lead #{rid}: {lead['name']} | {lead['service_interest']} "
                 f"| taken by {lead['taken_by']} -> {owner_name}")

        if DRY_RUN:
            log.info(f"  DRY RUN: would create Aspire(owner={aspire_owner}) + "
                     f"HubSpot(owner={hubspot_owner}) and notify {owner_email}")
            new_count += 1
            continue

        try:
            aspire_result = create_aspire_contact(lead, aspire_owner)
            log.info(f"  Aspire: {aspire_result.get('action', aspire_result.get('message', '?'))}")
        except Exception as e:
            aspire_result = {"success": False, "message": str(e)[:200]}
            log.warning(f"  Aspire failed: {e}")
        try:
            hubspot_result = create_hubspot_contact(lead, hubspot_owner)
            log.info(f"  HubSpot: {hubspot_result.get('action', hubspot_result.get('message', '?'))}")
        except Exception as e:
            hubspot_result = {"success": False, "message": str(e)[:200]}
            log.warning(f"  HubSpot failed: {e}")

        try:
            notify_owner(lead, owner_name, owner_email, aspire_result, hubspot_result)
        except Exception as e:
            log.warning(f"  Owner notification failed (non-fatal): {e}")
        try:
            notify_teams(lead, hubspot_owner, aspire_result, hubspot_result)
        except Exception as e:
            log.warning(f"  Teams notification failed (non-fatal): {e}")

        state["processed"][rid] = {
            "name": lead["name"],
            "service": lead["service_interest"],
            "taken_by": lead["taken_by"],
            "assigned_to": owner_name,
            "aspire": aspire_result.get("action", "error"),
            "hubspot": hubspot_result.get("action", "error"),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }
        if aspire_result.get("success") or hubspot_result.get("success"):
            state["stats"]["created"] += 1
        else:
            state["stats"]["errors"] += 1
        new_count += 1

    log.info(f"Processed {new_count} new phone lead(s)." if new_count else "No new phone leads.")


def run_monitor():
    config = load_config()
    if not config:
        sys.exit(1)
    token = get_token(config)
    if not token:
        sys.exit(1)
    state = load_state()
    state["stats"]["total_runs"] = state["stats"].get("total_runs", 0) + 1
    process(config, token, state)
    if not DRY_RUN:
        save_state(state)


def test_connection():
    config = load_config()
    if not config:
        print(json.dumps({"success": False, "message": "No config"})); return
    token = get_token(config)
    if not token:
        print(json.dumps({"success": False, "message": "Auth failed"})); return
    ms = config["microsoft"]; form = config.get("form", {})
    item_id = find_workbook(token, ms.get("user_email", "evelin@blackhilltx.com"),
                            form.get("file_name", "Phone Lead Intake"))
    print(json.dumps({"success": bool(item_id),
                      "message": "Found workbook" if item_id else "Workbook not found",
                      "item_id": item_id}, indent=2))


def list_rows():
    config = load_config()
    token = get_token(config) if config else None
    if not token:
        print("Auth failed."); return
    rows, _ = _load_rows(config, token)
    if not rows:
        print("No rows."); return
    state = load_state()
    for lead in rows:
        owner = OWNER_MAP[assign_lead_owner(dict(lead), dict(state))][0]
        print(f"  #{lead['response_id']}: {lead['name']} | {lead['phone']} | "
              f"{lead['service_interest']} | taken by {lead['taken_by']} -> {owner}")


def reconcile():
    """Daily safety net: email an alert if the responses table holds any row the
    monitor never routed (or if the table can't be read at all)."""
    alert_to = os.environ.get("ALERT_RECIPIENT", "evelin@blackhilltx.com")
    config = load_config()
    token = get_token(config) if config else None
    if not token:
        _send_via_gmail_smtp(alert_to, "Phone Lead Monitor: reconcile could not authenticate",
                             "<p>The daily phone-lead reconcile could not authenticate to Microsoft "
                             "Graph. Check the PHONE_MS_* secrets and the app registration.</p>")
        print(json.dumps({"ok": False, "reason": "auth"}))
        return
    rows, _ = _load_rows(config, token)
    state = load_state()
    processed = state.get("processed", {})
    if rows is None:
        _send_via_gmail_smtp(alert_to, "Phone Lead Monitor: cannot read responses table",
                             "<p>The daily reconcile could not read the Phone Lead Responses table "
                             "(missing columns or a read error). Phone leads may not be flowing, "
                             "check the Phone Lead Monitor logs.</p>")
        print(json.dumps({"ok": False, "reason": "read"}))
        return
    unprocessed = [r for r in rows if r.get("response_id") and r["response_id"] not in processed]
    if unprocessed:
        items = "".join(
            f"<li>#{r['response_id']} - {r.get('name') or 'Unknown'} "
            f"({r.get('service_interest', '?')}), taken by {r.get('taken_by', '?')}</li>"
            for r in unprocessed[:25])
        html = (f'<div style="font-family:Arial,sans-serif;max-width:600px;">'
                f'<h2 style="color:#B8860B;">Phone Lead Monitor: {len(unprocessed)} unrouted lead(s)</h2>'
                f'<p>These phone-lead form responses are in the table but were never routed to an '
                f'owner. Handle them manually and check the monitor.</p><ul>{items}</ul>'
                f'<p style="font-size:12px;color:#888;">{len(rows)} rows in table, '
                f'{len(processed)} processed.</p></div>')
        _send_via_gmail_smtp(alert_to, f"Phone Lead Monitor: {len(unprocessed)} unrouted lead(s)", html)
        print(json.dumps({"ok": False, "unprocessed": len(unprocessed), "total": len(rows)}))
    else:
        print(json.dumps({"ok": True, "total": len(rows), "processed": len(processed)}))


def show_status():
    state = load_state()
    stats = state.get("stats", {})
    print("Phone Lead Monitor Status")
    print("=" * 40)
    print(f"Total runs:       {stats.get('total_runs', 0)}")
    print(f"Leads created:    {stats.get('created', 0)}")
    print(f"Errors:           {stats.get('errors', 0)}")
    print(f"Total processed:  {len(state.get('processed', {}))}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_connection()
    elif "--list" in sys.argv:
        list_rows()
    elif "--status" in sys.argv:
        show_status()
    elif "--reconcile" in sys.argv:
        # Tracked under its OWN name, not "phone-lead-monitor". Both paths live
        # in this file but they are different scheduled jobs: run_monitor every
        # 30 minutes, reconcile once daily at 13:07. Sharing a name would let
        # the 30-minute runs satisfy a liveness check for reconcile, so
        # reconcile could stop entirely and nothing would notice -- the exact
        # blind spot this tracking exists to close.
        with db.track("phone-lead-reconcile"):
            reconcile()
    else:
        # Only the unattended paths are tracked. --test/--list/--status are
        # interactive debugging and would bury real runs in the history.
        with db.track("phone-lead-monitor"):
            run_monitor()
