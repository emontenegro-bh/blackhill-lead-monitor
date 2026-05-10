#!/usr/bin/env python3
"""WhatConverts Web Form Lead Monitor for Black Hill Landscaping.

Polls the WhatConverts API for new web form leads, then:
  - Skips spam/duplicate leads (WhatConverts flags)
  - Creates Aspire contacts
  - Creates HubSpot contacts + deals with owner assignment
  - Sends notification email to the assigned owner
  - Sends branded auto-reply to the lead via Gmail SMTP

Runs every 5 minutes via GitHub Actions.

Usage:
  python3 whatconverts-lead-monitor.py            # Normal run
  python3 whatconverts-lead-monitor.py --dry-run   # Preview without side effects
  python3 whatconverts-lead-monitor.py --test       # Test WhatConverts API connection
"""

import hashlib, json, os, sys, re, signal, smtplib, urllib.request, urllib.error, urllib.parse, base64
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- Timeout guard (120s) ---
TIMEOUT_SECONDS = 120

def _timeout_handler(signum, frame):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] TIMEOUT: Script exceeded {TIMEOUT_SECONDS}s, exiting.", flush=True)
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(TIMEOUT_SECONDS)

# --- Mode Detection ---
CLOUD_MODE = bool(os.environ.get("WC_API_TOKEN"))
DRY_RUN = "--dry-run" in sys.argv

# --- State ---
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "processed-state.json")

# --- Logging ---

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --- Config ---

def load_config():
    if CLOUD_MODE:
        return load_config_from_env()
    return load_config_from_file()


def load_config_from_file():
    wc_path = os.path.expanduser("~/.config/whatconverts/config.json")
    with open(wc_path) as f:
        wc = json.load(f)
    hs_path = os.path.expanduser("~/.config/hubspot/config.json")
    hs_token = ""
    if os.path.exists(hs_path):
        with open(hs_path) as f:
            hs_token = json.load(f).get("access_token", "")
    return {
        "whatconverts": {
            "api_token": wc.get("api_token", ""),
            "api_secret": wc.get("api_secret", ""),
            "profile_id": wc.get("profile_id", ""),
        },
        "notifications": {
            "lead_recipients": ["evelin@blackhilltx.com", "denisse@blackhilltx.com"],
            "spam_recipients": ["evelin@blackhilltx.com"],
            "from_email": "evelin@blackhilltx.com",
            "from_name": "Black Hill Lead Monitor",
        },
        "hubspot": {
            "enabled": bool(hs_token),
            "access_token": hs_token,
        },
        "mailchimp": {
            "enabled": False,
            "api_key": "",
            "server_prefix": "us20",
            "list_id": "",
            "tag": "web-lead",
        },
        "aspire": {
            "enabled": True,
        },
        "auto_reply": {
            "enabled": True,
            "from_name": "Black Hill Landscaping",
            "from_email": "inquiry@blackhilltx.com",
            "subject": "We received your inquiry - Black Hill Landscaping",
        },
    }


def load_config_from_env():
    return {
        "whatconverts": {
            "api_token": os.environ["WC_API_TOKEN"],
            "api_secret": os.environ["WC_API_SECRET"],
            "profile_id": os.environ.get("WC_PROFILE_ID", "162442"),
        },
        "notifications": {
            "lead_recipients": os.environ.get("LEAD_RECIPIENTS", "evelin@blackhilltx.com,denisse@blackhilltx.com").split(","),
            "spam_recipients": os.environ.get("SPAM_RECIPIENTS", "evelin@blackhilltx.com").split(","),
            "from_email": os.environ.get("NOTIFY_FROM_EMAIL", "evelin@blackhilltx.com"),
            "from_name": "Black Hill Lead Monitor",
        },
        "hubspot": {
            "enabled": bool(os.environ.get("HUBSPOT_ACCESS_TOKEN")),
            "access_token": os.environ.get("HUBSPOT_ACCESS_TOKEN", ""),
        },
        "mailchimp": {
            "enabled": bool(os.environ.get("MAILCHIMP_API_KEY")),
            "api_key": os.environ.get("MAILCHIMP_API_KEY", ""),
            "server_prefix": os.environ.get("MAILCHIMP_SERVER", "us20"),
            "list_id": os.environ.get("MAILCHIMP_LIST_ID", ""),
            "tag": "web-lead",
        },
        "aspire": {
            "enabled": bool(os.environ.get("ASPIRE_CLIENT_ID")),
            "api_client_id": os.environ.get("ASPIRE_CLIENT_ID", ""),
            "api_secret": os.environ.get("ASPIRE_SECRET", ""),
        },
        "auto_reply": {
            "enabled": os.environ.get("AUTO_REPLY_ENABLED", "true").lower() == "true",
            "from_name": "Black Hill Landscaping",
            "from_email": os.environ.get("AUTO_REPLY_FROM", "inquiry@blackhilltx.com"),
            "subject": "We received your inquiry - Black Hill Landscaping",
        },
    }


# --- State Management ---

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_ids": [], "stats": {"total_leads": 0, "total_spam": 0}}


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- WhatConverts API ---

def wc_api_request(config, endpoint, params=None):
    """Make a WhatConverts API request with Basic auth."""
    wc = config["whatconverts"]
    token = wc["api_token"]
    secret = wc["api_secret"]

    url = f"https://app.whatconverts.com/api/v1{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    credentials = base64.b64encode(f"{token}:{secret}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        log(f"  ERROR: WhatConverts API {e.code}: {body}")
        return None
    except Exception as e:
        log(f"  ERROR: WhatConverts request failed: {e}")
        return None


def fetch_recent_leads(config, lookback_minutes=130):
    """Fetch web form and phone call leads from the last N minutes."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    profile_id = config["whatconverts"]["profile_id"]

    all_leads = []
    for lead_type in ("web_form", "phone_call"):
        page = 1
        while True:
            result = wc_api_request(config, "/leads", {
                "profile_id": profile_id,
                "lead_type": lead_type,
                "start_date": since,
                "leads_per_page": 50,
                "page_number": page,
            })
            if not result:
                break
            leads = result.get("leads", [])
            all_leads.extend(leads)
            if page >= result.get("total_pages", 1):
                break
            page += 1

    return all_leads


# --- Lead Parsing ---

# Solicitation patterns (form spam from bots pitching services)
SOLICITATION_KEYWORDS = [
    "seo", "backlink", "link building", "domain authority",
    "guest post", "link exchange", "buy traffic",
    "virtual assistant", "appointment setting", "drip campaign",
    "fill your calendar", "we provide", "we offer", "prospecting",
    "visitors to your website", "visitors to your site",
    "traffic to your website", "traffic to your site",
    "missing out on leads", "more affordable than",
    "youtube subscribers", "youtube channel",
    "we help website owners", "we help businesses",
    "book a quick call", "book a call",
    "schedule a quick demo", "schedule a demo",
    "free audit", "our agency", "our team can help",
    "increase your revenue", "grow your business",
    "lead generation service", "leads for your business",
    "i'm reaching out because we", "reaching out because we",
    "we can guarantee", "we specialize in",
    "playbook", "turned into solid calls", "booked jobs",
    # Sales pitch patterns (added 2026-04-21)
    "runs your entire business", "handle the execution",
    "you focus on scaling", "opt out", "reply stop",
    "15-minute call", "15 minute call", "quick call this week",
    "open to a quick", "happy to connect and trade notes",
    "working in the business to", "shift from working in",
    "we built", "we handle", "you don't need to manage",
    "trained human", "no pressure at all",
]

SPAM_EMAIL_DOMAINS = [
    "rambler.ru", "yandex.ru", "mail.ru", "melssa.com",
    "mailnesia.com", "guerrillamail.com", "tempmail.com",
    "throwaway.email", "sharklasers.com", "circuitprompt.com",
    "consoleaidly.com", "parallelaid.com", "fusionescort.com",
    "vettedvas.com", "smartclerical.com", "sendproud.com",
    "cachehelper.com", "scopeadjunct.com",
]

# Our own addresses (never process as leads)
OWN_ADDRESSES = [
    "inquiry@blackhilltx.com", "sales@meangreenlawncare.com",
    "info@meangreenlawncare.com", "info@blackhilltx.com",
    "evelin@blackhilltx.com", "denisse@blackhilltx.com",
]

# Our own phone numbers (tracking numbers + business line — never process as leads)
OWN_PHONE_NUMBERS = [
    "+18179950324", "+18174056883", "+18174054340", "+18174054439",
    "+18172904711", "+18173456954", "+18173808161", "+18173829016",
]

# Known vendor/supplier phone numbers — skip these, not customer leads.
# Add numbers here as vendors are identified.
VENDOR_PHONE_NUMBERS = [
    # Format: "+1XXXXXXXXXX"  # Vendor Name
]

# Minimum call duration (seconds) to consider a call a real lead.
# Unanswered calls shorter than this are skipped.
MIN_CALL_DURATION_SECONDS = 15

# Service detection
SERVICE_MAP = {
    "Irrigation & Sprinklers": ["irrigation", "sprinkler", "drip system", "water line"],
    "Tree & Shrub Care": ["tree", "shrub", "stump", "trimming", "pruning"],
    "Fertilization & Weed Control": ["fertiliz", "weed", "pre-emergent", "post-emergent", "fert"],
    "Landscape Design & Install": ["landscape design", "landscaping", "design", "install", "planting", "flower", "color"],
    "Hardscaping": ["patio", "retaining wall", "pavers", "hardscape", "walkway", "stonework"],
    "Drainage & Erosion": ["drainage", "erosion", "french drain", "grading", "reslope"],
    "Sod Installation": ["sod", "new lawn", "turf"],
    "Commercial Maintenance": ["commercial maint", "commercial property", "hoa maint"],
    "Mulch & Bed Maintenance": ["mulch", "flower bed", "bed maintenance"],
    "Aeration & Overseeding": ["aeration", "aerate", "overseed"],
    "Outdoor Lighting": ["lighting", "landscape light", "outdoor light"],
    "Fence Installation": ["fence", "fencing"],
}


def detect_service(text):
    """Detect service interest from free text."""
    text_lower = text.lower()
    for service_name, keywords in SERVICE_MAP.items():
        for kw in keywords:
            if kw in text_lower:
                return service_name
    return "General Inquiry"


# Known spam contact names (exact match, case-insensitive)
SPAM_NAMES = [
    "susan smith",
]


def is_spam_lead(lead_data):
    """Check if a WhatConverts lead is spam using multiple signals."""
    # WhatConverts built-in spam flag
    if lead_data.get("spam", False):
        return True, "WhatConverts flagged as spam"

    # Check blocked names
    fields = lead_data.get("additional_fields", {})
    if isinstance(fields, list):
        fields = {}
    contact_name = (fields.get("Name", "") or lead_data.get("contact_name", "")).strip().lower()
    for spam_name in SPAM_NAMES:
        if contact_name == spam_name:
            return True, f"Blocked name: {spam_name}"

    is_call = lead_data.get("lead_type", "").lower() == "phone call"

    if is_call:
        return _is_spam_call(lead_data)
    return _is_spam_form(lead_data)


def _is_spam_call(lead_data):
    """Check if a phone call lead should be skipped."""
    caller = (lead_data.get("caller_number") or lead_data.get("contact_phone_number") or "").strip()
    digits = re.sub(r"\D", "", caller)
    if len(digits) == 11 and digits.startswith("1"):
        normalized = f"+{digits}"
    elif len(digits) == 10:
        normalized = f"+1{digits}"
    else:
        normalized = caller

    # Our own numbers
    if normalized in OWN_PHONE_NUMBERS:
        return True, f"Own number: {normalized}"

    # Known vendor numbers
    if normalized in VENDOR_PHONE_NUMBERS:
        return True, f"Vendor number: {normalized}"

    # Short unanswered calls — not real leads
    duration = lead_data.get("call_duration_seconds", 0) or 0
    answer_status = (lead_data.get("answer_status") or "").lower()
    if duration < MIN_CALL_DURATION_SECONDS and answer_status != "answered":
        return True, f"Short unanswered call ({duration}s, {answer_status})"

    # No caller number at all
    if not digits:
        return True, "No caller number"

    return False, ""


def _classify_message_with_llm(name, email, address, service, message):
    """Use Claude API to classify whether a lead message is spam/solicitation.
    Returns (is_spam: bool, reason: str). Falls back to False on any error."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        for path in ["~/.config/anthropic/config.json", "~/.config/anthropic/api-key"]:
            fp = os.path.expanduser(path)
            if os.path.exists(fp):
                try:
                    with open(fp) as f:
                        content = f.read().strip()
                    if content.startswith("{"):
                        api_key = json.loads(content).get("api_key", "")
                    else:
                        api_key = content
                    if api_key:
                        break
                except Exception:
                    pass
    if not api_key:
        return False, ""

    prompt = f"""You are a spam classifier for a landscaping company (Black Hill Landscaping) in Fort Worth, TX.
Classify this web form submission as SPAM or LEGIT.

SPAM = someone selling a product/service TO the company, soliciting business, or fake/bot submission.
LEGIT = a real potential customer asking about landscaping, irrigation, lawn care, drainage, sod, or related services.

Submission:
Name: {name}
Email: {email}
Address: {address}
Service requested: {service}
Message: {message}

Respond with exactly one line:
SPAM: <reason>
or
LEGIT"""

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        reply = data.get("content", [{}])[0].get("text", "").strip()
        if reply.upper().startswith("SPAM"):
            reason = reply.split(":", 1)[1].strip() if ":" in reply else "LLM classified as spam"
            log(f"  LLM spam check: {reply}")
            return True, f"LLM: {reason}"
        return False, ""
    except Exception as e:
        log(f"  LLM spam check failed (non-fatal): {e}")
        return False, ""


def _is_spam_form(lead_data):
    """Check if a web form lead is spam."""
    fields = lead_data.get("additional_fields", {})
    if isinstance(fields, list):
        fields = {}
    email = (lead_data.get("contact_email_address") or fields.get("Email", "") or "").lower()
    message = (fields.get("Anything else you would like to share?", "") or "").lower()

    # Check email domain
    for domain in SPAM_EMAIL_DOMAINS:
        if email.endswith(f"@{domain}"):
            return True, f"Spam email domain: {domain}"

    # Check solicitation keywords in message
    for kw in SOLICITATION_KEYWORDS:
        if kw in message:
            return True, f"Solicitation keyword: {kw}"

    # Check own addresses
    for addr in OWN_ADDRESSES:
        if email == addr:
            return True, f"Own address: {addr}"

    # "test" in name (internal test submissions)
    name = (fields.get("Name", "") or lead_data.get("contact_name", "")).strip().lower()
    if re.search(r'\btest\b', name):
        return True, f"Test submission detected in name: '{name}'"

    # Fake/reserved phone numbers (555 area code is reserved by FCC)
    phone = fields.get("Contact No", "") or lead_data.get("contact_phone_number", "") or ""
    phone_digits = re.sub(r"\D", "", phone)
    if phone_digits:
        ac = phone_digits[:3] if not phone_digits.startswith("1") else phone_digits[1:4]
        if ac == "555":
            return True, f"Fake phone number (555 area code): {phone}"

    # Fake address detection (bots use "123 Main St" pattern)
    address = (fields.get("Address", "") or lead_data.get("address", "") or "").lower()
    if "123 main st" in address:
        return True, "Fake address: 123 Main St"

    # City field contains person's name instead of a city (common bot pattern)
    city_field = (fields.get("City", "") or "").strip()
    name_field = (fields.get("Name", "") or "").strip()
    if city_field and name_field and city_field.lower() == name_field.lower():
        return True, f"City field contains name instead of city: '{city_field}'"

    # Out-of-state geolocation (WhatConverts IP-based state != Texas)
    geo_state = (lead_data.get("state", "") or "").strip()
    if geo_state and geo_state not in ("Texas", "TX", ""):
        lead_source = (lead_data.get("lead_source", "") or "").lower()
        lead_medium = (lead_data.get("lead_medium", "") or "").lower()
        # Out-of-state + direct/unknown source = very likely spam
        if lead_source in ("(direct)", "") or lead_medium in ("(none)", ""):
            return True, f"Out-of-state geolocation ({geo_state}) with direct/unknown source"

    # Non-Texas area code with no local signals
    phone = fields.get("Contact No", "") or ""
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) >= 10:
            area_code = digits[:3] if not digits.startswith("1") else digits[1:4]
            tx_area_codes = {"210", "214", "254", "281", "325", "346", "361", "409",
                             "430", "432", "469", "512", "682", "713", "726", "737",
                             "806", "817", "830", "832", "903", "915", "936", "940",
                             "945", "956", "972", "979"}
            if area_code not in tx_area_codes and "(direct)" in (lead_data.get("lead_source", "") or "").lower():
                return True, f"Non-TX area code ({area_code}) with direct/unknown source"

    # No email and no phone = likely spam
    if not email and not phone:
        return True, "No email and no phone"

    # LLM message classification (catches sales pitches that keywords miss)
    if message and len(message) > 20:
        name = (fields.get("Name", "") or lead_data.get("contact_name", "")).strip()
        service = fields.get("What Type Of Service Do You Need?", "")
        is_spam, reason = _classify_message_with_llm(name, email, address, service, message)
        if is_spam:
            return True, reason

    return False, ""


def parse_wc_lead(lead_data):
    """Parse a WhatConverts lead into our standard lead dict."""
    is_call = lead_data.get("lead_type", "").lower() == "phone call"
    if is_call:
        return _parse_call_lead(lead_data)
    return _parse_form_lead(lead_data)


def _parse_form_lead(lead_data):
    """Parse a web form lead."""
    fields = lead_data.get("additional_fields", {})
    if isinstance(fields, list):
        fields = {}

    # Name
    full_name = (fields.get("Name", "") or lead_data.get("contact_name", "")).strip()
    first_name = ""
    last_name = ""
    if full_name:
        parts = full_name.split(None, 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

    # Email
    email = (fields.get("Email", "") or lead_data.get("contact_email_address", "")).strip()

    # Phone - only if it looks like a phone (not email)
    phone = (fields.get("Contact No", "") or lead_data.get("contact_phone_number", "")).strip()
    if "@" in phone:
        phone = ""

    # Service
    service = (fields.get("What Type Of Service Do You Need?", "") or "").strip()
    message_text = (fields.get("Anything else you would like to share?", "") or "").strip()
    if not service or service.lower() in ("other", "general", ""):
        service = detect_service(f"{service} {message_text}")

    # Address
    address = (fields.get("Address", "") or "").strip()
    city = (fields.get("City", "") or lead_data.get("city", "")).strip()
    state = lead_data.get("state", "Texas")
    zip_code = (lead_data.get("zip", "") or "").strip()

    # Source info
    source = lead_data.get("lead_source", "")
    medium = lead_data.get("lead_medium", "")
    if source and medium:
        traffic_source = f"{source} / {medium}"
    else:
        traffic_source = source or medium or "unknown"

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "service_interest": service,
        "address": address,
        "city": city,
        "state": state if state != "Texas" else "TX",
        "zip": zip_code,
        "message": message_text[:500],
        "source": "web_form",
        "traffic_source": traffic_source,
        "received_at": lead_data.get("date_created", ""),
        "wc_lead_id": str(lead_data.get("lead_id", "")),
        "wc_lead_status": lead_data.get("lead_status", ""),
    }


def _parse_call_lead(lead_data):
    """Parse a phone call lead."""
    # Name from caller ID (carrier data — often partial like "Ferrington C.")
    caller_name = (lead_data.get("caller_name") or lead_data.get("contact_name") or "").strip()
    first_name = ""
    last_name = ""
    if caller_name:
        # Clean up carrier-style names (trailing periods, initials)
        caller_name = caller_name.rstrip(". ")
        parts = caller_name.split(None, 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1].rstrip(".") if len(parts) > 1 else ""

    # Phone
    phone = (lead_data.get("caller_number") or lead_data.get("contact_phone_number") or "").strip()

    # Location from caller ID
    city = (lead_data.get("caller_city") or lead_data.get("city") or "").strip()
    state = (lead_data.get("caller_state") or lead_data.get("state") or "TX").strip()
    zip_code = (lead_data.get("caller_zip") or lead_data.get("zip") or "").strip()

    # Try to detect service from call transcription
    transcription = (lead_data.get("call_transcription") or "").strip()
    service = detect_service(transcription) if transcription else "General Inquiry"

    # Call details for the message field
    duration = lead_data.get("call_duration", "")
    answer_status = lead_data.get("answer_status", "")
    message_parts = []
    if answer_status:
        message_parts.append(f"Call: {answer_status}")
    if duration:
        message_parts.append(f"Duration: {duration}")
    if transcription:
        # Include first 300 chars of transcription
        message_parts.append(f"Transcription: {transcription[:300]}")
    message_text = " | ".join(message_parts)

    # Source info
    source = lead_data.get("lead_source", "")
    medium = lead_data.get("lead_medium", "")
    if source and medium:
        traffic_source = f"{source} / {medium}"
    else:
        traffic_source = source or medium or "unknown"

    # Tracking number name helps identify channel
    phone_name = lead_data.get("phone_name", "")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": "",
        "phone": phone,
        "service_interest": service,
        "address": "",
        "city": city,
        "state": state if state != "Texas" else "TX",
        "zip": zip_code,
        "message": message_text[:500],
        "source": "phone_call",
        "traffic_source": traffic_source,
        "received_at": lead_data.get("date_created", ""),
        "wc_lead_id": str(lead_data.get("lead_id", "")),
        "wc_lead_status": lead_data.get("lead_status", ""),
        "call_duration_seconds": lead_data.get("call_duration_seconds", 0),
        "answer_status": lead_data.get("answer_status", ""),
        "phone_name": phone_name,
    }


# --- Email Delivery: Gmail SMTP ---

def _send_via_gmail_smtp(to_emails, subject, html_body, from_email=None, from_name=None, reply_to=None):
    """Send HTML email via Gmail SMTP. Returns (success, error_message)."""
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

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if isinstance(to_emails, str):
        to_emails = [to_emails]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{gmail_user}>" if from_name else gmail_user
    msg["To"] = ", ".join(to_emails)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_emails, msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)


def send_teams_notification(config, lead, lead_type="lead", aspire_url=None, hubspot_status=None,
                            owner_name=None, owner_email=None):
    """Send lead alert to Microsoft Teams via Power Automate webhook."""
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not webhook_url or DRY_RUN:
        if DRY_RUN:
            log("  DRY RUN: Would send Teams notification")
        return

    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Unknown"
    phone = lead.get("phone", "Not provided")
    email = lead.get("email", "Not provided")
    service = lead.get("service_interest", "General Inquiry")
    source = lead.get("traffic_source", "Web Form")
    message = (lead.get("message", "") or "")[:300]

    # Build Aspire/HubSpot status lines
    aspire_text = "Not added"
    if aspire_url and aspire_url.startswith("http"):
        aspire_text = f"[View in Aspire]({aspire_url})"
    elif aspire_url == "exists":
        aspire_text = "Already in Aspire"

    hubspot_text = "Not added"
    if hubspot_status and hubspot_status.startswith("http"):
        hubspot_text = f"[View in HubSpot]({hubspot_status})"
    elif hubspot_status in ("created", "exists"):
        hubspot_text = hubspot_status.capitalize()

    # Build assignee line with @mention
    assignee_name = owner_name or "Team"
    assignee_email = owner_email or ""
    mention_text = f"<at>{assignee_name}</at>" if assignee_email else assignee_name

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "Container",
                        "style": "emphasis",
                        "items": [{
                            "type": "TextBlock",
                            "text": f"New Lead: {name}",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "Good"
                        }]
                    },
                    {
                        "type": "TextBlock",
                        "text": f"Assigned to: {mention_text}",
                        "weight": "Bolder",
                        "spacing": "Small"
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {"title": "Phone", "value": phone},
                            {"title": "Email", "value": email},
                            {"title": "Service", "value": service},
                            {"title": "Source", "value": source},
                        ]
                    },
                    {
                        "type": "TextBlock",
                        "text": f"**Message:** {message}" if message else "*(no message)*",
                        "wrap": True,
                        "spacing": "Medium"
                    },
                    {
                        "type": "TextBlock",
                        "text": f"Aspire: {aspire_text} | HubSpot: {hubspot_text}",
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "Small"
                    }
                ],
                "msteams": {
                    "entities": [{
                        "type": "mention",
                        "text": mention_text,
                        "mentioned": {
                            "id": assignee_email,
                            "name": assignee_name
                        }
                    }] if assignee_email else []
                }
            }
        }]
    }

    try:
        data = json.dumps(card).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"  Teams notification sent ({resp.status})")
    except Exception as e:
        log(f"  WARNING: Teams notification failed (non-fatal): {e}")


def send_html_email(api_key, to_emails, from_email, from_name, subject, html_body, reply_to=None):
    """Send HTML email via Gmail SMTP.

    Returns True if delivered, False if failed.
    The api_key parameter is retained for call-site compatibility but is ignored.
    """
    if isinstance(to_emails, str):
        to_emails = [to_emails]

    ok, err = _send_via_gmail_smtp(to_emails, subject, html_body, from_email, from_name, reply_to)
    if ok:
        log(f"  Email sent via Gmail SMTP to {to_emails}")
        return True

    log(f"  ERROR: Gmail SMTP email delivery failed ({err})")
    return False


# --- Auto-Reply via Gmail SMTP ---

def send_auto_reply(config, lead):
    """Send branded auto-reply to the lead via Gmail SMTP."""
    if DRY_RUN:
        log(f"  DRY RUN: Would send auto-reply to {lead['email']}")
        return True
    if not config["auto_reply"].get("enabled", True):
        log("  Auto-reply disabled.")
        return False

    email = lead.get("email", "").strip()
    if not email or "@" not in email:
        log("  No valid email for auto-reply.")
        return False

    from_name = config["auto_reply"]["from_name"]
    from_email = config["auto_reply"]["from_email"]
    subject = config["auto_reply"]["subject"]
    first_name = lead.get("first_name", "").strip()
    greeting = first_name if first_name else "Hello"

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  body {{ font-family: 'Google Sans', Arial, sans-serif; color: #333; margin: 0; padding: 0; background: #f5f5f5; }}
  .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; }}
  .header {{ background: #000000; padding: 30px; text-align: center; }}
  .body {{ padding: 30px; line-height: 1.7; font-size: 15px; color: #333; }}
  .body p {{ margin: 0 0 15px; }}
  .divider {{ border: none; border-top: 1px solid #EFE9DF; margin: 25px 0; }}
  .footer {{ background: #000000; padding: 28px 30px; text-align: center; }}
  .gold {{ color: #C9A24D; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <img src="https://blackhilllandscaping.com/wp-content/uploads/2026/01/cropped-Black-Hill-Logo-Full-1.png" alt="Black Hill Landscaping" style="max-width: 200px; height: auto;">
  </div>
  <div class="body">
    <p>{greeting},</p>
    <p>Thank you for reaching out. We received your inquiry and a member of our team will be following up shortly.</p>
    <div style="background: #F9F6F0; border-left: 4px solid #C9A24D; padding: 16px 20px; margin: 20px 0; border-radius: 0 4px 4px 0;">
      <p style="margin: 0 0 4px; font-weight: bold; color: #C9A24D; font-size: 14px; letter-spacing: 0.5px;">OUR 5-10 RULE</p>
      <p style="margin: 0; font-size: 14px; color: #444;">Inquiries received before <strong>5 PM</strong> Monday through Friday get a response the same day. After 5 PM or on weekends, we will reach out by <strong>10 AM</strong> the next business day.</p>
    </div>
    <p>In the meantime, if you have photos of the property, specific areas of concern, or timeline details, feel free to reply to this email so we can come prepared.</p>
    <hr class="divider">
    <p style="font-size: 14px; color: #666;">
    <span class="gold">Black Hill Landscaping</span><br>
    <a href="tel:+18179950324" style="color: #C9A24D; text-decoration: none; font-weight: bold;">(817) 995-0324</a><br>
    <a href="https://blackhilllandscaping.com" style="color: #C9A24D;">BlackHillLandscaping.com</a>
    </p>
  </div>
  <div class="footer">
    <p style="margin: 0; font-family: Georgia, serif; font-size: 18px; color: #C9A24D; letter-spacing: 1.5px; font-style: italic;">Where Excellence Takes Root</p>
  </div>
</div>
</body>
</html>"""

    if send_html_email(
        api_key="",
        to_emails=email,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        html_body=html_body,
        reply_to="inquiry@blackhilltx.com",
    ):
        log(f"  Auto-reply sent to {email}")
        return True
    return False


# --- Aspire Lead Source Mapping ---

def _aspire_lead_source(lead):
    """Map WhatConverts traffic source to Aspire LeadSource dropdown value."""
    source = (lead.get("traffic_source") or "").lower()
    if "cpc" in source:
        return "Advertising"
    if "organic" in source:
        return "Website"
    if lead.get("source") == "phone_call" and ("direct" in source or not source or source == "unknown"):
        return "Call In"
    if "referral" in source:
        return "Website"
    if "email" in source or "mailchimp" in source:
        return "Website"
    return "Website"


# --- Owner Notification via Gmail SMTP ---

def send_owner_notification(config, lead, owner_name, owner_email, aspire_url=None, hubspot_status=None):
    """Send lead assignment notification to the assigned owner via Gmail SMTP."""
    if DRY_RUN:
        log(f"  DRY RUN: Would notify {owner_email}")
        return

    from_email = config["notifications"]["from_email"]
    from_name = config["notifications"]["from_name"]
    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Unknown"
    service = lead.get("service_interest", "General Inquiry")

    if aspire_url and aspire_url.startswith("http"):
        aspire_line = f'Added to Aspire - <a href="{aspire_url}">View Contact</a>'
    elif aspire_url == "exists":
        aspire_line = "Already in Aspire"
    else:
        aspire_line = "Not added to Aspire"

    if hubspot_status and hubspot_status.startswith("http"):
        hubspot_line = f'Added to HubSpot - <a href="{hubspot_status}">View Contact</a>'
    elif hubspot_status == "created":
        hubspot_line = "Added to HubSpot"
    elif hubspot_status == "exists":
        hubspot_line = "Already in HubSpot"
    else:
        hubspot_line = "Not added to HubSpot"

    html = f"""<div style="font-family: Arial, sans-serif; max-width: 600px;">
<h2 style="color: #115E00; margin-bottom: 4px;">New Lead Assigned to {owner_name}</h2>
<p style="color: #666; margin-top: 0;">Black Hill Landscaping</p>
<hr style="border: 1px solid #C8A951;">
<table style="width: 100%; border-collapse: collapse;">
<tr><td style="padding: 8px; font-weight: bold; width: 140px;">Assigned To</td><td style="padding: 8px; font-weight: bold; color: #115E00;">{owner_name}</td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold; width: 140px;">Name</td><td style="padding: 8px;">{name}</td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Phone</td><td style="padding: 8px;"><a href="tel:{lead.get('phone', '')}">{lead.get('phone', 'Not provided')}</a></td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Email</td><td style="padding: 8px;"><a href="mailto:{lead.get('email', '')}">{lead.get('email', 'Not provided')}</a></td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Address</td><td style="padding: 8px;">{lead.get('address', '')} {lead.get('city', '')} {lead.get('state', '')} {lead.get('zip', '')}</td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Service</td><td style="padding: 8px;">{service}</td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Source</td><td style="padding: 8px;">{lead.get('traffic_source', 'unknown')}</td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Aspire Lead Source</td><td style="padding: 8px; font-weight: bold; color: #115E00;">{_aspire_lead_source(lead)}</td></tr>
</table>
<div style="background: #f5f5f5; padding: 12px; margin: 16px 0; border-left: 4px solid #C8A951;">
<strong>Message:</strong><br>{lead.get('message', '(no message)')[:400]}
</div>
<p style="font-size: 13px; color: #888;">Aspire: {aspire_line}<br>HubSpot: {hubspot_line}<br>Auto-reply sent to lead.</p>
</div>"""

    if send_html_email(
        api_key="",
        to_emails=owner_email,
        from_email=from_email,
        from_name=from_name,
        subject=f"New Lead Assigned: {name} - {service}",
        html_body=html,
    ):
        log(f"  Owner notification sent to {owner_email}")


def send_spam_notification(config, lead_data, reason):
    """Notify Evelin about suspected spam."""
    recipients = config["notifications"].get("spam_recipients", [])
    if not recipients:
        return

    fields = lead_data.get("additional_fields", {})
    name = fields.get("Name", "Unknown")
    email = lead_data.get("contact_email_address", "unknown")
    message = fields.get("Anything else you would like to share?", "")[:300]

    spam_body = f"<pre>Filtered spam lead from WhatConverts\n{'='*45}\n\nName: {name}\nEmail: {email}\nReason: {reason}\n\nMessage:\n{message}\n\n{'='*45}\nThis lead was NOT processed. If legitimate, add manually.</pre>"
    send_html_email(
        api_key="",
        to_emails=recipients,
        from_email=config["notifications"]["from_email"],
        from_name=config["notifications"]["from_name"],
        subject=f"SPAM Lead Filtered: {name}",
        html_body=spam_body,
    )


# --- Repeat Submission Notification ---

def send_repeat_notification(config, lead, original_owner_name):
    """Notify BOTH owners that a lead submitted the form again."""
    if DRY_RUN:
        return

    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Unknown"
    service = lead.get("service_interest", "General Inquiry")

    html = f"""<div style="font-family: Arial, sans-serif; max-width: 600px;">
<h2 style="color: #B8860B; margin-bottom: 4px;">Repeat Form Submission</h2>
<p style="color: #666; margin-top: 0;">This lead has submitted the contact form again.</p>
<hr style="border: 1px solid #C8A951;">
<table style="width: 100%; border-collapse: collapse;">
<tr><td style="padding: 8px; font-weight: bold; width: 160px;">Originally Assigned To</td><td style="padding: 8px; font-weight: bold; color: #B8860B;">{original_owner_name}</td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Name</td><td style="padding: 8px;">{name}</td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Phone</td><td style="padding: 8px;"><a href="tel:{lead.get('phone', '')}">{lead.get('phone', 'Not provided')}</a></td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Email</td><td style="padding: 8px;"><a href="mailto:{lead.get('email', '')}">{lead.get('email', 'Not provided')}</a></td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Address</td><td style="padding: 8px;">{lead.get('address', '')} {lead.get('city', '')} {lead.get('state', '')} {lead.get('zip', '')}</td></tr>
<tr style="background: #f9f9f9;"><td style="padding: 8px; font-weight: bold;">Service</td><td style="padding: 8px;">{service}</td></tr>
<tr><td style="padding: 8px; font-weight: bold;">Source</td><td style="padding: 8px;">{lead.get('traffic_source', 'unknown')}</td></tr>
</table>
<div style="background: #FFF8E7; padding: 12px; margin: 16px 0; border-left: 4px solid #B8860B;">
<strong>Message:</strong><br>{lead.get('message', '(no message)')[:400]}
</div>
<p style="font-size: 13px; color: #888;">This person already exists in HubSpot and Aspire. No duplicate was created.<br>They submitted the website form again, which may indicate they haven't been contacted yet.</p>
</div>"""

    # Send to both Evelin and Denisse
    recipients = config["notifications"].get("lead_recipients", [])
    if recipients and send_html_email(
        api_key="",
        to_emails=recipients,
        from_email=config["notifications"]["from_email"],
        from_name=config["notifications"]["from_name"],
        subject=f"Repeat Submission: {name} - {service} (assigned to {original_owner_name})",
        html_body=html,
    ):
        log(f"  Repeat notification sent to {recipients}")


# --- HubSpot CRM ---

def create_hubspot_contact(config, lead):
    """Create a contact and deal in HubSpot via the hubspot-sync.py script."""
    hubspot_cfg = config.get("hubspot", {})
    if not hubspot_cfg.get("enabled"):
        return None, None

    lead_email = (lead.get("email") or "").lower()
    for addr in OWN_ADDRESSES:
        if addr in lead_email:
            log(f"  Skipping HubSpot: own address ({lead_email})")
            return None, None

    if DRY_RUN:
        log(f"  DRY RUN: Would create HubSpot contact for {lead.get('email')}")
        return "dry-run", None

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hubspot-sync.py")
    if not os.path.exists(script_path):
        log(f"  ERROR: HubSpot sync script not found at {script_path}")
        return None, None

    lead_json = json.dumps(lead)
    try:
        import subprocess
        env = os.environ.copy()
        hs_token = hubspot_cfg.get("access_token") or os.environ.get("HUBSPOT_ACCESS_TOKEN")
        if hs_token:
            env["HUBSPOT_ACCESS_TOKEN"] = hs_token

        result = subprocess.run(
            ["python3", script_path, "--lead-json", lead_json],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.stdout.strip():
            response = json.loads(result.stdout.strip())
            action = response.get("action", "unknown")
            owner_id = response.get("owner_id", "")

            if action == "created":
                contact_url = response.get("contact_url", "")
                log(f"  HubSpot: Contact + deal created ({contact_url})")
                return contact_url or "created", owner_id
            elif action == "exists":
                log(f"  HubSpot: Contact already exists")
                return "exists", owner_id
            elif not response.get("success"):
                log(f"  HubSpot: {response.get('message', 'Unknown error')}")
                return None, None
            else:
                return action, owner_id
        else:
            stderr_tail = (result.stderr or "")[-300:]
            log(f"  ERROR: HubSpot sync returned no output. stderr: {stderr_tail}")
            return None, None
    except Exception as e:
        log(f"  ERROR: HubSpot sync failed: {e}")
        return None, None


# --- Mailchimp ---

def add_to_mailchimp(config, lead):
    """Add lead to Mailchimp audience with service-specific tag."""
    mc_cfg = config.get("mailchimp", {})
    if not mc_cfg.get("enabled"):
        log("  Mailchimp disabled. Skipping.")
        return None
    if DRY_RUN:
        log(f"  DRY RUN: Would add {lead.get('email', '(no email)')} to Mailchimp")
        return "dry-run"

    email = lead.get("email", "")
    if not email or "@" not in email:
        log("  Skipping Mailchimp: no valid email")
        return None

    api_key = mc_cfg.get("api_key", "")
    server = mc_cfg.get("server_prefix", "")
    list_id = mc_cfg.get("list_id", "")
    tag = mc_cfg.get("tag", "web-lead")

    if not api_key or not server or not list_id:
        log("  Mailchimp not fully configured (need api_key, server_prefix, list_id). Skipping.")
        return None

    email_hash = hashlib.md5(email.lower().encode()).hexdigest()
    url = f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/members/{email_hash}"

    tags = [tag]
    # Add source-specific tag for phone call leads
    if lead.get("source") == "phone_call":
        tags = ["phone-lead"]

    # Add service tag if detected
    service = lead.get("service_interest", "")
    if service and service != "General Inquiry":
        service_tag = re.sub(r"[^a-zA-Z0-9\s]", "", service).lower().replace(" ", "-")
        tags.append(service_tag)

    payload = {
        "email_address": email,
        "status_if_new": "subscribed",
        "merge_fields": {
            "FNAME": lead.get("first_name", ""),
            "LNAME": lead.get("last_name", ""),
            "PHONE": lead.get("phone", ""),
        },
        "tags": tags,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            status = result.get("status", "unknown")
            log(f"  Mailchimp: {email} -> {status} (tags: {tags})")
            return status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:300]
        log(f"  ERROR: Mailchimp API {e.code}: {error_body}")
        return None
    except Exception as e:
        log(f"  ERROR: Mailchimp request failed: {e}")
        return None


# --- Aspire CRM ---

def create_aspire_contact(config, lead):
    """Create a contact in Aspire via the aspire-api-sync.py script.
    Returns (url_or_status, contact_id) tuple."""
    aspire_cfg = config.get("aspire", {})
    if not aspire_cfg.get("enabled"):
        return None, None

    if DRY_RUN:
        log(f"  DRY RUN: Would create Aspire contact for {lead.get('email')}")
        return "dry-run", None

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aspire-api-sync.py")
    if not os.path.exists(script_path):
        log(f"  ERROR: Aspire API script not found at {script_path}")
        return None, None

    env = os.environ.copy()
    if aspire_cfg.get("api_client_id"):
        env["ASPIRE_CLIENT_ID"] = aspire_cfg["api_client_id"]
    if aspire_cfg.get("api_secret"):
        env["ASPIRE_SECRET"] = aspire_cfg["api_secret"]

    lead_json = json.dumps(lead)
    try:
        import subprocess
        result = subprocess.run(
            ["python3", script_path, "--lead-json", lead_json],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.stdout.strip():
            response = json.loads(result.stdout.strip())
            action = response.get("action", "unknown")
            contact_id = response.get("contact_id", "")
            if action == "created":
                contact_url = response.get("contact_url", "")
                log(f"  Aspire: Contact created ({contact_url})")
                return contact_url or "created", contact_id
            elif action == "exists":
                log(f"  Aspire: Contact already exists")
                return "exists", contact_id
            elif not response.get("success"):
                log(f"  Aspire: {response.get('message', 'Unknown error')}")
                return None, None
            else:
                return action, contact_id
        else:
            stderr_tail = (result.stderr or "")[-300:]
            log(f"  ERROR: Aspire returned no output. stderr: {stderr_tail}")
            return None, None
    except Exception as e:
        log(f"  ERROR: Aspire contact creation failed: {e}")
        return None, None


# --- Owner ID to Name/Email Mapping ---

OWNER_MAP = {
    "88710208": ("Evelin", "evelin@blackhilltx.com"),
    "162535167": ("Denisse", "denisse@blackhilltx.com"),
}


def get_owner_info(owner_id):
    """Map HubSpot owner ID to name and email."""
    return OWNER_MAP.get(str(owner_id), ("Team", "evelin@blackhilltx.com"))


# --- Main Processing ---

def process_leads(config, state):
    """Main processing loop: fetch WhatConverts leads and process new ones."""
    leads = fetch_recent_leads(config, lookback_minutes=130)
    if not leads:
        log("No recent leads found.")
        return

    forms = [l for l in leads if l.get("lead_type", "").lower() != "phone call"]
    calls = [l for l in leads if l.get("lead_type", "").lower() == "phone call"]
    log(f"Found {len(leads)} recent lead(s) ({len(forms)} forms, {len(calls)} calls)")
    processed_ids = state.get("processed_ids", [])

    # Track emails flagged as spam within this run
    spam_emails = set()

    for lead_data in leads:
        lead_id = str(lead_data.get("lead_id", ""))
        if not lead_id:
            continue

        # De-duplicate
        if lead_id in processed_ids:
            continue

        is_call = lead_data.get("lead_type", "").lower() == "phone call"
        lead_type_label = "CALL" if is_call else "FORM"

        # Log entry
        if is_call:
            caller = lead_data.get("caller_name") or lead_data.get("contact_phone_number") or "unknown"
            phone = lead_data.get("caller_number", "")
            duration = lead_data.get("call_duration", "")
            log(f"\nProcessing {lead_type_label}: {caller} ({phone}) [{duration}] [WC #{lead_id}]")
        else:
            fields = lead_data.get("additional_fields", {})
            if isinstance(fields, list):
                fields = {}
            name = fields.get("Name", "") or lead_data.get("contact_name", "unknown")
            email = lead_data.get("contact_email_address", "unknown")
            log(f"\nProcessing {lead_type_label}: {name} ({email}) [WC #{lead_id}]")

        # Check if this email was already flagged as spam in this run
        lead_email = (lead_data.get("contact_email_address") or "").strip().lower()
        if lead_email and lead_email in spam_emails:
            log(f"  SKIP: Email already flagged as spam in this run")
            processed_ids.append(lead_id)
            state["stats"]["total_spam"] = state["stats"].get("total_spam", 0) + 1
            continue

        # Spam check
        is_spam, spam_reason = is_spam_lead(lead_data)
        if is_spam:
            log(f"  SKIP: {spam_reason}")
            if lead_email:
                spam_emails.add(lead_email)
            if not is_call:
                send_spam_notification(config, lead_data, spam_reason)
            processed_ids.append(lead_id)
            state["stats"]["total_spam"] = state["stats"].get("total_spam", 0) + 1
            continue

        # WhatConverts duplicate flag (repeat visitor)
        if lead_data.get("duplicate", False):
            log(f"  Note: WhatConverts marked as duplicate/repeat (processing anyway)")

        # Parse into standard lead dict
        lead = parse_wc_lead(lead_data)
        log(f"  Parsed: {lead['first_name']} {lead['last_name']} | {lead['email'] or '(no email)'} | {lead['phone']} | {lead['service_interest']}")

        # Send auto-reply (forms only — callers already contacted us)
        if not is_call and lead.get("email") and "@" in lead.get("email", ""):
            send_auto_reply(config, lead)
        elif is_call:
            log("  Skipping auto-reply: phone call lead")
        else:
            log("  Skipping auto-reply: no valid email")

        # Aspire CRM
        aspire_url, aspire_contact_id = create_aspire_contact(config, lead)

        # Pass Aspire result into the lead so HubSpot's note reflects accurate status
        if aspire_url == "exists":
            lead["_aspire_status"] = "exists"
        elif aspire_url and aspire_url.startswith("http"):
            lead["_aspire_status"] = aspire_url
            lead["_aspire_url"] = aspire_url
        else:
            lead["_aspire_status"] = "not_created"

        # HubSpot CRM (returns owner_id for notification routing)
        hubspot_status, owner_id = create_hubspot_contact(config, lead)

        # Mailchimp (add to audience with service tag for drip campaigns)
        add_to_mailchimp(config, lead)

        # Check if this is a repeat submission (contact already existed)
        is_repeat = hubspot_status == "exists" or aspire_url == "exists"

        if is_repeat:
            # Repeat submission: notify BOTH owners with original assignee info
            original_owner_name = get_owner_info(owner_id)[0] if owner_id else "Unassigned"
            log(f"  Repeat submission detected (originally assigned to {original_owner_name})")
            send_repeat_notification(config, lead, original_owner_name)
        elif owner_id:
            # New lead: notify the assigned owner only
            owner_name, owner_email = get_owner_info(owner_id)
            send_owner_notification(config, lead, owner_name, owner_email,
                                    aspire_url=aspire_url, hubspot_status=hubspot_status)
        else:
            # Fallback: notify all lead recipients
            for recipient in config["notifications"].get("lead_recipients", []):
                send_owner_notification(config, lead, "Team", recipient,
                                        aspire_url=aspire_url, hubspot_status=hubspot_status)

        # Teams push notification for instant mobile alert
        teams_owner_name, teams_owner_email = get_owner_info(owner_id) if owner_id else ("Team", "")
        send_teams_notification(config, lead, aspire_url=aspire_url, hubspot_status=hubspot_status,
                                owner_name=teams_owner_name, owner_email=teams_owner_email)

        # Update state
        processed_ids.append(lead_id)
        state["stats"]["total_leads"] = state["stats"].get("total_leads", 0) + 1
        if is_call:
            state["stats"]["total_calls"] = state["stats"].get("total_calls", 0) + 1
        state["stats"]["last_lead"] = datetime.now().isoformat()

        # Store WC lead ID → Aspire contact ID mapping for ROI sync
        if aspire_contact_id:
            lead_mappings = state.setdefault("lead_mappings", {})
            lead_mappings[str(lead_id)] = {
                "aspire_contact_id": str(aspire_contact_id),
                "traffic_source": lead.get("traffic_source", ""),
                "service": lead.get("service_interest", ""),
                "lead_type": lead.get("source", "web_form"),
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }

        # Save state after each lead to prevent re-processing on timeout
        state["processed_ids"] = processed_ids
        if not DRY_RUN:
            save_state(state)

    state["processed_ids"] = processed_ids


# --- CLI Modes ---

def test_connection(config):
    """Test WhatConverts API connectivity."""
    log("Testing WhatConverts API connection...")
    result = wc_api_request(config, "/leads", {
        "profile_id": config["whatconverts"]["profile_id"],
        "leads_per_page": 3,
        "lead_type": "web_form",
    })
    if not result:
        log("FAILED: Could not reach WhatConverts API")
        sys.exit(1)

    total = result.get("total_leads", 0)
    log(f"API accessible: {total} total web form leads in profile")
    for lead in result.get("leads", [])[:3]:
        name = lead.get("additional_fields", {}).get("Name", "?")
        created = lead.get("date_created", "?")
        log(f"  {name} | {created}")

    log("\nConnection test PASSED")


# --- Entry Point ---

def main():
    log("WhatConverts lead monitor starting...")

    config = load_config()

    if "--test" in sys.argv:
        test_connection(config)
        return

    state = load_state()
    log(f"  State: {len(state.get('processed_ids', []))} processed, {state.get('stats', {}).get('total_leads', 0)} leads")

    process_leads(config, state)

    if not DRY_RUN:
        save_state(state)
        log(f"  State saved: {len(state['processed_ids'])} processed IDs")

    log("WhatConverts lead monitor complete.")


if __name__ == "__main__":
    main()
