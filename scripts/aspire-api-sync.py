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
        safe_chars = "=&$,'()"
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
    service = lead.get("service_interest", "")
    if service:
        notes_parts.append(f"Service: {service}")
    message = lead.get("message", "").strip()
    if message:
        notes_parts.append(message)
    source = lead.get("traffic_source", "") or lead.get("source", "")
    if source:
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

def process_lead(lead, config, token):
    """Process a lead: dedup, create contact. Returns result dict."""
    email = lead.get("email", "").strip()
    first_name = lead.get("first_name", "").strip()
    last_name = lead.get("last_name", "").strip()

    # Dedup by email first
    if email:
        existing = search_contact_by_email(email, config, token)
        if existing:
            cid = existing.get("ContactID", "")
            contact_url = f"{ASPIRE_PORTAL}/app/contacts/{cid}" if cid else ""
            return {
                "success": True,
                "action": "exists",
                "contact_id": str(cid),
                "contact_url": contact_url,
                "message": f"Contact already exists: {existing.get('FirstName', '')} {existing.get('LastName', '')}",
            }

    # Dedup by name
    if first_name and last_name:
        existing = search_contact_by_name(first_name, last_name, config, token)
        if existing:
            cid = existing.get("ContactID", "")
            contact_url = f"{ASPIRE_PORTAL}/app/contacts/{cid}" if cid else ""
            return {
                "success": True,
                "action": "exists",
                "contact_id": str(cid),
                "contact_url": contact_url,
                "message": f"Contact already exists: {existing.get('FirstName', '')} {existing.get('LastName', '')}",
            }

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
        return {
            "success": True,
            "action": "created",
            "contact_id": contact_id,
            "contact_url": contact_url,
        }

    # Error
    error_msg = ""
    if isinstance(resp, dict):
        error_msg = resp.get("message", resp.get("Message", resp.get("title", str(resp))))
    else:
        error_msg = str(resp)
    return {"success": False, "message": f"Contact creation failed ({status}): {error_msg}"}


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
