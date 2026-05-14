#!/usr/bin/env python3
"""Aspire CRM sync via REST API for Black Hill Landscaping lead monitor.

Creates contacts in Aspire when new leads come in. Uses the Aspire REST API
instead of Playwright browser automation.

Usage:
  python3 aspire-api-sync.py --lead-json '{"first_name":"Rick",...}'
  python3 aspire-api-sync.py --test          # Test API connection
  python3 aspire-api-sync.py --dry-run --lead-json '...'

Returns JSON to stdout:
  {"success": true, "action": "created", "contact_id": "123", "contact_url": "..."}
  {"success": true, "action": "exists", "contact_id": "123"}
  {"success": false, "message": "error details"}
"""

import json, os, sys, urllib.request, urllib.error, urllib.parse, re

CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
TOKEN_CACHE = os.path.expanduser("~/.config/aspire/api-token.json")
DRY_RUN = "--dry-run" in sys.argv

# Known IDs (looked up 2026-02-24)
CONTACT_TYPE_PROSPECT = 8
OWNER_EVELIN_CONTACT_ID = 6

# ContactCustomFieldDefinitionID for the "Lead Source" picklist (looked up 2026-05-13)
LEAD_SOURCE_DEFINITION_ID = 34

ASPIRE_PORTAL = "https://cloud.youraspire.com"


# --- Config ---

def load_config():
    """Load Aspire API config. Supports local file or env vars."""
    client_id = os.environ.get("ASPIRE_CLIENT_ID")
    secret = os.environ.get("ASPIRE_SECRET")
    if client_id and secret:
        base = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
        return {"api_base_url": base, "api_client_id": client_id, "api_secret": secret}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return None


# --- Auth ---

def authenticate(config):
    """Get JWT token from Aspire API. Caches token locally."""
    # Try cached token first (local only)
    if os.path.exists(TOKEN_CACHE):
        try:
            with open(TOKEN_CACHE) as f:
                cached = json.load(f)
            token = cached.get("token", "")
            if token and _test_token(config, token):
                return token
        except Exception:
            pass

    base = config["api_base_url"]
    data = json.dumps({
        "ClientId": config["api_client_id"],
        "Secret": config["api_secret"],
    }).encode()

    req = urllib.request.Request(
        f"{base}/Authorization",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            token = body.get("Token", "")
            if not token:
                return None

            # Cache token locally
            try:
                os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
                with open(TOKEN_CACHE, "w") as f:
                    json.dump({"token": token, "refresh": body.get("RefreshToken", "")}, f)
            except Exception:
                pass

            return token
    except urllib.error.HTTPError as e:
        err = e.read().decode() if e.fp else str(e)
        return None


def _test_token(config, token):
    """Quick check if a cached token is still valid."""
    base = config["api_base_url"]
    req = urllib.request.Request(
        f"{base}/ContactTypes?$top=1",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status == 200
    except Exception:
        return False


# --- API Helpers ---

def api_request(method, endpoint, config, token, data=None):
    """Make an Aspire API request. Returns (response_dict_or_list, status_code)."""
    base = config["api_base_url"]
    # Split path and query, encode query params properly
    if "?" in endpoint:
        path, query = endpoint.split("?", 1)
        safe_chars = "=&$,'()@"
        encoded_query = urllib.parse.quote(query, safe=safe_chars)
        url = f"{base}{path}?{encoded_query}"
    else:
        url = f"{base}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}, resp.status
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        try:
            return json.loads(err_body), e.code
        except Exception:
            return {"message": err_body or str(e)}, e.code
    except Exception as e:
        return {"message": str(e)}, 0


# --- Contact Operations ---

def search_contact_by_email(email, config, token):
    """Search for existing contact by email. Returns contact dict or None."""
    if not email:
        return None
    safe_email = email.replace("'", "''")
    endpoint = f"/Contacts?$filter=Email eq '{safe_email}'&$top=1"
    resp, status = api_request("GET", endpoint, config, token)
    if status == 200 and isinstance(resp, list) and len(resp) > 0:
        return resp[0]
    return None


def search_contact_by_name(first_name, last_name, config, token):
    """Search for existing contact by name. Returns contact dict or None."""
    if not last_name:
        return None
    safe_last = last_name.replace("'", "''")
    endpoint = f"/Contacts?$filter=LastName eq '{safe_last}'&$top=10"
    if first_name:
        safe_first = first_name.replace("'", "''")
        endpoint = f"/Contacts?$filter=FirstName eq '{safe_first}' and LastName eq '{safe_last}'&$top=5"
    resp, status = api_request("GET", endpoint, config, token)
    if status == 200 and isinstance(resp, list) and len(resp) > 0:
        return resp[0]
    return None


def search_contact_by_phone(phone, config, token):
    """Search for existing contact by mobile phone. Returns contact dict or None."""
    if not phone:
        return None
    formatted = format_phone(phone)
    if not formatted:
        return None
    safe_phone = formatted.replace("'", "''")
    endpoint = f"/Contacts?$filter=MobilePhone eq '{safe_phone}'&$top=1"
    resp, status = api_request("GET", endpoint, config, token)
    if status == 200 and isinstance(resp, list) and len(resp) > 0:
        return resp[0]
    return None


def format_phone(phone):
    """Format phone to XXX-XXX-XXXX for Aspire."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return phone


def create_contact(lead, config, token):
    """Create a new contact in Aspire. Returns (response, status)."""
    contact_data = {
        "Contact": {
            "FirstName": lead.get("first_name", ""),
            "LastName": lead.get("last_name", ""),
            "Email": lead.get("email", ""),
            "MobilePhone": format_phone(lead.get("phone", "")),
            "ContactTypeID": CONTACT_TYPE_PROSPECT,
            "OwnerContactID": OWNER_EVELIN_CONTACT_ID,
            "Active": True,
        },
    }

    # Add notes
    notes_parts = []
    lead_source_type = lead.get("source", "web_form")
    if lead_source_type == "phone_call":
        notes_parts.append("Phone Call Lead")
    else:
        notes_parts.append("Web Lead")
    service = lead.get("service_interest", "")
    if service and service != "General Inquiry":
        notes_parts.append(f"Service: {service}")
    message = lead.get("message", "").strip()
    if message:
        notes_parts.append(message)
    source = lead.get("traffic_source", "") or lead.get("source", "")
    if source and source not in ("web_form", "phone_call"):
        notes_parts.append(f"Source: {source}")
    received = lead.get("received_at", "")
    if received:
        notes_parts.append(f"Received: {received}")
    if notes_parts:
        contact_data["Contact"]["Notes"] = "\n".join(notes_parts)

    # Add address if available
    address = lead.get("address", "").strip()
    city = lead.get("city", "").strip()
    zipcode = lead.get("zip", "").strip()
    if address or city or zipcode:
        contact_data["OfficeAddress"] = {
            "AddressLine1": address,
            "City": city,
            "StateProvinceCode": "TX",
            "ZipCode": zipcode,
        }

    return api_request("POST", "/Contacts", config, token, contact_data)


# --- Main ---

def _stamp_attribution(contact_id, lead, config, token):
    """Stamp the Lead Source custom field + append attribution note. Best-effort, swallows errors into result."""
    out = {}
    value = lead.get("lead_source_aspire")
    if value:
        ok, msg = set_lead_source(contact_id, value, config, token)
        out["lead_source"] = msg if ok else f"FAILED: {msg}"
    note = lead.get("attribution_note")
    if note:
        ok, msg = append_contact_note(contact_id, note, config, token)
        out["note"] = msg if ok else f"FAILED: {msg}"
    return out


def process_lead(lead, config, token):
    """Process a lead: dedup, create contact, stamp lead source + attribution. Returns result dict."""
    email = lead.get("email", "").strip()
    first_name = lead.get("first_name", "").strip()
    last_name = lead.get("last_name", "").strip()
    phone = lead.get("phone", "").strip()

    def _exists_response(existing):
        cid = existing.get("ContactID", "")
        contact_url = f"{ASPIRE_PORTAL}/app/contacts/{cid}" if cid else ""
        result = {
            "success": True,
            "action": "exists",
            "contact_id": str(cid),
            "contact_url": contact_url,
            "message": f"Contact already exists: {existing.get('FirstName', '')} {existing.get('LastName', '')}",
        }
        if cid:
            result["attribution"] = _stamp_attribution(cid, lead, config, token)
        return result

    # Dedup by email first
    if email:
        existing = search_contact_by_email(email, config, token)
        if existing:
            return _exists_response(existing)

    # Dedup by phone (important for call leads which have no email)
    if phone:
        existing = search_contact_by_phone(phone, config, token)
        if existing:
            return _exists_response(existing)

    # Dedup by name
    if first_name and last_name:
        existing = search_contact_by_name(first_name, last_name, config, token)
        if existing:
            return _exists_response(existing)

    # Create contact
    resp, status = create_contact(lead, config, token)
    if status in (200, 201):
        # Response is typically the new contact ID as a string or object
        contact_id = ""
        if isinstance(resp, dict):
            contact_id = str(resp.get("ContactID", resp.get("Id", resp.get("id", ""))))
        elif isinstance(resp, (int, str)):
            contact_id = str(resp)

        contact_url = f"{ASPIRE_PORTAL}/app/contacts/{contact_id}" if contact_id else ""
        result = {
            "success": True,
            "action": "created",
            "contact_id": contact_id,
            "contact_url": contact_url,
        }
        if contact_id:
            result["attribution"] = _stamp_attribution(contact_id, lead, config, token)
        return result

    # Error
    error_msg = ""
    if isinstance(resp, dict):
        error_msg = resp.get("message", resp.get("Message", resp.get("title", str(resp))))
    else:
        error_msg = str(resp)
    return {"success": False, "message": f"Contact creation failed ({status}): {error_msg}"}


# --- Lead Source Custom Field ---

def get_lead_source_row(contact_id, config, token):
    """Return existing /ContactCustomFields row for this contact + Lead Source definition, or None."""
    endpoint = (
        f"/ContactCustomFields?$filter=ContactID eq {int(contact_id)} and "
        f"ContactCustomFieldDefinitionID eq {LEAD_SOURCE_DEFINITION_ID}&$top=1"
    )
    resp, status = api_request("GET", endpoint, config, token)
    if status == 200 and isinstance(resp, list) and resp:
        return resp[0]
    return None


def set_lead_source(contact_id, value, config, token):
    """Upsert the Lead Source custom field on a contact. Returns (success, message)."""
    existing = get_lead_source_row(contact_id, config, token)
    body = {
        "ContactID": int(contact_id),
        "ContactCustomFieldDefinitionID": LEAD_SOURCE_DEFINITION_ID,
        "ColumnValue": value,
    }
    if existing:
        body["ContactCustomFieldValueID"] = existing["ContactCustomFieldValueID"]
        resp, status = api_request("PUT", "/ContactCustomFields", config, token, body)
    else:
        resp, status = api_request("POST", "/ContactCustomFields", config, token, body)
    if status in (200, 201):
        return True, f"Lead Source set to '{value}'"
    return False, f"Lead Source write failed ({status}): {resp}"


def append_contact_note(contact_id, note_line, config, token):
    """Append a single line to the contact's Notes field. Idempotent: skips if line already present.

    Aspire's PUT /Contacts rejects partial updates — it requires First/Last name in the body — so
    we fetch the contact via $filter and round-trip the required fields.
    """
    resp_list, status = api_request(
        "GET", f"/Contacts?$filter=ContactID eq {int(contact_id)}&$top=1", config, token
    )
    if status != 200 or not isinstance(resp_list, list) or not resp_list:
        return False, f"Could not fetch contact ({status})"
    contact = resp_list[0]
    existing_notes = (contact.get("Notes") or "").rstrip()
    needle = note_line.strip()
    if needle and needle in existing_notes:
        return True, "Note already present"
    new_notes = f"{existing_notes}\n{note_line}" if existing_notes else note_line
    body = {"Contact": {
        "ContactID": int(contact_id),
        "FirstName": contact.get("FirstName") or "",
        "LastName": contact.get("LastName") or "",
        "Email": contact.get("Email") or "",
        "MobilePhone": contact.get("MobilePhone") or "",
        "ContactTypeID": contact.get("ContactTypeID"),
        "Active": contact.get("Active", True),
        "Notes": new_notes,
    }}
    _, st = api_request("PUT", "/Contacts", config, token, body)
    if st in (200, 201, 204):
        return True, "Note appended"
    return False, f"Note append failed ({st})"


def update_lead_source_by_phone(phone, value, note_line, config, token):
    """Find contact by phone, stamp Lead Source, optionally append note. Returns result dict."""
    contact = search_contact_by_phone(phone, config, token)
    if not contact:
        return {"success": False, "action": "not_found", "message": f"No Aspire contact found for {phone}"}
    cid = contact.get("ContactID")
    if not cid:
        return {"success": False, "action": "not_found", "message": "Contact found but missing ContactID"}
    ls_ok, ls_msg = set_lead_source(cid, value, config, token)
    note_ok, note_msg = (True, "skipped")
    if note_line:
        note_ok, note_msg = append_contact_note(cid, note_line, config, token)
    return {
        "success": ls_ok and note_ok,
        "action": "updated",
        "contact_id": str(cid),
        "contact_url": f"{ASPIRE_PORTAL}/app/contacts/{cid}",
        "lead_source": ls_msg,
        "note": note_msg,
    }


# --- Test ---

def test_connection(config):
    """Test Aspire API connectivity."""
    token = authenticate(config)
    if not token:
        print(json.dumps({"success": False, "message": "Authentication failed"}))
        return False

    # Test contact types
    resp, status = api_request("GET", "/ContactTypes", config, token)
    if status == 200 and isinstance(resp, list):
        types = [t.get("ContactTypeName", "") for t in resp]
        print(json.dumps({
            "success": True,
            "message": f"Connected. Contact types: {', '.join(types)}",
        }))
        return True

    print(json.dumps({"success": False, "message": f"API error ({status})"}))
    return False


if __name__ == "__main__":
    config = load_config()
    if not config or not config.get("api_client_id"):
        print(json.dumps({"success": False, "message": "No Aspire API config found"}))
        sys.exit(1)

    if "--test" in sys.argv:
        ok = test_connection(config)
        sys.exit(0 if ok else 1)

    # --update-lead-source mode: stamp the Lead Source custom field on an existing contact
    # (used by the WC monitor for phone calls, which the branch admin enters into Aspire manually).
    if "--update-lead-source" in sys.argv:
        def _arg(name):
            for i, a in enumerate(sys.argv):
                if a == name and i + 1 < len(sys.argv):
                    return sys.argv[i + 1]
            return None
        phone = _arg("--phone")
        value = _arg("--value")
        note = _arg("--note") or ""
        if not phone or not value:
            print(json.dumps({"success": False, "message": "--phone and --value are required"}))
            sys.exit(1)
        if DRY_RUN:
            print(json.dumps({"success": True, "action": "dry_run", "message": f"Would stamp {value} for {phone}"}))
            sys.exit(0)
        token = authenticate(config)
        if not token:
            print(json.dumps({"success": False, "message": "Authentication failed"}))
            sys.exit(1)
        result = update_lead_source_by_phone(phone, value, note, config, token)
        print(json.dumps(result))
        sys.exit(0 if result.get("success") else 1)

    # Parse lead from CLI arg
    lead_json = None
    for i, arg in enumerate(sys.argv):
        if arg == "--lead-json" and i + 1 < len(sys.argv):
            lead_json = sys.argv[i + 1]
            break

    if not lead_json:
        print(json.dumps({"success": False, "message": "No --lead-json provided"}))
        sys.exit(1)

    try:
        lead = json.loads(lead_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"success": False, "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    if DRY_RUN:
        desc = lead.get("email") or f"{lead.get('first_name', '')} {lead.get('last_name', '')}"
        print(json.dumps({"success": True, "action": "dry_run", "message": f"Would create contact for {desc}"}))
        sys.exit(0)

    # Authenticate
    token = authenticate(config)
    if not token:
        print(json.dumps({"success": False, "message": "Authentication failed"}))
        sys.exit(1)

    result = process_lead(lead, config, token)
    print(json.dumps(result))
    sys.exit(0 if result.get("success") else 1)
