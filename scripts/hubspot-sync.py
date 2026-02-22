#!/usr/bin/env python3
"""HubSpot CRM sync for Black Hill Landscaping lead monitor.

Creates contacts and deals in HubSpot when new leads come in.
Called by lead-monitor.py (cloud or local) after a lead is classified.

Usage:
  python3 hubspot-sync.py --lead-json '{"first_name":"Rick",...}'
  python3 hubspot-sync.py --test          # Test API connection
  python3 hubspot-sync.py --dry-run --lead-json '...'

Returns JSON to stdout:
  {"success": true, "action": "created", "contact_id": "123", "deal_id": "456", "contact_url": "..."}
  {"success": true, "action": "exists", "contact_id": "123"}
  {"success": false, "message": "error details"}
"""

import json, os, sys, urllib.request, urllib.error

CONFIG_FILE = os.path.expanduser("~/.config/hubspot/config.json")
DRY_RUN = "--dry-run" in sys.argv

# --- Config ---

def load_config():
    """Load HubSpot config. Supports local file or env var."""
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if token:
        return {"access_token": token}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return None


# --- API Helpers ---

def api_request(method, endpoint, data=None, token=None):
    """Make a HubSpot API request. Returns (response_dict, status_code)."""
    url = f"https://api.hubapi.com{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            response_body = resp.read().decode()
            return json.loads(response_body) if response_body else {}, resp.status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        try:
            return json.loads(error_body), e.code
        except Exception:
            return {"message": error_body or str(e)}, e.code
    except Exception as e:
        return {"message": str(e)}, 0


# --- Contact Operations ---

def search_contact_by_email(email, token):
    """Search for an existing contact by email. Returns contact ID or None."""
    data = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email
            }]
        }],
        "properties": ["firstname", "lastname", "email", "phone"],
        "limit": 1
    }
    resp, status = api_request("POST", "/crm/v3/objects/contacts/search", data, token)
    if status == 200 and resp.get("total", 0) > 0:
        return resp["results"][0]
    return None


def create_contact(lead, token):
    """Create a new HubSpot contact from lead data. Returns (contact_dict, status)."""
    properties = {
        "firstname": lead.get("first_name", ""),
        "lastname": lead.get("last_name", ""),
        "email": lead.get("email", ""),
        "phone": lead.get("phone", ""),
        "address": lead.get("address", ""),
        "city": lead.get("city", ""),
        "zip": lead.get("zip", ""),
        "hs_lead_status": "NEW",
    }

    # Map traffic source to HubSpot lead source
    traffic = lead.get("traffic_source", "").lower()
    if "google" in traffic and ("cpc" in traffic or "ads" in traffic):
        properties["hs_analytics_source"] = "PAID_SEARCH"
    elif "organic" in traffic:
        properties["hs_analytics_source"] = "ORGANIC_SEARCH"
    elif "direct" in traffic:
        properties["hs_analytics_source"] = "DIRECT_TRAFFIC"
    elif "referral" in traffic:
        properties["hs_analytics_source"] = "REFERRALS"

    # Clean empty values
    properties = {k: v for k, v in properties.items() if v}

    return api_request("POST", "/crm/v3/objects/contacts", {"properties": properties}, token)


def create_deal(lead, contact_id, token, pipeline_id="default"):
    """Create a deal associated with the contact."""
    service = lead.get("service_interest", "General Inquiry")
    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
    deal_name = f"{name} - {service}" if name else service

    properties = {
        "dealname": deal_name,
        "pipeline": pipeline_id,
        "dealstage": "appointmentscheduled",  # First stage in default pipeline
        "description": lead.get("message", "")[:1000],
    }

    # Create deal
    deal_resp, deal_status = api_request(
        "POST", "/crm/v3/objects/deals", {"properties": properties}, token
    )
    if deal_status not in (200, 201):
        return deal_resp, deal_status

    # Associate deal with contact
    deal_id = deal_resp.get("id")
    if deal_id and contact_id:
        api_request(
            "PUT",
            f"/crm/v3/objects/deals/{deal_id}/associations/contacts/{contact_id}/deal_to_contact",
            None, token
        )

    return deal_resp, deal_status


# --- Main ---

def process_lead(lead, token, portal_id=None):
    """Process a lead: dedup, create contact, create deal. Returns result dict."""
    email = lead.get("email", "").strip()

    # Check for existing contact by email
    if email:
        existing = search_contact_by_email(email, token)
        if existing:
            contact_id = existing["id"]
            contact_url = f"https://app-na2.hubspot.com/contacts/{portal_id or ''}/record/0-1/{contact_id}"
            return {
                "success": True,
                "action": "exists",
                "contact_id": contact_id,
                "contact_url": contact_url,
                "message": f"Contact already exists: {existing.get('properties', {}).get('firstname', '')} {existing.get('properties', {}).get('lastname', '')}"
            }

    # Create contact
    contact_resp, contact_status = create_contact(lead, token)
    if contact_status not in (200, 201):
        error_msg = contact_resp.get("message", "Unknown error")
        # Handle duplicate email conflict
        if contact_status == 409:
            return {
                "success": True,
                "action": "exists",
                "message": f"Contact already exists (conflict): {error_msg}"
            }
        return {"success": False, "message": f"Contact creation failed ({contact_status}): {error_msg}"}

    contact_id = contact_resp.get("id")
    contact_url = f"https://app-na2.hubspot.com/contacts/{portal_id or ''}/record/0-1/{contact_id}"

    # Create deal
    deal_resp, deal_status = create_deal(lead, contact_id, token)
    deal_id = deal_resp.get("id") if deal_status in (200, 201) else None
    deal_url = f"https://app-na2.hubspot.com/contacts/{portal_id or ''}/record/0-3/{deal_id}" if deal_id else None

    return {
        "success": True,
        "action": "created",
        "contact_id": contact_id,
        "contact_url": contact_url,
        "deal_id": deal_id,
        "deal_url": deal_url,
    }


def test_connection(token):
    """Test HubSpot API connectivity."""
    resp, status = api_request("GET", "/crm/v3/objects/contacts?limit=1", None, token)
    if status == 200:
        total = resp.get("total", len(resp.get("results", [])))
        print(json.dumps({"success": True, "message": f"Connected. {total} contact(s) in CRM."}))
    else:
        print(json.dumps({"success": False, "message": f"API error ({status}): {resp.get('message', '')}"}))
    return status == 200


if __name__ == "__main__":
    config = load_config()
    if not config:
        print(json.dumps({"success": False, "message": "No HubSpot config found"}))
        sys.exit(1)

    token = config["access_token"]
    portal_id = config.get("portal_id")

    if "--test" in sys.argv:
        ok = test_connection(token)
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

    result = process_lead(lead, token, portal_id)
    print(json.dumps(result))
    sys.exit(0 if result.get("success") else 1)
