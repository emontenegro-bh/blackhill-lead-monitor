#!/usr/bin/env python3
"""WhatConverts ROI Sync — Aspire → WhatConverts.

Reads WC lead ID → Aspire contact ID mappings from the lead monitor state,
queries Aspire for opportunity status changes, and writes quotable/quote_value/
sales_value back to WhatConverts.

Logic:
  - Aspire opportunity "Delivered" or later → WC quotable=yes, quote_value=$EstimatedDollars
  - Aspire opportunity "Won"               → WC sales_value=$WonDollars
  - Aspire opportunity "Lost"              → WC quotable=no

Runs on a schedule (e.g., every 30 minutes via launchd or GitHub Actions).

Usage:
  python3 whatconverts-roi-sync.py            # Normal run
  python3 whatconverts-roi-sync.py --dry-run  # Preview without updating WhatConverts
  python3 whatconverts-roi-sync.py --backfill # Scan ALL Aspire contacts to find matches
"""

import json, os, sys, urllib.request, urllib.error, urllib.parse, base64
from datetime import datetime, timezone

DRY_RUN = "--dry-run" in sys.argv
BACKFILL = "--backfill" in sys.argv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "..", "data", "processed-state.json")
SYNC_STATE_FILE = os.path.join(SCRIPT_DIR, "..", "data", "roi-sync-state.json")

# Aspire opportunity stages that mean "quotable" (proposal sent or beyond)
QUOTABLE_STATUSES = {"Delivered", "Won"}
# "Bidding", "Pending Approval", "Approved" = still estimating, not yet quotable
# "Lost" = was quotable but lost

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --- Config Loading ---

def load_wc_config():
    """Load WhatConverts API config."""
    if os.environ.get("WC_API_TOKEN"):
        return {
            "api_token": os.environ["WC_API_TOKEN"],
            "api_secret": os.environ["WC_API_SECRET"],
            "profile_id": os.environ.get("WC_PROFILE_ID", "162442"),
        }
    wc_path = os.path.expanduser("~/.config/whatconverts/config.json")
    with open(wc_path) as f:
        wc = json.load(f)
    return {
        "api_token": wc["api_token"],
        "api_secret": wc["api_secret"],
        "profile_id": wc.get("profile_id", "162442"),
    }


def load_aspire_config():
    """Load Aspire API config."""
    client_id = os.environ.get("ASPIRE_CLIENT_ID") or os.environ.get("ASPIRE_REPORTING_CLIENT_ID")
    secret = os.environ.get("ASPIRE_SECRET") or os.environ.get("ASPIRE_REPORTING_SECRET")
    if client_id and secret:
        base = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
        return {"api_base_url": base, "client_id": client_id, "secret": secret}
    config_path = os.path.expanduser("~/.config/aspire/config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    return {
        "api_base_url": cfg["api_base_url"],
        "client_id": cfg["reporting_client_id"],
        "secret": cfg["reporting_secret"],
    }


# --- Aspire API ---

def aspire_authenticate(config):
    """Get JWT token from Aspire."""
    data = json.dumps({
        "ClientId": config["client_id"],
        "Secret": config["secret"],
    }).encode()
    req = urllib.request.Request(
        f"{config['api_base_url']}/Authorization",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode())
        return body.get("Token", "")


def aspire_query(config, token, endpoint, params=""):
    """Query Aspire OData endpoint. Returns list of results."""
    base = config["api_base_url"]
    url = f"{base}/{endpoint}"
    if params:
        safe_chars = "=&$,'()"
        url += "?" + urllib.parse.quote(params, safe=safe_chars)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data if isinstance(data, list) else [data]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        log(f"  ERROR: Aspire API {e.code}: {body}")
        return []
    except Exception as e:
        log(f"  ERROR: Aspire query failed: {e}")
        return []


def get_opportunities_for_contact(config, token, contact_id):
    """Get all opportunities where this contact is the billing contact."""
    params = f"$filter=BillingContactID eq {contact_id}&$select=OpportunityID,OpportunityNumber,OpportunityStatusName,EstimatedDollars,WonDollars,WonDate,LostDate,ProposedDate,DivisionName,OpportunityName&$orderby=CreatedDateTime desc"
    return aspire_query(config, token, "Opportunities", params)


# --- WhatConverts API ---

def wc_update_lead(wc_config, lead_id, updates):
    """Update a WhatConverts lead with form-encoded data.
    updates: dict like {"quotable": "yes", "quote_value": 500, "sales_value": 500}
    """
    token = wc_config["api_token"]
    secret = wc_config["api_secret"]
    credentials = base64.b64encode(f"{token}:{secret}".encode()).decode()

    url = f"https://app.whatconverts.com/api/v1/leads/{lead_id}"
    data = urllib.parse.urlencode(updates).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    }, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        log(f"  ERROR: WC update failed {e.code}: {body}")
        return None
    except Exception as e:
        log(f"  ERROR: WC update failed: {e}")
        return None


def wc_get_lead(wc_config, lead_id):
    """Get current state of a WhatConverts lead."""
    token = wc_config["api_token"]
    secret = wc_config["api_secret"]
    credentials = base64.b64encode(f"{token}:{secret}".encode()).decode()

    url = f"https://app.whatconverts.com/api/v1/leads/{lead_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# --- State Management ---

def load_lead_mappings():
    """Load WC lead ID → Aspire contact ID mappings from lead monitor state."""
    if not os.path.exists(STATE_FILE):
        log(f"State file not found: {STATE_FILE}")
        return {}
    with open(STATE_FILE) as f:
        state = json.load(f)
    return state.get("lead_mappings", {})


def load_sync_state():
    """Load sync state (tracks which leads have been synced and their last known status)."""
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE) as f:
            return json.load(f)
    return {"synced_leads": {}, "stats": {"total_synced": 0, "last_run": None}}


def save_sync_state(sync_state):
    """Save sync state."""
    sync_state["stats"]["last_run"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(SYNC_STATE_FILE), exist_ok=True)
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump(sync_state, f, indent=2)


# --- Backfill: Match existing Aspire contacts to WhatConverts leads ---

def backfill_mappings(wc_config, aspire_config, aspire_token):
    """Scan WhatConverts leads and try to match to Aspire contacts by name/email.
    Builds mappings for leads that existed before the lead monitor stored them."""
    log("Starting backfill scan...")
    profile_id = wc_config["profile_id"]
    token = wc_config["api_token"]
    secret = wc_config["api_secret"]
    credentials = base64.b64encode(f"{token}:{secret}".encode()).decode()

    mappings = load_lead_mappings()
    new_mappings = 0
    page = 1

    # WC API limits to 400 days max range
    from datetime import timedelta
    start_date = (datetime.now(timezone.utc) - timedelta(days=390)).strftime("%Y-%m-%d")

    while True:
        url = f"https://app.whatconverts.com/api/v1/leads?profile_id={profile_id}&leads_per_page=50&page_number={page}&start_date={start_date}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            log(f"  ERROR fetching WC leads page {page}: {e}")
            break

        leads = result.get("leads", [])
        if not leads:
            break

        for lead in leads:
            lead_id = str(lead.get("lead_id", ""))
            if lead_id in mappings:
                continue  # Already mapped

            # Try to match by email or name
            email = (lead.get("contact_email_address") or "").strip()
            name = (lead.get("contact_name") or "").strip()
            fields = lead.get("additional_fields", {})
            if isinstance(fields, dict):
                email = email or (fields.get("Email", "") or "").strip()
                name = name or (fields.get("Name", "") or "").strip()

            contact = None
            if email and "@" in email:
                safe_email = email.replace("'", "''")
                contacts = aspire_query(aspire_config, aspire_token,
                    "Contacts", f"$filter=Email eq '{safe_email}'&$top=1&$select=ContactID,FirstName,LastName,Email")
                if contacts:
                    contact = contacts[0]

            if not contact and name:
                parts = name.split(None, 1)
                if len(parts) == 2:
                    safe_first = parts[0].replace("'", "''")
                    safe_last = parts[1].replace("'", "''")
                    contacts = aspire_query(aspire_config, aspire_token,
                        "Contacts", f"$filter=FirstName eq '{safe_first}' and LastName eq '{safe_last}'&$top=1&$select=ContactID,FirstName,LastName")
                    if contacts:
                        contact = contacts[0]

            if contact:
                contact_id = str(contact.get("ContactID", ""))
                source = lead.get("lead_source", "")
                medium = lead.get("lead_medium", "")
                traffic = f"{source} / {medium}" if source and medium else source or medium or "unknown"

                mappings[lead_id] = {
                    "aspire_contact_id": contact_id,
                    "traffic_source": traffic,
                    "service": "",
                    "date": (lead.get("date_created", ""))[:10],
                }
                new_mappings += 1
                log(f"  Matched WC #{lead_id} → Aspire contact {contact_id} ({contact.get('FirstName', '')} {contact.get('LastName', '')})")

        if page >= result.get("total_pages", 1):
            break
        page += 1

    # Save mappings back to state
    if new_mappings > 0:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        else:
            state = {"processed_ids": [], "stats": {}}
        state["lead_mappings"] = mappings
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        log(f"Backfill complete: {new_mappings} new mappings added (total: {len(mappings)})")
    else:
        log("Backfill complete: no new matches found")

    return mappings


# --- Main Sync Logic ---

def sync_lead(wc_config, aspire_config, aspire_token, wc_lead_id, mapping, sync_state):
    """Check Aspire opportunity status for a mapped lead and update WhatConverts."""
    aspire_contact_id = mapping.get("aspire_contact_id")
    if not aspire_contact_id:
        return

    synced = sync_state["synced_leads"].get(wc_lead_id, {})
    last_status = synced.get("last_status", "")

    # If already synced as Won, nothing more to do
    if last_status == "Won":
        return

    # Get opportunities for this contact
    opps = get_opportunities_for_contact(aspire_config, aspire_token, int(aspire_contact_id))
    if not opps:
        return

    # Find the best opportunity status (prioritize Won > Delivered > Lost > Bidding)
    best_opp = None
    for opp in opps:
        status = opp.get("OpportunityStatusName", "")
        if status == "Won":
            best_opp = opp
            break
        elif status in ("Delivered",) and (not best_opp or best_opp.get("OpportunityStatusName") != "Won"):
            best_opp = opp
        elif status == "Lost" and not best_opp:
            best_opp = opp
        elif not best_opp:
            best_opp = opp

    if not best_opp:
        return

    status = best_opp.get("OpportunityStatusName", "")
    opp_num = best_opp.get("OpportunityNumber", "?")
    estimated = best_opp.get("EstimatedDollars", 0) or 0
    won_dollars = best_opp.get("WonDollars", 0) or 0

    # Determine what to update in WhatConverts
    updates = {}

    if status == "Won" and last_status != "Won":
        updates["quotable"] = "yes"
        updates["quote_value"] = int(estimated)
        updates["sales_value"] = int(won_dollars)
        log(f"  WC #{wc_lead_id} → Won (Opp #{opp_num}: ${won_dollars:,.0f})")

    elif status == "Delivered" and last_status not in ("Delivered", "Won"):
        updates["quotable"] = "yes"
        updates["quote_value"] = int(estimated)
        log(f"  WC #{wc_lead_id} → Quotable (Opp #{opp_num}: ${estimated:,.0f})")

    elif status == "Lost" and last_status not in ("Lost", "Won"):
        updates["quotable"] = "yes"  # It was quotable — they got a proposal
        updates["quote_value"] = int(estimated)
        log(f"  WC #{wc_lead_id} → Lost (Opp #{opp_num}: ${estimated:,.0f})")

    if not updates:
        return

    if DRY_RUN:
        log(f"  DRY RUN: Would update WC #{wc_lead_id}: {updates}")
    else:
        result = wc_update_lead(wc_config, wc_lead_id, updates)
        if result:
            log(f"  Updated WC #{wc_lead_id}: quotable={result.get('quotable')}, "
                f"quote_value={result.get('quote_value')}, sales_value={result.get('sales_value')}")
        else:
            log(f"  FAILED to update WC #{wc_lead_id}")
            return  # Don't mark as synced if update failed

    # Record sync state
    sync_state["synced_leads"][wc_lead_id] = {
        "last_status": status,
        "opp_number": opp_num,
        "quote_value": int(estimated),
        "sales_value": int(won_dollars) if status == "Won" else 0,
        "traffic_source": mapping.get("traffic_source", ""),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    sync_state["stats"]["total_synced"] = len(sync_state["synced_leads"])


def main():
    log("=== WhatConverts ROI Sync ===")
    if DRY_RUN:
        log("DRY RUN mode — no changes will be made")

    # Load configs
    try:
        wc_config = load_wc_config()
        aspire_config = load_aspire_config()
    except Exception as e:
        log(f"FATAL: Config load failed: {e}")
        sys.exit(1)

    # Authenticate with Aspire
    try:
        aspire_token = aspire_authenticate(aspire_config)
        if not aspire_token:
            log("FATAL: Aspire authentication failed")
            sys.exit(1)
        log("Aspire authenticated")
    except Exception as e:
        log(f"FATAL: Aspire auth failed: {e}")
        sys.exit(1)

    # Load mappings (backfill if requested)
    if BACKFILL:
        mappings = backfill_mappings(wc_config, aspire_config, aspire_token)
    else:
        mappings = load_lead_mappings()

    if not mappings:
        log("No lead mappings found. Run with --backfill to scan existing leads, or wait for new leads from the lead monitor.")
        return

    log(f"Loaded {len(mappings)} lead mappings")

    # Load sync state
    sync_state = load_sync_state()

    # Process each mapped lead
    synced_count = 0
    for wc_lead_id, mapping in mappings.items():
        # Skip leads already synced as Won (final state)
        if sync_state["synced_leads"].get(wc_lead_id, {}).get("last_status") == "Won":
            continue
        try:
            sync_lead(wc_config, aspire_config, aspire_token, wc_lead_id, mapping, sync_state)
            synced_count += 1
        except Exception as e:
            log(f"  ERROR syncing WC #{wc_lead_id}: {e}")

    # Save sync state
    if not DRY_RUN:
        save_sync_state(sync_state)

    # Summary
    won = sum(1 for s in sync_state["synced_leads"].values() if s.get("last_status") == "Won")
    quotable = sum(1 for s in sync_state["synced_leads"].values() if s.get("last_status") in ("Delivered", "Won", "Lost"))
    total_revenue = sum(s.get("sales_value", 0) for s in sync_state["synced_leads"].values())

    log(f"\nSync complete: checked {synced_count} leads")
    log(f"  Total mapped: {len(mappings)} | Quotable: {quotable} | Won: {won} | Revenue: ${total_revenue:,.0f}")


if __name__ == "__main__":
    main()
