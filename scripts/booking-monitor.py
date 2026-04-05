#!/usr/bin/env python3
"""Microsoft Bookings Monitor - Auto-creates CRM contacts from new appointments.

Polls Microsoft Bookings API for new appointments. For each new booking:
  1. Extracts customer details (name, email, phone, notes)
  2. Creates contact in Aspire CRM (assigned to Evelin)
  3. Creates contact + deal in HubSpot (assigned to Evelin)

Runs via launchd every 5 minutes.

Usage:
    python3 booking-monitor.py           # Process new bookings
    python3 booking-monitor.py --test    # Test Bookings API connection
    python3 booking-monitor.py --status  # Show processing stats
    python3 booking-monitor.py --list    # List recent bookings
"""

import json, logging, os, signal, subprocess, sys, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta

# Hard timeout to prevent zombie processes under launchd
signal.alarm(120) if hasattr(signal, "alarm") else None

CLOUD_MODE = bool(os.environ.get("MS_CLIENT_SECRET"))

CONFIG_DIR = os.path.expanduser("~/.config/booking-monitor")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
TOKEN_CACHE_FILE = os.path.join(CONFIG_DIR, "msal_token_cache.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
LOG_PATH = os.path.join(CONFIG_DIR, "booking-monitor.log") if not CLOUD_MODE else None
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASPIRE_SYNC = os.path.join(SCRIPT_DIR, "aspire-api-sync.py")
HUBSPOT_SYNC = os.path.join(SCRIPT_DIR, "hubspot-sync.py")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Bookings.Read.All"]

# Owner IDs - bookings always assigned to Evelin
HUBSPOT_OWNER_EVELIN = "88710208"

# Appointment discovery window
LOOKBACK_HOURS = 24 * 7  # 7 days back
LOOKAHEAD_DAYS = 30  # 30 days forward

_log_handlers = [logging.StreamHandler(sys.stderr)]
if LOG_PATH:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    _log_handlers.append(logging.FileHandler(LOG_PATH))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("booking-monitor")


# --- Config ---

def load_config():
    if CLOUD_MODE:
        return {
            "microsoft": {
                "client_id": os.environ["MS_CLIENT_ID"],
                "tenant_id": os.environ["MS_TENANT_ID"],
                "client_secret": os.environ["MS_CLIENT_SECRET"],
                "user_email": os.environ.get("MS_USER_EMAIL", ""),
            },
        }
    if not os.path.exists(CONFIG_FILE):
        log.error(f"Config not found: {CONFIG_FILE}")
        return None
    with open(CONFIG_FILE) as f:
        return json.load(f)


# --- Auth ---

def get_token(config):
    """Acquire Graph API token via MSAL."""
    try:
        import msal
    except ImportError:
        log.error("msal not installed. Run: pip3 install msal")
        return None

    ms = config.get("microsoft", {})
    client_id = ms.get("client_id", "")
    tenant_id = ms.get("tenant_id", "")

    if CLOUD_MODE:
        return _get_token_client_credentials(msal, ms)
    return _get_token_silent(msal, client_id, tenant_id)


def _get_token_client_credentials(msal, ms):
    """Client credentials flow for GitHub Actions (no user interaction)."""
    app = msal.ConfidentialClientApplication(
        ms["client_id"],
        authority=f"https://login.microsoftonline.com/{ms['tenant_id']}",
        client_credential=ms["client_secret"],
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if result and "access_token" in result:
        return result["access_token"]
    log.error(f"Client credentials auth failed: {result.get('error_description', 'unknown') if result else 'no result'}")
    return None


def _get_token_silent(msal, client_id, tenant_id):
    """Silent token flow for local (cached user session)."""
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            cache.deserialize(f.read())

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if not accounts:
        log.error("No cached account. Run ms-auth-setup.py first.")
        return None

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if result and "access_token" in result:
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(cache.serialize())
        os.chmod(TOKEN_CACHE_FILE, 0o600)
        return result["access_token"]

    log.error(f"Token acquisition failed: {result.get('error_description', 'unknown') if result else 'no result'}")
    return None


# --- Graph API ---

def graph_request(endpoint, token):
    """GET request to Microsoft Graph API."""
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


# --- Bookings ---

def discover_booking_business(token):
    """Find the booking business ID."""
    resp, status = graph_request("/solutions/bookingBusinesses", token)
    if status != 200:
        log.error(f"Failed to list booking businesses: {status} - {resp}")
        return None

    businesses = resp.get("value", [])
    if not businesses:
        log.error("No booking businesses found.")
        return None

    biz = businesses[0]
    log.info(f"Found booking business: {biz.get('displayName')} (id: {biz.get('id')})")
    return biz.get("id")


def get_appointments(token, business_id, lookback_hours=LOOKBACK_HOURS):
    """Fetch recent and upcoming appointments."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    filter_str = f"start/dateTime ge '{start}' and start/dateTime le '{end}'"
    encoded_filter = urllib.parse.quote(filter_str)
    endpoint = (
        f"/solutions/bookingBusinesses/{business_id}/appointments"
        f"?$filter={encoded_filter}&$top=50&$orderby=start/dateTime desc"
    )

    resp, status = graph_request(endpoint, token)
    if status != 200:
        # Fallback: try without filter (some tenants don't support OData on Bookings)
        log.warning(f"Filtered query failed ({status}), trying unfiltered...")
        endpoint = f"/solutions/bookingBusinesses/{business_id}/appointments?$top=50"
        resp, status = graph_request(endpoint, token)
        if status != 200:
            log.error(f"Failed to get appointments: {status} - {resp}")
            return []

    return resp.get("value", [])


def extract_customer(appt):
    """Extract customer info from appointment, handling both API formats."""
    # Newer v1.0 format uses customers[] array
    if "customers" in appt and appt["customers"]:
        c = appt["customers"][0]
        return {
            "name": c.get("name", ""),
            "email": c.get("emailAddress", ""),
            "phone": c.get("phone", ""),
            "notes": c.get("notes", ""),
            "timezone": c.get("timeZone", ""),
        }
    # Older format uses flat fields
    return {
        "name": appt.get("customerName", ""),
        "email": appt.get("customerEmailAddress", ""),
        "phone": appt.get("customerPhone", ""),
        "notes": appt.get("customerNotes", ""),
        "timezone": appt.get("customerTimeZone", ""),
    }


# --- State ---

CLOUD_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "booking-state.json"
)


def load_state():
    state_file = CLOUD_STATE_FILE if CLOUD_MODE else STATE_FILE
    if os.path.exists(state_file):
        with open(state_file) as f:
            return json.load(f)
    return {
        "processed": {},
        "business_id": None,
        "stats": {"created": 0, "exists": 0, "errors": 0, "total_runs": 0},
    }


def save_state(state):
    if CLOUD_MODE:
        os.makedirs(os.path.dirname(CLOUD_STATE_FILE), exist_ok=True)
        with open(CLOUD_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- CRM Creation ---

def parse_name(name):
    """Split full name into first and last."""
    parts = (name or "").strip().split(None, 1)
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def create_aspire_contact(customer):
    """Create contact in Aspire CRM."""
    first, last = parse_name(customer["name"])
    lead = {
        "first_name": first,
        "last_name": last,
        "email": customer.get("email", ""),
        "phone": customer.get("phone", ""),
        "message": customer.get("notes", ""),
        "source": "Microsoft Bookings",
    }
    try:
        result = subprocess.run(
            ["python3", ASPIRE_SYNC, "--lead-json", json.dumps(lead)],
            capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
        return {"success": False, "message": f"No output. stderr: {result.stderr[-300:]}"}
    except Exception as e:
        return {"success": False, "message": str(e)[:200]}


def create_hubspot_contact(customer, appointment_date):
    """Create contact + deal in HubSpot (always assigned to Evelin)."""
    first, last = parse_name(customer["name"])
    lead = {
        "first_name": first,
        "last_name": last,
        "email": customer.get("email", ""),
        "phone": customer.get("phone", ""),
        "message": customer.get("notes", ""),
        "service_interest": "consultation",
        "traffic_source": "direct",
        "received_at": appointment_date,
        "owner_override": HUBSPOT_OWNER_EVELIN,
    }
    try:
        result = subprocess.run(
            ["python3", HUBSPOT_SYNC, "--lead-json", json.dumps(lead)],
            capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
        return {"success": False, "message": f"No output. stderr: {result.stderr[-300:]}"}
    except Exception as e:
        return {"success": False, "message": str(e)[:200]}


# --- Main ---

def process_appointments(token, state):
    """Find and process new booking appointments."""
    business_id = state.get("business_id")

    if not business_id:
        business_id = discover_booking_business(token)
        if not business_id:
            return
        state["business_id"] = business_id
        save_state(state)

    appointments = get_appointments(token, business_id)
    if not appointments:
        log.info("No appointments found in window.")
        return

    new_count = 0
    for appt in appointments:
        appt_id = appt.get("id", "")
        if not appt_id or appt_id in state["processed"]:
            continue

        customer = extract_customer(appt)
        if not customer["name"]:
            log.debug(f"Skipping appointment {appt_id}: no customer name")
            continue

        start_dt = appt.get("startDateTime", {}).get("dateTime", "")
        service_name = appt.get("serviceName", "Consultation")

        log.info(f"New booking: {customer['name']} ({customer['email']}) "
                 f"- {service_name} on {start_dt[:10]}")

        # Create in Aspire
        aspire_result = create_aspire_contact(customer)
        aspire_action = aspire_result.get("action", "error")
        if aspire_result.get("success"):
            log.info(f"  Aspire: {aspire_action} - {aspire_result.get('contact_url', '')}")
        else:
            log.warning(f"  Aspire error: {aspire_result.get('message', '')}")

        # Create in HubSpot
        hubspot_result = create_hubspot_contact(customer, start_dt)
        hubspot_action = hubspot_result.get("action", "error")
        if hubspot_result.get("success"):
            log.info(f"  HubSpot: {hubspot_action} - {hubspot_result.get('contact_url', '')}")
        else:
            log.warning(f"  HubSpot error: {hubspot_result.get('message', '')}")

        # Track as processed
        state["processed"][appt_id] = {
            "customer_name": customer["name"],
            "customer_email": customer["email"],
            "appointment_date": start_dt,
            "service": service_name,
            "aspire": {
                "action": aspire_action,
                "contact_url": aspire_result.get("contact_url", ""),
            },
            "hubspot": {
                "action": hubspot_action,
                "contact_url": hubspot_result.get("contact_url", ""),
                "deal_url": hubspot_result.get("deal_url", ""),
            },
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }

        if aspire_action in ("created", "exists"):
            state["stats"]["created" if aspire_action == "created" else "exists"] += 1
        else:
            state["stats"]["errors"] += 1

        new_count += 1

    if new_count == 0:
        log.info("No new bookings to process.")
    else:
        log.info(f"Processed {new_count} new booking(s).")


def run_monitor():
    """Main entry point."""
    config = load_config()
    if not config:
        return

    token = get_token(config)
    if not token:
        return

    state = load_state()
    state["stats"]["total_runs"] = state["stats"].get("total_runs", 0) + 1

    process_appointments(token, state)
    save_state(state)


def test_connection():
    """Test Bookings API connectivity."""
    config = load_config()
    if not config:
        print(json.dumps({"success": False, "message": "No config found"}))
        return

    token = get_token(config)
    if not token:
        print(json.dumps({"success": False, "message": "Auth failed. Run ms-auth-setup.py"}))
        return

    business_id = discover_booking_business(token)
    if not business_id:
        print(json.dumps({"success": False, "message": "No booking businesses found"}))
        return

    appointments = get_appointments(token, business_id, lookback_hours=24 * 30)
    print(json.dumps({
        "success": True,
        "business_id": business_id,
        "recent_appointments": len(appointments),
        "message": f"Connected. Found {len(appointments)} appointments in last 30 days.",
    }, indent=2))


def show_status():
    """Show processing stats."""
    state = load_state()
    stats = state.get("stats", {})
    processed = state.get("processed", {})

    print("Booking Monitor Status")
    print("=" * 40)
    print(f"Business ID:      {state.get('business_id', 'Not discovered')}")
    print(f"Total runs:       {stats.get('total_runs', 0)}")
    print(f"Contacts created: {stats.get('created', 0)}")
    print(f"Already existed:  {stats.get('exists', 0)}")
    print(f"Errors:           {stats.get('errors', 0)}")
    print(f"Total processed:  {len(processed)}")

    recent = sorted(
        processed.items(),
        key=lambda x: x[1].get("processed_at", ""),
        reverse=True,
    )[:5]
    if recent:
        print("\nRecent bookings:")
        for _, info in recent:
            name = info.get("customer_name", "?")
            email = info.get("customer_email", "")
            date = info.get("appointment_date", "")[:10]
            aspire = info.get("aspire", {}).get("action", "?")
            hubspot = info.get("hubspot", {}).get("action", "?")
            print(f"  {name} ({email}) - {date} [Aspire: {aspire}, HubSpot: {hubspot}]")


def list_bookings():
    """List recent bookings from the API."""
    config = load_config()
    if not config:
        return

    token = get_token(config)
    if not token:
        print("Auth failed. Run ms-auth-setup.py")
        return

    state = load_state()
    business_id = state.get("business_id")
    if not business_id:
        business_id = discover_booking_business(token)
        if not business_id:
            return

    appointments = get_appointments(token, business_id, lookback_hours=24 * 14)
    print(f"Recent Bookings ({len(appointments)})")
    print("=" * 60)

    for appt in appointments:
        customer = extract_customer(appt)
        start = appt.get("startDateTime", {}).get("dateTime", "")[:16]
        service = appt.get("serviceName", "?")
        processed = "Y" if appt.get("id") in state.get("processed", {}) else " "
        print(f"  [{processed}] {customer['name']} ({customer['email']}) {customer['phone']}")
        print(f"      {service} - {start}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_connection()
    elif "--status" in sys.argv:
        show_status()
    elif "--list" in sys.argv:
        list_bookings()
    else:
        run_monitor()
