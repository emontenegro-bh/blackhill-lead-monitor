#!/usr/bin/env python3
"""Fuzzy-match unmapped WhatConverts leads to Aspire CRM contacts.

Finds leads in processed-state.json that have no aspire_contact_id mapping,
then searches Aspire contacts using fuzzy name/phone/email matching.
Emails a digest of proposed matches for review.

Usage:
    python3 scripts/lead-fuzzy-match.py              # Run and email results
    python3 scripts/lead-fuzzy-match.py --dry-run     # Print results, don't email
    python3 scripts/lead-fuzzy-match.py --auto-link   # Auto-link high-confidence matches (>=0.90)
"""

import json, os, re, smtplib, sys, urllib.request, urllib.error
from datetime import datetime
from difflib import SequenceMatcher
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

DRY_RUN = "--dry-run" in sys.argv
AUTO_LINK = "--auto-link" in sys.argv
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "data")
STATE_FILE = os.path.join(DATA_DIR, "processed-state.json")

# Aspire config
ASPIRE_CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
ASPIRE_TOKEN_CACHE = os.path.expanduser("~/.config/aspire/api-token.json")

# WhatConverts config
WC_CONFIG_FILE = os.path.expanduser("~/.config/whatconverts/config.json")

# Thresholds
HIGH_CONFIDENCE = 0.90   # Auto-link if --auto-link
REVIEW_THRESHOLD = 0.65  # Show in review digest

# Known spam domains (from spam-detector.py)
SPAM_DOMAINS = {
    "consoleaidly.com", "parallelaid.com", "circuitprompt.com",
    "fusionescort.com", "vettedvas.com", "smartclerical.com",
    "sendproud.com",
}
SPAM_NAMES = {"test", "umair test"}

# Own company contacts to skip as match targets
OWN_EMAILS = {"evelin@blackhilltx.com", "denisse@blackhilltx.com"}
OWN_PHONES = {"8179946663", "8179950324", "5625475384"}

# Email
EMAIL_TO = "evelin@blackhilltx.com"
SENDGRID_SMTP = "smtp.sendgrid.net"
SENDGRID_PORT = 587


# --- Config ---

def load_aspire_config():
    client_id = os.environ.get("ASPIRE_CLIENT_ID")
    secret = os.environ.get("ASPIRE_SECRET")
    if client_id and secret:
        base = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
        return {"api_base_url": base, "api_client_id": client_id, "api_secret": secret}
    with open(ASPIRE_CONFIG_FILE) as f:
        return json.load(f)


def load_wc_config():
    token = os.environ.get("WC_API_TOKEN")
    secret = os.environ.get("WC_API_SECRET")
    profile = os.environ.get("WC_PROFILE_ID")
    if token and secret and profile:
        return {"api_token": token, "api_secret": secret, "profile_id": profile}
    with open(WC_CONFIG_FILE) as f:
        return json.load(f)


# --- Aspire API ---

def aspire_auth(config):
    base = config.get("api_base_url", "https://cloud-api.youraspire.com")
    url = f"{base}/Authorization"
    body = json.dumps({
        "ClientId": config["api_client_id"],
        "Secret": config["api_secret"],
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("Token", data.get("token", "")), base


def aspire_query(endpoint, token, base):
    # Split path and query, encode query params properly
    if "?" in endpoint:
        path, query = endpoint.split("?", 1)
        url = f"{base}{path}?{urllib.parse.quote(query, safe='=&$,')}"
    else:
        url = f"{base}{endpoint}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return [], e.code


def get_all_aspire_contacts(token, base):
    """Fetch all prospect contacts from Aspire for fuzzy matching."""
    contacts = []
    skip = 0
    while True:
        endpoint = (
            f"/Contacts?$select=ContactID,FirstName,LastName,Email,MobilePhone,HomePhone,OfficePhone,Notes"
            f"&$filter=Active eq true&$top=200&$skip={skip}"
        )
        resp, status = aspire_query(endpoint, token, base)
        if status != 200 or not isinstance(resp, list) or len(resp) == 0:
            break
        contacts.extend(resp)
        if len(resp) < 200:
            break
        skip += 200
    return contacts


# --- WhatConverts API ---

def get_wc_lead(lead_id, wc_config):
    """Fetch a single lead from WhatConverts."""
    url = f"https://app.whatconverts.com/api/v1/leads/{lead_id}"
    import base64
    creds = base64.b64encode(f"{wc_config['api_token']}:{wc_config['api_secret']}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("leads", [data])[0] if "leads" in data else data
    except Exception:
        return None


# --- Fuzzy Matching ---

def normalize_phone(phone):
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def fuzzy_name_score(name_a, name_b):
    a = (name_a or "").lower().strip()
    b = (name_b or "").lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Try reversed: "John Doe" vs "Doe John"
    parts_b = b.split(None, 1)
    if len(parts_b) == 2 and a == f"{parts_b[1]} {parts_b[0]}":
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def phone_match(phone_a, phone_b):
    a = normalize_phone(phone_a)
    b = normalize_phone(phone_b)
    if not a or not b or len(a) < 10 or len(b) < 10:
        return 0.0
    if a == b:
        return 1.0
    # Allow 1 digit off
    if len(a) == len(b) == 10:
        diffs = sum(1 for x, y in zip(a, b) if x != y)
        if diffs == 1:
            return 0.85
    return 0.0


def normalize_address(addr):
    """Normalize address for comparison: lowercase, strip common suffixes."""
    if not addr:
        return ""
    a = addr.lower().strip()
    # Standardize common abbreviations
    for full, abbr in [("street", "st"), ("drive", "dr"), ("avenue", "ave"),
                        ("boulevard", "blvd"), ("lane", "ln"), ("court", "ct"),
                        ("circle", "cir"), ("road", "rd"), ("place", "pl")]:
        a = re.sub(rf"\b{full}\b", abbr, a)
        a = re.sub(rf"\b{abbr}\.\b", abbr, a)
    return a


def address_match(lead_address, aspire_notes):
    """Check if lead address appears in Aspire contact Notes field."""
    if not lead_address or not aspire_notes:
        return 0.0
    addr = normalize_address(lead_address)
    notes = aspire_notes.lower()

    # Extract street number + name from lead address
    m = re.match(r"(\d+)\s+(.+)", addr)
    if not m:
        return 0.0
    street_num = m.group(1)
    street_name = m.group(2).split(",")[0].strip()  # Before city

    # Check if street number and first word of street name appear in notes
    if street_num in notes and street_name.split()[0] in notes:
        return 0.90
    # Just street number match (weaker)
    if street_num in notes and len(street_num) >= 4:
        return 0.50
    return 0.0


def score_match(lead, aspire_contact):
    """Score a lead against an Aspire contact. Returns (score, method)."""
    scores = []

    # Email match (strongest signal)
    lead_email = (lead.get("email") or "").lower().strip()
    aspire_email = (aspire_contact.get("Email") or "").lower().strip()
    if lead_email and aspire_email and lead_email == aspire_email:
        return 1.0, "exact_email"

    # Phone match
    lead_phone = lead.get("phone", "")
    for field in ["MobilePhone", "HomePhone", "OfficePhone"]:
        ps = phone_match(lead_phone, aspire_contact.get(field, ""))
        if ps > 0:
            scores.append((ps, f"phone_{field}"))

    # Name match
    lead_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
    aspire_name = f"{aspire_contact.get('FirstName', '')} {aspire_contact.get('LastName', '')}".strip()
    ns = fuzzy_name_score(lead_name, aspire_name)
    if ns > 0.5:
        scores.append((ns, "name"))

    # Address match (check lead address against Aspire Notes field)
    lead_addr = lead.get("address", "")
    aspire_notes = aspire_contact.get("Notes", "") or ""
    addr_score = address_match(lead_addr, aspire_notes)
    if addr_score > 0:
        scores.append((addr_score, "address"))

    if not scores:
        return 0.0, "none"

    # Combine: best single score, boosted if multiple signals agree
    best_score, best_method = max(scores, key=lambda x: x[0])
    if len(scores) > 1:
        second_score = sorted(scores, key=lambda x: x[0], reverse=True)[1][0]
        if second_score > 0.6:
            best_score = min(1.0, best_score + 0.1)
            best_method += "+multi"

    return best_score, best_method


# --- Main ---

def find_unmapped_leads(state):
    """Find WC lead IDs that are processed but not in lead_mappings."""
    mappings = state.get("lead_mappings", {})
    # WC lead IDs are numeric strings in lead_mappings
    mapped_wc_ids = set(mappings.keys())

    # processed_ids contains email message IDs (MS Graph), not WC IDs
    # The WC IDs are stored as keys in lead_mappings
    # We need to find WC leads that were processed but didn't get mapped

    # Actually, the lead_mappings keys ARE WC lead IDs for matched leads
    # Unmapped = processed WC leads that aren't keys in lead_mappings
    # But processed_ids are MS Graph message IDs, not WC lead IDs...
    # The WC lead IDs come from the whatconverts-lead-monitor's own tracking

    # Better approach: query WC for recent leads and check which ones
    # don't have a mapping
    return mapped_wc_ids


def run():
    print("Lead Fuzzy Matcher")
    print("=" * 50)

    # Load state
    with open(STATE_FILE) as f:
        state = json.load(f)

    mappings = state.get("lead_mappings", {})
    mapped_wc_ids = set(mappings.keys())
    print(f"Currently mapped: {len(mapped_wc_ids)} leads")

    # Load configs
    aspire_config = load_aspire_config()
    wc_config = load_wc_config()

    # Auth to Aspire
    print("Authenticating to Aspire...")
    token, base = aspire_auth(aspire_config)

    # Fetch all Aspire contacts for fuzzy matching
    print("Fetching Aspire contacts...")
    contacts = get_all_aspire_contacts(token, base)
    print(f"Loaded {len(contacts)} Aspire contacts")

    # Fetch recent WC leads (last 90 days)
    print("Fetching WhatConverts leads...")
    import base64
    from datetime import timedelta as td
    creds = base64.b64encode(f"{wc_config['api_token']}:{wc_config['api_secret']}".encode()).decode()
    start_date = (datetime.now() - td(days=90)).strftime("%Y-%m-%d")
    wc_leads = []
    page = 1
    while True:
        url = (
            f"https://app.whatconverts.com/api/v1/leads"
            f"?profile_id={wc_config['profile_id']}"
            f"&lead_type=web_form"
            f"&start_date={start_date}"
            f"&leads_per_page=250&page_number={page}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        leads = data.get("leads", [])
        if not leads:
            break
        wc_leads.extend(leads)
        if page >= int(data.get("total_pages", 1)):
            break
        page += 1
    print(f"Fetched {len(wc_leads)} WhatConverts leads (since {start_date})")

    # Find unmapped
    unmapped = []
    for lead in wc_leads:
        wc_id = str(lead.get("lead_id", ""))
        if wc_id and wc_id not in mapped_wc_ids:
            # Parse lead into standard form
            raw_additional = lead.get("additional_fields", {})
            if isinstance(raw_additional, dict):
                additional = raw_additional
            elif isinstance(raw_additional, list):
                additional = {}
                for field in raw_additional:
                    if isinstance(field, dict):
                        additional[field.get("field_name", "")] = field.get("field_value", "")
            else:
                additional = {}

            name = lead.get("contact_name", "") or additional.get("Name", "")
            parts = name.strip().split(None, 1)
            parsed = {
                "wc_id": wc_id,
                "first_name": parts[0] if parts else "",
                "last_name": parts[1] if len(parts) > 1 else "",
                "email": lead.get("contact_email_address", "") or additional.get("Email", ""),
                "phone": lead.get("contact_phone_number", "") or additional.get("Contact No", ""),
                "address": additional.get("Address", "") or lead.get("mapped_address", ""),
                "city": additional.get("City", "") or lead.get("mapped_city", ""),
                "date": lead.get("date_created", "")[:10],
                "source": f"{lead.get('lead_source', '')} / {lead.get('lead_medium', '')}",
            }
            # Skip spam
            email_domain = (parsed["email"].split("@")[1] if "@" in parsed["email"] else "").lower()
            full_name = f"{parsed['first_name']} {parsed['last_name']}".strip().lower()
            if email_domain in SPAM_DOMAINS or full_name in SPAM_NAMES:
                continue

            if parsed["first_name"] or parsed["email"] or parsed["phone"]:
                unmapped.append(parsed)

    print(f"Unmapped leads with contact info: {len(unmapped)}")

    if not unmapped:
        print("No unmapped leads to process.")
        return

    # Fuzzy match each unmapped lead
    results = {"high": [], "review": [], "no_match": []}
    for lead in unmapped:
        best_score = 0.0
        best_contact = None
        best_method = "none"

        for contact in contacts:
            # Skip own company contacts
            c_email = (contact.get("Email") or "").lower()
            c_phone = normalize_phone(contact.get("MobilePhone", ""))
            if c_email in OWN_EMAILS or c_phone in OWN_PHONES:
                continue

            score, method = score_match(lead, contact)
            if score > best_score:
                best_score = score
                best_contact = contact
                best_method = method

        lead_name = f"{lead['first_name']} {lead['last_name']}".strip()

        if best_score >= HIGH_CONFIDENCE:
            results["high"].append({
                "lead": lead, "contact": best_contact,
                "score": best_score, "method": best_method,
            })
        elif best_score >= REVIEW_THRESHOLD:
            results["review"].append({
                "lead": lead, "contact": best_contact,
                "score": best_score, "method": best_method,
            })
        else:
            results["no_match"].append({"lead": lead, "score": best_score})

    # Print summary
    print(f"\nResults:")
    print(f"  High confidence (>={HIGH_CONFIDENCE}): {len(results['high'])}")
    print(f"  Needs review ({REVIEW_THRESHOLD}-{HIGH_CONFIDENCE}): {len(results['review'])}")
    print(f"  No match (<{REVIEW_THRESHOLD}): {len(results['no_match'])}")

    # Auto-link high confidence matches
    linked = 0
    if AUTO_LINK and results["high"]:
        print(f"\nAuto-linking {len(results['high'])} high-confidence matches...")
        for match in results["high"]:
            wc_id = match["lead"]["wc_id"]
            contact_id = str(match["contact"]["ContactID"])
            state["lead_mappings"][wc_id] = {
                "aspire_contact_id": contact_id,
                "traffic_source": match["lead"]["source"],
                "service": "fuzzy-matched",
                "lead_type": "web_form",
                "date": match["lead"]["date"],
                "match_score": match["score"],
                "match_method": match["method"],
            }
            linked += 1
            lead_name = f"{match['lead']['first_name']} {match['lead']['last_name']}".strip()
            aspire_name = f"{match['contact']['FirstName']} {match['contact']['LastName']}".strip()
            print(f"  Linked: {lead_name} -> {aspire_name} ({match['score']:.0%})")

        if linked and not DRY_RUN:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            print(f"Saved {linked} new mappings to state.")

    # Build email digest
    if not results["high"] and not results["review"]:
        print("No matches to report.")
        return

    lines = ["Lead Fuzzy Match Report", "=" * 40, ""]

    if results["high"]:
        action = "AUTO-LINKED" if AUTO_LINK else "HIGH CONFIDENCE"
        lines.append(f"--- {action} ({len(results['high'])}) ---")
        for m in results["high"]:
            ln = f"{m['lead']['first_name']} {m['lead']['last_name']}".strip()
            an = f"{m['contact']['FirstName']} {m['contact']['LastName']}".strip()
            lines.append(f"  {ln} -> {an} ({m['score']:.0%}, {m['method']})")
        lines.append("")

    if results["review"]:
        lines.append(f"--- NEEDS REVIEW ({len(results['review'])}) ---")
        for m in results["review"]:
            ln = f"{m['lead']['first_name']} {m['lead']['last_name']}".strip()
            an = f"{m['contact']['FirstName']} {m['contact']['LastName']}".strip()
            ae = m['contact'].get('Email', '')
            lines.append(f"  {ln} ({m['lead'].get('email','')}) -> {an} ({ae})")
            lines.append(f"    Score: {m['score']:.0%} via {m['method']} | WC#{m['lead']['wc_id']}")
        lines.append("")

    lines.append(f"No match: {len(results['no_match'])} leads")
    report = "\n".join(lines)
    print(f"\n{report}")

    if DRY_RUN:
        print("\n[DRY RUN - email not sent]")
        return

    # Send email
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    if not api_key:
        key_file = os.path.expanduser("~/.config/sendgrid-api-key")
        if os.path.exists(key_file):
            with open(key_file) as f:
                api_key = f.read().strip()
    if not api_key:
        print("No SendGrid API key. Report printed but not emailed.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Lead Fuzzy Match: {len(results['high'])} auto, {len(results['review'])} review"
    msg["From"] = formataddr(("Black Hill Assistant", EMAIL_TO))
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(report, "plain"))

    try:
        with smtplib.SMTP(SENDGRID_SMTP, SENDGRID_PORT) as s:
            s.starttls()
            s.login("apikey", api_key)
            s.sendmail(EMAIL_TO, EMAIL_TO, msg.as_string())
        print("Report emailed.")
    except Exception as e:
        print(f"Email failed: {e}")


if __name__ == "__main__":
    run()
