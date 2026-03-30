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
from datetime import datetime, timezone

CONFIG_FILE = os.path.expanduser("~/.config/hubspot/config.json")
ROUND_ROBIN_FILE = os.path.expanduser("~/.config/hubspot/round-robin.json")
DRY_RUN = "--dry-run" in sys.argv

# --- Owner Assignment ---

OWNER_EVELIN = "88710208"
OWNER_DENISSE = "162535167"

def get_round_robin_next():
    """Get next owner in round robin rotation. Returns owner ID."""
    owners = [OWNER_EVELIN, OWNER_DENISSE]
    owners = [o for o in owners if o]  # Skip None until Denisse is added
    if len(owners) < 2:
        return owners[0] if owners else None

    try:
        with open(ROUND_ROBIN_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"last_index": -1}

    next_index = (state.get("last_index", -1) + 1) % len(owners)
    state["last_index"] = next_index

    try:
        os.makedirs(os.path.dirname(ROUND_ROBIN_FILE), exist_ok=True)
        with open(ROUND_ROBIN_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

    return owners[next_index]


def assign_owner(lead):
    """Determine deal owner based on service interest.

    Rules:
      - Irrigation -> Denisse
      - Commercial Maintenance -> Evelin
      - Everything else -> Round robin
    """
    service = (lead.get("service_interest", "") or "").lower()
    message = (lead.get("message", "") or "").lower()

    # Irrigation -> Denisse
    if "irrigation" in service or "sprinkler" in service or "irrigation" in message:
        return OWNER_DENISSE

    # Commercial Maintenance -> Evelin
    if "commercial" in service and "maint" in service:
        return OWNER_EVELIN

    # Everything else -> round robin
    return get_round_robin_next()


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


def search_contact_by_phone(phone, token):
    """Search for an existing contact by phone number. Returns contact dict or None."""
    import re
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    data = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "phone",
                "operator": "CONTAINS_TOKEN",
                "value": digits[-7:]
            }]
        }],
        "properties": ["firstname", "lastname", "email", "phone"],
        "limit": 5
    }
    resp, status = api_request("POST", "/crm/v3/objects/contacts/search", data, token)
    if status == 200 and resp.get("total", 0) > 0:
        # Match against full digits to avoid false positives
        for result in resp.get("results", []):
            result_phone = re.sub(r"\D", "", result.get("properties", {}).get("phone", ""))
            if result_phone.endswith(digits) or digits.endswith(result_phone):
                return result
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


def classify_traffic_source(traffic_raw):
    """Map raw traffic source string to HubSpot lead_source enum value."""
    t = (traffic_raw or "").lower()
    if "cpc" in t or "ads" in t or "paid" in t:
        return "google_ads"
    elif "organic" in t:
        return "organic_search"
    elif "direct" in t:
        return "direct"
    elif "referral" in t or "referred" in t:
        return "referral"
    elif "social" in t or "facebook" in t or "instagram" in t or "linkedin" in t:
        return "social_media"
    elif t:
        return "other"
    return None


def create_deal(lead, contact_id, token, pipeline_id="default"):
    """Create a deal associated with the contact. Returns (deal_resp, deal_status, owner_id)."""
    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
    deal_name = name or lead.get("service_interest", "New Lead")

    properties = {
        "dealname": deal_name,
        "pipeline": pipeline_id,
        "dealstage": "appointmentscheduled",  # "New Lead" stage
    }

    # Assign owner
    owner_id = assign_owner(lead)
    if owner_id:
        properties["hubspot_owner_id"] = owner_id

    # Set filterable properties
    source_val = classify_traffic_source(lead.get("traffic_source", ""))
    if source_val:
        properties["lead_source"] = source_val

    service = lead.get("service_interest", "")
    if service:
        properties["service_interest"] = service

    # Create deal
    deal_resp, deal_status = api_request(
        "POST", "/crm/v3/objects/deals", {"properties": properties}, token
    )
    if deal_status not in (200, 201):
        return deal_resp, deal_status, owner_id

    # Associate deal with contact
    deal_id = deal_resp.get("id")
    if deal_id and contact_id:
        api_request(
            "PUT",
            f"/crm/v3/objects/deals/{deal_id}/associations/contacts/{contact_id}/deal_to_contact",
            None, token
        )

    # Add note with project details
    if deal_id:
        create_deal_note(lead, deal_id, contact_id, token)

    return deal_resp, deal_status, owner_id


def create_deal_note(lead, deal_id, contact_id, token):
    """Add a note to the deal with service interest and message."""
    service = lead.get("service_interest", "Not specified")
    message = lead.get("message", "").strip()
    received = lead.get("received_at", "")

    # Aspire status
    aspire_status = lead.get("_aspire_status", "")
    if aspire_status and aspire_status not in ("not_created",):
        if aspire_status == "exists":
            aspire_line = "Aspire: Contact already existed"
        elif aspire_status == "dry-run":
            aspire_line = "Aspire: Dry run (not created)"
        else:
            aspire_line = f"Aspire: Contact created ({aspire_status})"
    elif aspire_status == "not_created":
        aspire_line = "Aspire: Pending (syncs when Mac is on)"
    else:
        aspire_line = "Aspire: Pending (syncs when Mac is on)"

    note_lines = [f"Service: {service}"]
    if message:
        note_lines.append(f"\n{message}")
    note_lines.append(f"\n{aspire_line}")
    if received:
        note_lines.append(f"Received: {received}")

    note_body = "\n".join(note_lines)

    data = {
        "properties": {
            "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "hs_note_body": note_body,
        },
        "associations": [
            {
                "to": {"id": deal_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]
            },
        ]
    }
    if contact_id:
        data["associations"].append({
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]
        })

    api_request("POST", "/crm/v3/objects/notes", data, token)


def update_aspire_status(email, aspire_url, token):
    """Find HubSpot contact by email and add an Aspire status note to their deal."""
    if not email:
        return False

    # Find contact
    contact = search_contact_by_email(email, token)
    if not contact:
        return False

    contact_id = contact["id"]

    # Find associated deals
    resp, status = api_request(
        "GET",
        f"/crm/v3/objects/contacts/{contact_id}/associations/deals",
        None, token
    )
    if status != 200 or not resp.get("results"):
        return False

    deal_id = resp["results"][0].get("id")
    if not deal_id:
        return False

    # Check the "Added to Aspire" checkbox on the deal
    api_request(
        "PATCH", f"/crm/v3/objects/deals/{deal_id}",
        {"properties": {"added_to_aspire": "true"}}, token
    )

    # Add note about Aspire
    if "http" in str(aspire_url):
        note_body = f"Aspire: Contact created ({aspire_url})"
    else:
        note_body = f"Aspire: Contact created"

    data = {
        "properties": {
            "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "hs_note_body": note_body,
        },
        "associations": [
            {
                "to": {"id": deal_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]
            },
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]
            },
        ]
    }
    _, note_status = api_request("POST", "/crm/v3/objects/notes", data, token)
    return note_status in (200, 201)


# --- Main ---

def process_lead(lead, token, portal_id=None):
    """Process a lead: dedup, create contact, create deal. Returns result dict."""
    email = lead.get("email", "").strip()
    phone = lead.get("phone", "").strip()

    # Check for existing contact by email
    existing = None
    if email:
        existing = search_contact_by_email(email, token)

    # Fall back to phone dedup for call leads without email
    if not existing and not email and phone:
        existing = search_contact_by_phone(phone, token)

    if existing:
            contact_id = existing["id"]
            contact_url = f"https://app-na2.hubspot.com/contacts/{portal_id or ''}/record/0-1/{contact_id}"
            # Look up existing deal owner
            existing_owner_id = None
            try:
                assoc_resp, assoc_status = api_request(
                    "GET", f"/crm/v3/objects/contacts/{contact_id}/associations/deals", None, token
                )
                if assoc_status == 200 and assoc_resp.get("results"):
                    deal_id = assoc_resp["results"][0].get("id")
                    if deal_id:
                        deal_resp, deal_status = api_request(
                            "GET", f"/crm/v3/objects/deals/{deal_id}?properties=hubspot_owner_id", None, token
                        )
                        if deal_status == 200:
                            existing_owner_id = deal_resp.get("properties", {}).get("hubspot_owner_id", "")
            except Exception:
                pass
            return {
                "success": True,
                "action": "exists",
                "contact_id": contact_id,
                "contact_url": contact_url,
                "owner_id": existing_owner_id,
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
    deal_resp, deal_status, owner_id = create_deal(lead, contact_id, token)
    deal_id = deal_resp.get("id") if deal_status in (200, 201) else None
    deal_url = f"https://app-na2.hubspot.com/contacts/{portal_id or ''}/record/0-3/{deal_id}" if deal_id else None

    return {
        "success": True,
        "action": "created",
        "contact_id": contact_id,
        "contact_url": contact_url,
        "deal_id": deal_id,
        "deal_url": deal_url,
        "owner_id": owner_id,
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

    # Update Aspire status on existing deal
    # Usage: --update-aspire --email X --aspire-url Y
    if "--update-aspire" in sys.argv:
        email = None
        aspire_url = None
        for i, arg in enumerate(sys.argv):
            if arg == "--email" and i + 1 < len(sys.argv):
                email = sys.argv[i + 1]
            if arg == "--aspire-url" and i + 1 < len(sys.argv):
                aspire_url = sys.argv[i + 1]
        if not email:
            print(json.dumps({"success": False, "message": "No --email provided"}))
            sys.exit(1)
        ok = update_aspire_status(email, aspire_url or "created", token)
        print(json.dumps({"success": ok, "action": "aspire_updated" if ok else "not_found"}))
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
