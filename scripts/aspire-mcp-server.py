#!/usr/bin/env python3
"""Aspire CRM MCP Server for Claude Code.

Lightweight MCP server (JSON-RPC over stdio) wrapping the Aspire OData API.
No SDK required — runs on Python 3.9+.

Provides tools for:
  - search_contacts: Find contacts by email, phone, or name
  - get_contact: Get a contact by ID
  - create_contact: Create a new prospect contact
  - search_opportunities: Query opportunities by status, date range, property
  - get_property: Get property details with contacts
  - get_jobs: Query jobs by status, opportunity, or property
  - get_job_statuses: List all job status codes
  - get_opportunity_statuses: List all opportunity status codes
  - create_work_ticket: Create as-needed work tickets
  - get_opportunity_service_groups: Query service groups by opportunity
  - get_availability: Get property availability records
  - create_availability: Add property availability records
  - query_odata: Run a raw OData query against any endpoint

API quirks baked in:
  - Phone fields: MobilePhone, HomePhone, OfficePhone (NOT Phone/PhoneCell)
  - No /api/ prefix in URLs
  - Opportunities endpoint may 403 with lead-monitor client — uses reporting client
  - Single quotes in OData filters escaped automatically
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import re

CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
TOKEN_CACHE_API = os.path.expanduser("~/.config/aspire/api-token.json")
TOKEN_CACHE_REPORTING = os.path.expanduser("~/.config/aspire/reporting-token.json")

# --- Config & Auth ---

_config = None
_tokens = {}  # client_type -> (token, expires_at)


def load_config():
    global _config
    if _config:
        return _config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            _config = json.load(f)
        return _config
    raise RuntimeError(f"Aspire config not found at {CONFIG_FILE}")


def get_token(client_type="api"):
    """Get JWT token, caching in memory and on disk."""
    config = load_config()

    # Check memory cache
    if client_type in _tokens:
        token, expires_at = _tokens[client_type]
        if time.time() < expires_at - 300:  # 5 min buffer
            return token

    # Check disk cache
    cache_file = TOKEN_CACHE_REPORTING if client_type == "reporting" else TOKEN_CACHE_API
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            token = cached.get("token", "")
            expires_at = cached.get("expires_at", 0)
            if token and time.time() < expires_at - 300:
                _tokens[client_type] = (token, expires_at)
                return token
        except Exception:
            pass

    # Authenticate
    if client_type == "reporting":
        client_id = config["reporting_client_id"]
        secret = config["reporting_secret"]
    else:
        client_id = config["api_client_id"]
        secret = config["api_secret"]

    base = config["api_base_url"]
    data = json.dumps({"ClientId": client_id, "Secret": secret}).encode()
    req = urllib.request.Request(
        f"{base}/Authorization",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode())

    token = body.get("Token", "")
    if not token:
        raise RuntimeError("Failed to get Aspire token")

    expires_at = time.time() + 3600  # 1 hour
    _tokens[client_type] = (token, expires_at)

    # Cache to disk
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump({"token": token, "expires_at": expires_at}, f)

    return token


def api_get(endpoint, params=None, client_type="api"):
    """Make authenticated GET to Aspire API."""
    config = load_config()
    base = config["api_base_url"]
    token = get_token(client_type)

    url = f"{base}/{endpoint.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Aspire API {e.code}: {body[:500]}")


def api_post(endpoint, payload, client_type="api"):
    """Make authenticated POST to Aspire API."""
    config = load_config()
    base = config["api_base_url"]
    token = get_token(client_type)

    url = f"{base}/{endpoint.lstrip('/')}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Aspire API {e.code}: {body[:500]}")


def api_put(endpoint, payload, client_type="api"):
    """Make authenticated PUT to Aspire API."""
    config = load_config()
    base = config["api_base_url"]
    token = get_token(client_type)

    url = f"{base}/{endpoint.lstrip('/')}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Aspire API {e.code}: {body[:500]}")


def escape_odata(value):
    """Escape single quotes for OData filter values."""
    return str(value).replace("'", "''")


def normalize_phone(phone):
    """Normalize phone to XXX-XXX-XXXX format."""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return phone


# --- Tool implementations ---

def tool_search_contacts(email=None, phone=None, first_name=None, last_name=None, top=10):
    """Search Aspire contacts by email, phone, or name."""
    filters = []
    if email:
        filters.append(f"Email eq '{escape_odata(email)}'")
    if phone:
        formatted = normalize_phone(phone)
        filters.append(
            f"(MobilePhone eq '{escape_odata(formatted)}' "
            f"or HomePhone eq '{escape_odata(formatted)}' "
            f"or OfficePhone eq '{escape_odata(formatted)}')"
        )
    if first_name:
        filters.append(f"FirstName eq '{escape_odata(first_name)}'")
    if last_name:
        filters.append(f"LastName eq '{escape_odata(last_name)}'")

    if not filters:
        return {"error": "Provide at least one search parameter: email, phone, first_name, or last_name"}

    params = {"$filter": " and ".join(filters), "$top": str(top)}
    results = api_get("Contacts", params)

    contacts = results if isinstance(results, list) else results.get("value", [results])
    return {
        "count": len(contacts),
        "contacts": [
            {
                "ContactID": c.get("ContactID"),
                "FirstName": c.get("FirstName"),
                "LastName": c.get("LastName"),
                "Email": c.get("Email"),
                "MobilePhone": c.get("MobilePhone"),
                "HomePhone": c.get("HomePhone"),
                "OfficePhone": c.get("OfficePhone"),
                "Active": c.get("Active"),
                "url": f"https://cloud.youraspire.com/app/contacts/{c.get('ContactID')}",
            }
            for c in contacts
        ],
    }


def tool_get_contact(contact_id):
    """Get a single contact by ID."""
    result = api_get(f"Contacts({contact_id})")
    if isinstance(result, dict):
        result["url"] = f"https://cloud.youraspire.com/app/contacts/{contact_id}"
    return result


def tool_create_contact(first_name, last_name, email=None, phone=None,
                        address=None, city=None, state="TX", zip_code=None,
                        notes=None):
    """Create a new prospect contact in Aspire."""
    contact = {
        "FirstName": first_name,
        "LastName": last_name,
        "ContactTypeID": 8,  # Prospect
        "OwnerContactID": 6,  # Evelin Montenegro
        "Active": True,
    }
    if email:
        contact["Email"] = email
    if phone:
        contact["MobilePhone"] = normalize_phone(phone)
    if notes:
        contact["Notes"] = notes

    payload = {"Contact": contact}

    if address or city or zip_code:
        office_address = {"StateProvinceCode": state}
        if address:
            office_address["AddressLine1"] = address
        if city:
            office_address["City"] = city
        if zip_code:
            office_address["ZipCode"] = zip_code
        payload["OfficeAddress"] = office_address

    result = api_post("Contacts", payload)

    # Extract contact ID from response (handles multiple formats)
    contact_id = None
    if isinstance(result, dict):
        contact_id = result.get("ContactID") or result.get("Id") or result.get("id")
    elif isinstance(result, (int, str)):
        contact_id = result

    return {
        "success": True,
        "contact_id": contact_id,
        "url": f"https://cloud.youraspire.com/app/contacts/{contact_id}" if contact_id else None,
    }


def tool_search_opportunities(status=None, min_amount=None, won_after=None,
                              won_before=None, top=100, client_type="reporting"):
    """Search opportunities. Uses reporting client by default (api client gets 403)."""
    filters = []
    if status:
        filters.append(f"OpportunityStatusName eq '{escape_odata(status)}'")
    if min_amount is not None:
        filters.append(f"EstimatedDollars gt {min_amount}")
    if won_after:
        filters.append(f"WonDate ge {won_after}T00:00:00Z")
    if won_before:
        filters.append(f"WonDate le {won_before}T23:59:59Z")

    params = {"$top": str(top), "$orderby": "WonDate desc"}
    if filters:
        params["$filter"] = " and ".join(filters)

    try:
        results = api_get("Opportunities", params, client_type=client_type)
    except RuntimeError as e:
        if "403" in str(e):
            return {
                "error": "403 Forbidden on Opportunities endpoint. "
                         "The reporting client may not have access. "
                         "Try querying Properties with $expand instead.",
                "suggestion": "Use query_odata with endpoint='Properties' and "
                              "params like $expand=Contacts to get property-level data."
            }
        raise

    opps = results if isinstance(results, list) else results.get("value", [results])
    return {
        "count": len(opps),
        "opportunities": [
            {
                "OpportunityNumber": o.get("OpportunityNumber"),
                "OpportunityName": o.get("OpportunityName"),
                "OpportunityStatusName": o.get("OpportunityStatusName"),
                "EstimatedDollars": o.get("EstimatedDollars"),
                "WonDate": o.get("WonDate"),
                "LostDate": o.get("LostDate"),
                "PropertyID": o.get("PropertyID"),
                "ContactID": o.get("ContactID"),
            }
            for o in opps
        ],
    }


def tool_get_property(property_id, expand=None):
    """Get property details, optionally expanding related data."""
    params = {}
    if expand:
        params["$expand"] = expand
    return api_get(f"Properties({property_id})", params, client_type="reporting")


def tool_get_jobs(status=None, opportunity_id=None, property_id=None, top=100):
    """Get jobs, optionally filtered by status, opportunity, or property."""
    filters = []
    if status:
        filters.append(f"Status eq '{escape_odata(status)}'")
    if opportunity_id:
        filters.append(f"OpportunityID eq {opportunity_id}")
    if property_id:
        filters.append(f"PropertyID eq {property_id}")
    params = {"$top": str(top)}
    if filters:
        params["$filter"] = " and ".join(filters)
    return api_get("Jobs", params, client_type="reporting")


def tool_get_job_statuses():
    """Get all job status records (system codes and names)."""
    return api_get("JobStatuses", client_type="reporting")


def tool_get_opportunity_statuses():
    """Get all opportunity status records (IDs, names, stages, active flags)."""
    return api_get("OpportunityStatuses", client_type="reporting")


def tool_create_work_ticket(property_id, description=None, service_id=None,
                            opportunity_id=None, requested_by=None):
    """Create an as-needed work ticket."""
    payload = {"PropertyID": property_id}
    if description:
        payload["Description"] = description
    if service_id:
        payload["ServiceID"] = service_id
    if opportunity_id:
        payload["OpportunityID"] = opportunity_id
    if requested_by:
        payload["RequestedBy"] = requested_by
    return api_post("AsNeededWorkTickets", payload, client_type="api")


def tool_get_opportunity_service_groups(opportunity_id=None, top=100):
    """Get opportunity service groups, optionally filtered by opportunity."""
    params = {"$top": str(top)}
    if opportunity_id:
        params["$filter"] = f"OpportunityID eq {opportunity_id}"
    return api_get("OpportunityServiceGroups", params, client_type="reporting")


def tool_get_availability(property_id=None, top=100):
    """Get property availability records."""
    params = {"$top": str(top)}
    if property_id:
        params["$filter"] = f"PropertyID eq {property_id}"
    return api_get("PropertyAvailabilities", params, client_type="reporting")


def tool_create_availability(property_id, day_of_week=None, start_time=None,
                             end_time=None, notes=None):
    """Add a property availability record."""
    payload = {"PropertyID": property_id}
    if day_of_week is not None:
        payload["DayOfWeek"] = day_of_week
    if start_time:
        payload["StartTime"] = start_time
    if end_time:
        payload["EndTime"] = end_time
    if notes:
        payload["Notes"] = notes
    return api_post("PropertyAvailabilities", payload, client_type="api")


def tool_query_odata(endpoint, filter=None, select=None, expand=None, orderby=None,
                     top=100, skip=0, client_type="reporting"):
    """Run a raw OData query against any Aspire endpoint."""
    params = {"$top": str(top)}
    if skip:
        params["$skip"] = str(skip)
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    if orderby:
        params["$orderby"] = orderby

    return api_get(endpoint, params, client_type=client_type)


# --- MCP Protocol (JSON-RPC over stdio) ---

TOOLS = [
    {
        "name": "search_contacts",
        "description": (
            "Search Aspire CRM contacts by email, phone number, or name. "
            "Phone fields searched: MobilePhone, HomePhone, OfficePhone. "
            "Phone numbers are auto-normalized to XXX-XXX-XXXX format."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Email address to search"},
                "phone": {"type": "string", "description": "Phone number (any format, auto-normalized)"},
                "first_name": {"type": "string", "description": "First name"},
                "last_name": {"type": "string", "description": "Last name"},
                "top": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
        },
    },
    {
        "name": "get_contact",
        "description": "Get a single Aspire contact by ContactID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer", "description": "Aspire ContactID"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "create_contact",
        "description": (
            "Create a new Prospect contact in Aspire CRM. "
            "Auto-sets ContactTypeID=8 (Prospect), OwnerContactID=6 (Evelin), Active=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string", "description": "Auto-normalized to XXX-XXX-XXXX"},
                "address": {"type": "string"},
                "city": {"type": "string"},
                "state": {"type": "string", "default": "TX"},
                "zip_code": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["first_name", "last_name"],
        },
    },
    {
        "name": "search_opportunities",
        "description": (
            "Search Aspire opportunities by status, amount, and date range. "
            "Uses reporting client. If 403, try query_odata on Properties with $expand."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "e.g. 'Won', 'Lost', 'Open'"},
                "min_amount": {"type": "number", "description": "Minimum EstimatedDollars"},
                "won_after": {"type": "string", "description": "YYYY-MM-DD"},
                "won_before": {"type": "string", "description": "YYYY-MM-DD"},
                "top": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "get_property",
        "description": "Get Aspire property details by PropertyID, with optional $expand.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "integer"},
                "expand": {"type": "string", "description": "OData $expand (e.g. 'Contacts')"},
            },
            "required": ["property_id"],
        },
    },
    {
        "name": "get_jobs",
        "description": (
            "Get Aspire jobs, optionally filtered by status, opportunity, or property. "
            "Returns job list with status, opportunity links, and completion dates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Job status filter"},
                "opportunity_id": {"type": "integer", "description": "Filter by OpportunityID"},
                "property_id": {"type": "integer", "description": "Filter by PropertyID"},
                "top": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "get_job_statuses",
        "description": "Get all Aspire job status records (system codes and customizable names).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_opportunity_statuses",
        "description": "Get all Aspire opportunity status records (IDs, names, stages, active flags).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_work_ticket",
        "description": (
            "Create an as-needed work ticket in Aspire. "
            "Use for ad-hoc service requests outside regular schedules."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "integer", "description": "PropertyID for the work ticket"},
                "description": {"type": "string", "description": "Work ticket description"},
                "service_id": {"type": "integer", "description": "ServiceID if known"},
                "opportunity_id": {"type": "integer", "description": "Link to an opportunity"},
                "requested_by": {"type": "string", "description": "Who requested the work"},
            },
            "required": ["property_id"],
        },
    },
    {
        "name": "get_opportunity_service_groups",
        "description": "Get opportunity service group data, optionally filtered by opportunity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "integer", "description": "Filter by OpportunityID"},
                "top": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "get_availability",
        "description": "Get property availability records, optionally filtered by property.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "integer", "description": "Filter by PropertyID"},
                "top": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "create_availability",
        "description": "Add a property availability record in Aspire.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "integer", "description": "PropertyID"},
                "day_of_week": {"type": "integer", "description": "Day of week (0=Sun, 6=Sat)"},
                "start_time": {"type": "string", "description": "Start time (HH:MM)"},
                "end_time": {"type": "string", "description": "End time (HH:MM)"},
                "notes": {"type": "string"},
            },
            "required": ["property_id"],
        },
    },
    {
        "name": "query_odata",
        "description": (
            "Run a raw OData query against any Aspire endpoint. "
            "Available endpoints: Contacts, Opportunities, Properties, ContactTypes, "
            "Divisions, Jobs, JobStatus, WorkTickets, Invoices, OpportunityStatus, "
            "OpportunityServiceGroups, OpportunityServiceKitItems, Availability, "
            "AsNeededWorkTickets, CatalogItems, UnitTypes, CertificationsTypes. "
            "Uses reporting client by default (broader read access)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "API endpoint (e.g. 'Contacts', 'Properties')"},
                "filter": {"type": "string", "description": "OData $filter expression"},
                "select": {"type": "string", "description": "OData $select fields"},
                "expand": {"type": "string", "description": "OData $expand relations"},
                "orderby": {"type": "string", "description": "OData $orderby expression"},
                "top": {"type": "integer", "default": 100},
                "skip": {"type": "integer", "default": 0},
                "client_type": {
                    "type": "string",
                    "enum": ["api", "reporting"],
                    "default": "reporting",
                    "description": "Which API client to use",
                },
            },
            "required": ["endpoint"],
        },
    },
]

TOOL_HANDLERS = {
    "search_contacts": tool_search_contacts,
    "get_contact": tool_get_contact,
    "create_contact": tool_create_contact,
    "search_opportunities": tool_search_opportunities,
    "get_property": tool_get_property,
    "get_jobs": tool_get_jobs,
    "get_job_statuses": tool_get_job_statuses,
    "get_opportunity_statuses": tool_get_opportunity_statuses,
    "create_work_ticket": tool_create_work_ticket,
    "get_opportunity_service_groups": tool_get_opportunity_service_groups,
    "get_availability": tool_get_availability,
    "create_availability": tool_create_availability,
    "query_odata": tool_query_odata,
}


def handle_request(req):
    """Handle a single JSON-RPC request."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "aspire-crm",
                    "version": "1.1.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = TOOL_HANDLERS.get(tool_name)

        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        try:
            result = handler(**arguments)
            text = json.dumps(result, indent=2, default=str)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
    """Run MCP server on stdio."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(req)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
