#!/usr/bin/env python3
"""API Health Monitor for Black Hill Landscaping automation stack.

Contract tests, diagnostics, and self-healing for 5 API integrations:
  Aspire OData, WhatConverts, CompanyCam, Google Ads, HubSpot.

Usage:
  python3 scripts/api-health-monitor.py --test              # Run contract tests
  python3 scripts/api-health-monitor.py --test --api aspire  # Test single API
  python3 scripts/api-health-monitor.py --diagnose           # Diagnose failures
  python3 scripts/api-health-monitor.py --heal               # Diagnose + patch + branch + PR
  python3 scripts/api-health-monitor.py --dry-run --heal     # Show what would change

Modes:
  CI (env vars):  ASPIRE_CLIENT_ID, WC_API_TOKEN, COMPANYCAM_TOKEN, etc.
  Local (~/.config/):  aspire/config.json, whatconverts/config.json, etc.
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --- Constants ---

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
PROJECT_DIR = os.path.join(SCRIPT_DIR, "..")
DRY_RUN = "--dry-run" in sys.argv

# Expected field names per API (the contract)
ASPIRE_CONTACT_FIELDS = {"MobilePhone", "HomePhone", "OfficePhone", "Email",
                         "FirstName", "LastName", "ContactID", "Active"}
ASPIRE_OPP_FIELDS = {"OpportunityNumber", "OpportunityStatusName",
                      "EstimatedDollars", "WonDollars"}
WC_LEAD_FIELDS = {"id", "lead_type", "contact_name", "phone_number"}
WC_UPDATE_FIELDS = {"quotable", "quote_value", "sales_value"}
COMPANYCAM_PROJECT_FIELDS = {"id", "name", "created_at", "updated_at", "address"}
HUBSPOT_CONTACT_PROPS = {"firstname", "lastname", "email", "phone"}
HUBSPOT_OWNER_IDS = {"88710208", "162535167"}

# Scripts affected by each API's field names
SCRIPT_TARGETS = {
    "aspire": [
        "aspire-api-sync.py", "aspire-mcp-server.py", "whatconverts-roi-sync.py",
        "export-irrigation-customers.py", "pull-march-leads.py",
        "build-maintenance-cross-sell-csv.py", "get-contact-emails.py",
    ],
    "whatconverts": ["whatconverts-roi-sync.py", "whatconverts-lead-monitor.py"],
    "companycam": ["proposal-monitor.py", "companycam-query.py"],
    "hubspot": ["hubspot-sync.py", "hubspot-builder-pipeline.py",
                "whatconverts-lead-monitor.py"],
    "google_ads": ["morning-briefing.py", "ads-smoke-test.py",
                   "ads-weekly-report.py", "ads-budget-and-negatives.py"],
}


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============================================================
# Config Loaders
# ============================================================

def load_aspire_config():
    """Load Aspire API config. Returns dict with api + reporting creds, or None."""
    api_id = os.environ.get("ASPIRE_CLIENT_ID")
    api_secret = os.environ.get("ASPIRE_SECRET")
    rep_id = os.environ.get("ASPIRE_REPORTING_CLIENT_ID")
    rep_secret = os.environ.get("ASPIRE_REPORTING_SECRET")
    base = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
    if api_id and api_secret:
        return {
            "api_base_url": base,
            "api_client_id": api_id, "api_secret": api_secret,
            "reporting_client_id": rep_id or api_id,
            "reporting_secret": rep_secret or api_secret,
        }
    path = os.path.expanduser("~/.config/aspire/config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        cfg.setdefault("api_base_url", "https://cloud-api.youraspire.com")
        return cfg
    return None


def load_wc_config():
    """Load WhatConverts API config."""
    if os.environ.get("WC_API_TOKEN"):
        return {
            "api_token": os.environ["WC_API_TOKEN"],
            "api_secret": os.environ["WC_API_SECRET"],
            "profile_id": os.environ.get("WC_PROFILE_ID", "162442"),
        }
    path = os.path.expanduser("~/.config/whatconverts/config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        return {
            "api_token": cfg["api_token"],
            "api_secret": cfg["api_secret"],
            "profile_id": cfg.get("profile_id", "162442"),
        }
    return None


def load_companycam_config():
    """Load CompanyCam API config."""
    token = os.environ.get("COMPANYCAM_TOKEN")
    if token:
        return {"access_token": token, "base_url": "https://api.companycam.com/v2"}
    path = os.path.expanduser("~/.config/companycam/config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        cfg.setdefault("base_url", "https://api.companycam.com/v2")
        return cfg
    return None


def load_google_ads_config():
    """Load Google Ads config."""
    if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        return {
            "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
            "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            "customer_id": os.environ["GOOGLE_ADS_CUSTOMER_ID"],
        }
    path = os.path.expanduser("~/.config/google-ads/config.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def load_hubspot_config():
    """Load HubSpot config."""
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if token:
        return {"access_token": token}
    path = os.path.expanduser("~/.config/hubspot/config.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


# ============================================================
# HTTP Helper
# ============================================================

def http_request(method, url, headers=None, data=None, timeout=30):
    """Make HTTP request. Returns (body_dict_or_list, status_code).
    On error returns (error_body, status_code). status_code=0 for network errors.
    """
    headers = headers or {}
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, bytes):
            body = data
        else:
            body = str(data).encode()

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return json.loads(raw) if raw.strip() else {}, resp.status
            except json.JSONDecodeError:
                return {"_raw": raw}, resp.status
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        try:
            return json.loads(err_body), e.code
        except Exception:
            return {"_raw": err_body}, e.code
    except Exception as e:
        return {"_error": str(e)}, 0


# ============================================================
# Test Result / Drift Report
# ============================================================

class TestResult:
    def __init__(self, api, test, passed, message, details=None):
        self.api = api
        self.test = test
        self.passed = passed
        self.message = message
        self.details = details or {}


class DriftReport:
    def __init__(self, api, drift_type, old_value, new_value, confidence,
                 affected_scripts):
        self.api = api
        self.drift_type = drift_type
        self.old_value = old_value
        self.new_value = new_value
        self.confidence = confidence
        self.affected_scripts = affected_scripts


# ============================================================
# Aspire Contract Tests
# ============================================================

def aspire_authenticate(base_url, client_id, secret):
    """Authenticate with Aspire. Returns (token, status_code)."""
    body, status = http_request("POST", f"{base_url}/Authorization", data={
        "ClientId": client_id, "Secret": secret,
    })
    if status == 200 and isinstance(body, dict):
        return body.get("Token", ""), status
    return "", status


def test_aspire_auth_api(config):
    token, status = aspire_authenticate(
        config["api_base_url"], config["api_client_id"], config["api_secret"])
    if token:
        return TestResult("aspire", "auth (api client)", True,
                          "JWT obtained", {"token_len": len(token)})
    return TestResult("aspire", "auth (api client)", False,
                      f"Auth failed (HTTP {status})")


def test_aspire_auth_reporting(config):
    token, status = aspire_authenticate(
        config["api_base_url"], config["reporting_client_id"],
        config["reporting_secret"])
    if token:
        return TestResult("aspire", "auth (reporting client)", True,
                          "JWT obtained", {"token_len": len(token)})
    return TestResult("aspire", "auth (reporting client)", False,
                      f"Auth failed (HTTP {status})")


def test_aspire_read_contact_types(config, token):
    url = f"{config['api_base_url']}/ContactTypes?$top=1"
    body, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    if status == 200:
        items = body if isinstance(body, list) else [body]
        return TestResult("aspire", "read /ContactTypes", True,
                          f"{len(items)} type(s)", {"count": len(items)})
    return TestResult("aspire", "read /ContactTypes", False,
                      f"HTTP {status}", {"body": body})


def test_aspire_read_contacts(config, token):
    url = f"{config['api_base_url']}/Contacts?$top=1"
    body, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    if status == 200:
        items = body if isinstance(body, list) else [body]
        if items:
            keys = set(items[0].keys())
            return TestResult("aspire", "read /Contacts", True,
                              f"fields: {len(keys)}", {"keys": sorted(keys)})
        return TestResult("aspire", "read /Contacts", True,
                          "empty response", {"keys": []})
    return TestResult("aspire", "read /Contacts", False,
                      f"HTTP {status}", {"body": body})


def test_aspire_opportunities_403(config, api_token):
    """Verify api client gets 403 on Opportunities (known restriction)."""
    url = f"{config['api_base_url']}/Opportunities?$top=1"
    _, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {api_token}", "Accept": "application/json"})
    if status == 403:
        return TestResult("aspire", "opportunities 403 (api client)", True,
                          "403 as expected — use reporting client")
    return TestResult("aspire", "opportunities 403 (api client)", False,
                      f"Expected 403 but got {status} — permission model may have changed",
                      {"actual_status": status})


def test_aspire_opportunities_reporting(config, reporting_token):
    """Verify reporting client CAN read Opportunities."""
    url = f"{config['api_base_url']}/Opportunities?$top=1&$select=OpportunityNumber,OpportunityStatusName,EstimatedDollars,WonDollars"
    body, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {reporting_token}", "Accept": "application/json"})
    if status == 200:
        items = body if isinstance(body, list) else [body]
        if items:
            keys = set(items[0].keys())
            return TestResult("aspire", "read /Opportunities (reporting)", True,
                              f"fields: {len(keys)}", {"keys": sorted(keys)})
        return TestResult("aspire", "read /Opportunities (reporting)", True,
                          "empty response", {"keys": []})
    return TestResult("aspire", "read /Opportunities (reporting)", False,
                      f"HTTP {status}", {"body": body})


def test_aspire_field_names(config, token):
    """Verify phone field names are MobilePhone/HomePhone/OfficePhone."""
    url = f"{config['api_base_url']}/Contacts?$top=1&$select=MobilePhone,HomePhone,OfficePhone,Email,FirstName,LastName,ContactID"
    body, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    if status != 200:
        return TestResult("aspire", "field names", False,
                          f"HTTP {status} — fields may have been renamed",
                          {"status": status, "body": body})
    items = body if isinstance(body, list) else [body]
    if not items:
        return TestResult("aspire", "field names", True,
                          "No contacts to verify fields (empty account)")
    keys = set(items[0].keys())
    expected_phone = {"MobilePhone", "HomePhone", "OfficePhone"}
    missing = expected_phone - keys
    if missing:
        return TestResult("aspire", "field names", False,
                          f"Missing phone fields: {missing}",
                          {"expected": sorted(expected_phone),
                           "actual": sorted(keys), "missing": sorted(missing)})
    return TestResult("aspire", "field names", True,
                      f"MobilePhone, HomePhone, OfficePhone confirmed",
                      {"keys": sorted(keys)})


def run_aspire_tests(config):
    results = []
    # Auth - api client
    r = test_aspire_auth_api(config)
    results.append(r)
    api_token = r.details.get("token_len") and r.passed

    # Auth - reporting client
    r2 = test_aspire_auth_reporting(config)
    results.append(r2)

    if not api_token:
        for name in ["read /ContactTypes", "read /Contacts",
                      "opportunities 403 (api client)", "field names"]:
            results.append(TestResult("aspire", name, False,
                                      "Skipped — api auth failed"))
    else:
        # Get fresh tokens for tests
        api_tok, _ = aspire_authenticate(
            config["api_base_url"], config["api_client_id"], config["api_secret"])
        results.append(test_aspire_read_contact_types(config, api_tok))
        results.append(test_aspire_read_contacts(config, api_tok))
        results.append(test_aspire_opportunities_403(config, api_tok))
        results.append(test_aspire_field_names(config, api_tok))

    if not r2.passed:
        results.append(TestResult("aspire", "read /Opportunities (reporting)",
                                  False, "Skipped — reporting auth failed"))
    else:
        rep_tok, _ = aspire_authenticate(
            config["api_base_url"], config["reporting_client_id"],
            config["reporting_secret"])
        results.append(test_aspire_opportunities_reporting(config, rep_tok))

    return results


# ============================================================
# WhatConverts Contract Tests
# ============================================================

def wc_auth_header(config):
    creds = base64.b64encode(
        f"{config['api_token']}:{config['api_secret']}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


def test_wc_auth(config):
    url = f"https://app.whatconverts.com/api/v1/leads?leads_per_page=1&profile_id={config['profile_id']}"
    body, status = http_request("GET", url, headers=wc_auth_header(config))
    if status == 200:
        leads = body.get("leads", [])
        return TestResult("whatconverts", "auth", True,
                          f"OK ({body.get('total_leads', '?')} total leads)",
                          {"total": body.get("total_leads"),
                           "sample_keys": sorted(leads[0].keys()) if leads else []})
    return TestResult("whatconverts", "auth", False,
                      f"HTTP {status}", {"body": body})


def test_wc_field_names(config, max_retries=3):
    url = f"https://app.whatconverts.com/api/v1/leads?leads_per_page=1&profile_id={config['profile_id']}"
    for attempt in range(max_retries):
        body, status = http_request("GET", url, headers=wc_auth_header(config))
        if status != 200:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return TestResult("whatconverts", "field names", False,
                              f"HTTP {status} after {max_retries} attempts")
        leads = body.get("leads", [])
        if not leads:
            return TestResult("whatconverts", "field names", True,
                              "No leads to verify (empty profile)")
        keys = set(leads[0].keys())
        expected = {"id", "lead_type", "date_created"}
        missing = expected - keys
        if missing:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return TestResult("whatconverts", "field names", False,
                              f"Missing expected fields: {missing} (after {max_retries} attempts)",
                              {"expected": sorted(expected), "actual": sorted(keys)})
        retried = f" (passed on attempt {attempt + 1})" if attempt > 0 else ""
        return TestResult("whatconverts", "field names", True,
                          f"Structure OK ({len(keys)} fields){retried}",
                          {"keys": sorted(keys)})


def test_wc_write_persist(config):
    """Idempotent write test: read quotable, write same value back, verify."""
    # Find a lead ID that has been synced (will have quotable set)
    test_lead_id = None
    sync_path = os.path.join(DATA_DIR, "roi-sync-state.json")
    if os.path.exists(sync_path):
        try:
            with open(sync_path) as f:
                sync_state = json.load(f)
            synced = sync_state.get("synced_leads", {})
            if synced:
                # Pick a lead that was synced (should have quotable=yes/no)
                test_lead_id = list(synced.keys())[-1]
        except Exception:
            pass
    # Fallback to processed-state
    if not test_lead_id:
        state_path = os.path.join(DATA_DIR, "processed-state.json")
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state = json.load(f)
                mappings = state.get("lead_mappings", {})
                if mappings:
                    test_lead_id = list(mappings.keys())[-1]
                elif state.get("processed_ids"):
                    test_lead_id = state["processed_ids"][-1]
            except Exception:
                pass

    if not test_lead_id:
        return TestResult("whatconverts", "write persistence", True,
                          "Skipped — no lead ID in state file",
                          {"skipped": True})

    headers = wc_auth_header(config)

    # Step 1: Read current state
    url = f"https://app.whatconverts.com/api/v1/leads/{test_lead_id}"
    before, status = http_request("GET", url, headers=headers)
    if status != 200:
        return TestResult("whatconverts", "write persistence", False,
                          f"Cannot read lead {test_lead_id} (HTTP {status})")

    current_quotable = (before.get("quotable") or "").strip()

    # Only test write persistence if lead already has a quotable value.
    # Writing to a lead with no value would change data (not idempotent).
    if not current_quotable:
        return TestResult("whatconverts", "write persistence", True,
                          f"Skipped — lead {test_lead_id} has no quotable value set "
                          f"(cannot test idempotently)",
                          {"lead_id": test_lead_id, "skipped": True})

    # Step 2: Write same value back (idempotent — changes nothing)
    update_data = urllib.parse.urlencode({"quotable": current_quotable}).encode()
    write_url = f"https://app.whatconverts.com/api/v1/leads/{test_lead_id}"
    _, w_status = http_request("POST", write_url, headers={
        **headers, "Content-Type": "application/x-www-form-urlencoded"},
        data=update_data)
    if w_status != 200:
        return TestResult("whatconverts", "write persistence", False,
                          f"Write returned HTTP {w_status}")

    # Step 3: Read back and verify
    after, r_status = http_request("GET", url, headers=headers)
    if r_status != 200:
        return TestResult("whatconverts", "write persistence", False,
                          f"Read-back failed (HTTP {r_status})")

    actual = (after.get("quotable") or "").strip()
    persisted = actual == current_quotable
    return TestResult("whatconverts", "write persistence", persisted,
                      f"{'Persisted' if persisted else 'DID NOT persist'}: "
                      f"wrote quotable={current_quotable}, read back={actual}",
                      {"lead_id": test_lead_id, "expected": current_quotable,
                       "actual": actual})


def run_wc_tests(config):
    results = [test_wc_auth(config)]
    if results[0].passed:
        results.append(test_wc_field_names(config))
        results.append(test_wc_write_persist(config))
    else:
        results.append(TestResult("whatconverts", "field names", False,
                                  "Skipped — auth failed"))
        results.append(TestResult("whatconverts", "write persistence", False,
                                  "Skipped — auth failed"))
    return results


# ============================================================
# CompanyCam Contract Tests
# ============================================================

def test_companycam_auth(config):
    url = f"{config['base_url']}/projects?per_page=1"
    body, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {config['access_token']}"})
    if status == 200:
        projects = body if isinstance(body, list) else [body]
        return TestResult("companycam", "auth", True,
                          f"OK ({len(projects)} project(s) returned)",
                          {"sample_keys": sorted(projects[0].keys()) if projects else []})
    return TestResult("companycam", "auth", False,
                      f"HTTP {status}", {"body": body})


def test_companycam_filter(config):
    """Verify filter[updated_after] parameter is accepted."""
    # Use a recent timestamp (1 hour ago)
    ts = int(datetime.now(timezone.utc).timestamp()) - 3600
    url = (f"{config['base_url']}/projects"
           f"?per_page=1&filter[updated_after]={ts}")
    body, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {config['access_token']}"})
    if status == 200:
        projects = body if isinstance(body, list) else [body]
        has_created_at = bool(projects and "created_at" in projects[0])
        return TestResult("companycam", "filter syntax", True,
                          f"filter[updated_after] accepted, "
                          f"created_at field {'present' if has_created_at else 'MISSING'}",
                          {"has_created_at": has_created_at,
                           "project_count": len(projects)})
    return TestResult("companycam", "filter syntax", False,
                      f"HTTP {status} — filter parameter may have changed",
                      {"status": status, "body": body})


def run_companycam_tests(config):
    results = [test_companycam_auth(config)]
    if results[0].passed:
        results.append(test_companycam_filter(config))
    else:
        results.append(TestResult("companycam", "filter syntax", False,
                                  "Skipped — auth failed"))
    return results


# ============================================================
# Google Ads Contract Tests
# ============================================================

def test_google_ads_auth(config):
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        return TestResult("google_ads", "auth", True,
                          "Skipped — google-ads library not installed",
                          {"skipped": True})

    credentials = {
        "developer_token": config["developer_token"],
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": config["refresh_token"],
        "login_customer_id": config["login_customer_id"],
        "use_proto_plus": True,
    }
    customer_id = config["customer_id"]

    try:
        client = GoogleAdsClient.load_from_dict(credentials)
        ga_service = client.get_service("GoogleAdsService")
        query = "SELECT campaign.id, campaign.name FROM campaign LIMIT 1"
        response = ga_service.search(customer_id=customer_id, query=query)
        rows = list(response)
        return TestResult("google_ads", "auth", True,
                          f"OK ({len(rows)} campaign(s))",
                          {"campaign_count": len(rows)})
    except Exception as e:
        return TestResult("google_ads", "auth", False,
                          f"Failed: {e}", {"error": str(e)})


def test_google_ads_fields(config):
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        return TestResult("google_ads", "metric fields", True,
                          "Skipped — google-ads library not installed",
                          {"skipped": True})

    credentials = {
        "developer_token": config["developer_token"],
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": config["refresh_token"],
        "login_customer_id": config["login_customer_id"],
        "use_proto_plus": True,
    }
    customer_id = config["customer_id"]

    try:
        client = GoogleAdsClient.load_from_dict(credentials)
        ga_service = client.get_service("GoogleAdsService")
        query = """
            SELECT
                metrics.cost_micros, metrics.impressions,
                metrics.clicks, metrics.conversions,
                metrics.cost_per_conversion
            FROM customer
            WHERE segments.date DURING LAST_7_DAYS
        """
        response = ga_service.search(customer_id=customer_id, query=query)
        list(response)  # consume to validate
        return TestResult("google_ads", "metric fields", True,
                          "cost_micros, impressions, clicks, conversions, "
                          "cost_per_conversion all valid")
    except Exception as e:
        return TestResult("google_ads", "metric fields", False,
                          f"Field query failed: {e}", {"error": str(e)})


def run_google_ads_tests(config):
    results = [test_google_ads_auth(config)]
    if results[0].passed and not results[0].details.get("skipped"):
        results.append(test_google_ads_fields(config))
    elif results[0].details.get("skipped"):
        results.append(TestResult("google_ads", "metric fields", True,
                                  "Skipped — google-ads library not installed",
                                  {"skipped": True}))
    else:
        results.append(TestResult("google_ads", "metric fields", False,
                                  "Skipped — auth failed"))
    return results


# ============================================================
# HubSpot Contract Tests
# ============================================================

def hubspot_headers(config):
    return {"Authorization": f"Bearer {config['access_token']}",
            "Content-Type": "application/json"}


def test_hubspot_auth(config):
    url = "https://api.hubapi.com/crm/v3/objects/contacts?limit=1"
    body, status = http_request("GET", url, headers=hubspot_headers(config))
    if status == 200:
        total = body.get("total", 0)
        return TestResult("hubspot", "auth", True,
                          f"OK ({total} total contacts)")
    return TestResult("hubspot", "auth", False,
                      f"HTTP {status}", {"body": body})


def test_hubspot_search(config):
    data = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "HAS_PROPERTY"}
        ]}],
        "properties": ["firstname", "lastname", "email", "phone"],
        "limit": 1,
    }
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    body, status = http_request("POST", url, headers=hubspot_headers(config),
                                data=data)
    if status == 200:
        return TestResult("hubspot", "search endpoint", True,
                          f"OK ({body.get('total', 0)} matches)")
    return TestResult("hubspot", "search endpoint", False,
                      f"HTTP {status}", {"body": body})


def test_hubspot_field_names(config):
    """Verify expected contact properties exist via metadata endpoint."""
    url = "https://api.hubapi.com/crm/v3/properties/contacts"
    body, status = http_request("GET", url, headers=hubspot_headers(config))
    if status != 200:
        return TestResult("hubspot", "field names", False,
                          f"HTTP {status}", {"body": body})
    results_list = body.get("results", [])
    prop_names = {p.get("name") for p in results_list}
    missing = HUBSPOT_CONTACT_PROPS - prop_names
    if missing:
        return TestResult("hubspot", "field names", False,
                          f"Missing properties: {missing}",
                          {"missing": sorted(missing),
                           "available": sorted(prop_names)})
    return TestResult("hubspot", "field names", True,
                      f"firstname, lastname, email, phone confirmed",
                      {"total_props": len(prop_names)})


def test_hubspot_owners(config):
    """Verify hardcoded owner IDs are still valid."""
    missing = []
    for oid in sorted(HUBSPOT_OWNER_IDS):
        url = f"https://api.hubapi.com/crm/v3/owners/{oid}"
        body, status = http_request("GET", url, headers=hubspot_headers(config))
        if status != 200:
            missing.append(oid)
    if missing:
        return TestResult("hubspot", "owners", False,
                          f"Invalid owner IDs: {missing}",
                          {"missing": missing})
    return TestResult("hubspot", "owners", True,
                      f"Owner IDs {sorted(HUBSPOT_OWNER_IDS)} valid")


def run_hubspot_tests(config):
    results = [test_hubspot_auth(config)]
    if results[0].passed:
        results.append(test_hubspot_search(config))
        results.append(test_hubspot_field_names(config))
        results.append(test_hubspot_owners(config))
    else:
        for name in ["search endpoint", "field names", "owners"]:
            results.append(TestResult("hubspot", name, False,
                                      "Skipped — auth failed"))
    return results


# ============================================================
# Test Runner
# ============================================================

def run_all_tests(api_filter=None):
    """Run contract tests for all (or filtered) APIs. Returns list[TestResult]."""
    results = []
    apis = {
        "aspire": (load_aspire_config, run_aspire_tests),
        "whatconverts": (load_wc_config, run_wc_tests),
        "companycam": (load_companycam_config, run_companycam_tests),
        "google_ads": (load_google_ads_config, run_google_ads_tests),
        "hubspot": (load_hubspot_config, run_hubspot_tests),
    }

    for api_name, (loader, runner) in apis.items():
        if api_filter and api_name != api_filter:
            continue
        config = loader()
        if config is None:
            results.append(TestResult(api_name, "config", False,
                                      "No credentials found"))
            continue
        try:
            results.extend(runner(config))
        except Exception as e:
            results.append(TestResult(api_name, "unexpected error", False,
                                      f"Exception: {e}"))
    return results


def print_results(results):
    """Pretty-print test results."""
    print("\n=== API Health Monitor ===\n")
    current_api = None
    for r in results:
        if r.api != current_api:
            current_api = r.api
            print()
        status = "PASS" if r.passed else "FAIL"
        label = f"[{r.api.upper()}] {r.test}"
        dots = "." * max(2, 52 - len(label))
        print(f"  {label} {dots} {status}  {r.message}")

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\nResult: {passed}/{total} PASSED\n")


# ============================================================
# Diagnostics
# ============================================================

def camel_parts(name):
    """Split CamelCase into parts: 'MobilePhone' -> {'mobile', 'phone'}."""
    parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)", name)
    return {p.lower() for p in parts}


def find_rename_candidates(expected_field, actual_fields):
    """Find fields that might be renames of expected_field.
    Returns list of (field_name, confidence) sorted by confidence desc.
    """
    expected_parts = camel_parts(expected_field)
    if not expected_parts:
        return []
    candidates = []
    for field in actual_fields:
        field_parts = camel_parts(field)
        overlap = expected_parts & field_parts
        if overlap:
            score = len(overlap) / len(expected_parts)
            candidates.append((field, score))
    return sorted(candidates, key=lambda x: -x[1])


def diagnose_aspire(failed_results):
    """Diagnose Aspire failures. Returns list[DriftReport]."""
    drifts = []
    config = load_aspire_config()
    if not config:
        return drifts

    # Try to get a token (use whichever client works)
    token = None
    for cid, sec in [(config.get("api_client_id"), config.get("api_secret")),
                     (config.get("reporting_client_id"), config.get("reporting_secret"))]:
        if cid and sec:
            t, s = aspire_authenticate(config["api_base_url"], cid, sec)
            if t:
                token = t
                break

    if not token:
        drifts.append(DriftReport("aspire", "auth_failure", "JWT auth", None, 1.0,
                                  SCRIPT_TARGETS["aspire"]))
        return drifts

    # Fetch contacts and check field names
    url = f"{config['api_base_url']}/Contacts?$top=1"
    body, status = http_request("GET", url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    if status == 200:
        items = body if isinstance(body, list) else [body]
        if items:
            actual_keys = set(items[0].keys())
            for expected in ["MobilePhone", "HomePhone", "OfficePhone"]:
                if expected not in actual_keys:
                    candidates = find_rename_candidates(expected, actual_keys)
                    best = candidates[0] if candidates else (None, 0)
                    drifts.append(DriftReport(
                        "aspire", "field_rename", expected, best[0], best[1],
                        SCRIPT_TARGETS["aspire"]))

    return drifts


def diagnose_whatconverts(failed_results):
    """Diagnose WhatConverts failures."""
    drifts = []
    config = load_wc_config()
    if not config:
        return drifts

    # Check if it's a write persistence issue
    for r in failed_results:
        if r.test == "write persistence" and not r.passed:
            drifts.append(DriftReport(
                "whatconverts", "write_persistence", "POST update persists",
                "POST update does NOT persist", 1.0,
                SCRIPT_TARGETS["whatconverts"]))

    # Check field structure
    url = f"https://app.whatconverts.com/api/v1/leads?leads_per_page=1&profile_id={config['profile_id']}"
    body, status = http_request("GET", url, headers=wc_auth_header(config))
    if status == 200:
        leads = body.get("leads", [])
        if leads:
            actual_keys = set(leads[0].keys())
            for expected in ["id", "lead_type", "date_created", "quotable",
                             "quote_value", "sales_value"]:
                if expected not in actual_keys:
                    candidates = find_rename_candidates(expected, actual_keys)
                    best = candidates[0] if candidates else (None, 0)
                    drifts.append(DriftReport(
                        "whatconverts", "field_rename", expected, best[0], best[1],
                        SCRIPT_TARGETS["whatconverts"]))
    return drifts


def diagnose_companycam(failed_results):
    """Diagnose CompanyCam failures."""
    drifts = []
    config = load_companycam_config()
    if not config:
        return drifts

    # Try alternate filter syntaxes
    base = config["base_url"]
    token_hdr = {"Authorization": f"Bearer {config['access_token']}"}
    ts = int(datetime.now(timezone.utc).timestamp()) - 86400

    filter_variants = [
        ("filter[updated_after]", f"{base}/projects?per_page=1&filter[updated_after]={ts}"),
        ("updated_after", f"{base}/projects?per_page=1&updated_after={ts}"),
        ("since", f"{base}/projects?per_page=1&since={ts}"),
    ]

    working_filter = None
    for name, url in filter_variants:
        _, status = http_request("GET", url, headers=token_hdr)
        if status == 200:
            working_filter = name
            break

    if working_filter and working_filter != "filter[updated_after]":
        drifts.append(DriftReport(
            "companycam", "filter_rename", "filter[updated_after]",
            working_filter, 0.9, SCRIPT_TARGETS["companycam"]))

    return drifts


def diagnose_google_ads(failed_results):
    """Diagnose Google Ads failures by parsing error messages."""
    drifts = []
    for r in failed_results:
        error_msg = r.details.get("error", "")
        # Google Ads exceptions often contain field suggestions
        if "is not a valid" in error_msg or "Unrecognized field" in error_msg:
            drifts.append(DriftReport(
                "google_ads", "field_deprecation", "unknown",
                error_msg[:200], 0.5, SCRIPT_TARGETS["google_ads"]))
    return drifts


def diagnose_hubspot(failed_results):
    """Diagnose HubSpot failures using metadata endpoints."""
    drifts = []
    config = load_hubspot_config()
    if not config:
        return drifts

    headers = hubspot_headers(config)

    # Check contact property list
    url = "https://api.hubapi.com/crm/v3/properties/contacts"
    body, status = http_request("GET", url, headers=headers)
    if status == 200:
        prop_names = {p.get("name") for p in body.get("results", [])}
        for expected in HUBSPOT_CONTACT_PROPS:
            if expected not in prop_names:
                # Look for close matches
                candidates = [(p, 0.8) for p in prop_names
                              if expected in p or p in expected]
                best = candidates[0] if candidates else (None, 0)
                drifts.append(DriftReport(
                    "hubspot", "field_rename", expected, best[0], best[1],
                    SCRIPT_TARGETS["hubspot"]))

    return drifts


def run_diagnostics(failed_results):
    """Run diagnostics for all failed APIs. Returns list[DriftReport]."""
    drifts = []
    failed_apis = {r.api for r in failed_results if not r.passed}

    diagnostics = {
        "aspire": diagnose_aspire,
        "whatconverts": diagnose_whatconverts,
        "companycam": diagnose_companycam,
        "google_ads": diagnose_google_ads,
        "hubspot": diagnose_hubspot,
    }

    for api in failed_apis:
        api_failures = [r for r in failed_results if r.api == api and not r.passed]
        if api in diagnostics:
            try:
                drifts.extend(diagnostics[api](api_failures))
            except Exception as e:
                log(f"Diagnostic error for {api}: {e}")

    return drifts


def print_drifts(drifts):
    """Pretty-print drift reports."""
    if not drifts:
        print("\nNo actionable drift detected.\n")
        return
    print(f"\n=== Drift Report ({len(drifts)} issue(s)) ===\n")
    for d in drifts:
        conf_pct = f"{d.confidence * 100:.0f}%"
        print(f"  [{d.api.upper()}] {d.drift_type}: "
              f"{d.old_value} -> {d.new_value} (confidence: {conf_pct})")
        print(f"           Affects: {', '.join(d.affected_scripts)}")
    print()


# ============================================================
# Self-Healing (Patching)
# ============================================================

def patch_field_in_file(filepath, old_field, new_field, dry_run=False):
    """Replace old_field with new_field using word boundaries.
    Returns count of replacements.
    """
    if not os.path.exists(filepath):
        return 0
    with open(filepath) as f:
        content = f.read()

    pattern = r"(?<![a-zA-Z0-9_])" + re.escape(old_field) + r"(?![a-zA-Z0-9_])"
    new_content, count = re.subn(pattern, new_field, content)

    if count > 0 and not dry_run:
        with open(filepath, "w") as f:
            f.write(new_content)
    return count


def apply_patches(drifts, dry_run=False):
    """Apply patches for high-confidence drift. Returns list of change dicts."""
    changes = []
    for d in drifts:
        if d.drift_type != "field_rename" or not d.new_value or d.confidence < 0.8:
            continue
        for script_name in d.affected_scripts:
            filepath = os.path.join(SCRIPT_DIR, script_name)
            count = patch_field_in_file(filepath, d.old_value, d.new_value, dry_run)
            if count > 0:
                changes.append({
                    "file": script_name,
                    "api": d.api,
                    "old": d.old_value,
                    "new": d.new_value,
                    "replacements": count,
                    "dry_run": dry_run,
                })
                action = "Would replace" if dry_run else "Replaced"
                log(f"  {action} {d.old_value} -> {d.new_value} "
                    f"in {script_name} ({count} occurrence(s))")

    # Also patch CLAUDE.md API reference
    claude_md = os.path.join(PROJECT_DIR, "CLAUDE.md")
    for d in drifts:
        if d.drift_type == "field_rename" and d.new_value and d.confidence >= 0.8:
            count = patch_field_in_file(claude_md, d.old_value, d.new_value, dry_run)
            if count > 0:
                changes.append({
                    "file": "CLAUDE.md", "api": d.api,
                    "old": d.old_value, "new": d.new_value,
                    "replacements": count, "dry_run": dry_run,
                })

    return changes


def create_heal_branch(drifts, changes):
    """Create git branch, commit patches, push, and open PR.
    Returns PR URL or None.
    """
    if not changes:
        return None

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"api-drift/{ts}"

    # Check for existing open drift PR
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--search", "api-drift in:title", "--state", "open",
             "--json", "number,headRefName", "--limit", "1"],
            capture_output=True, text=True, cwd=PROJECT_DIR)
        existing = json.loads(result.stdout) if result.returncode == 0 else []
    except Exception:
        existing = []

    if existing:
        pr_num = existing[0]["number"]
        # Add comment to existing PR instead of creating a new one
        comment_body = f"Additional drift detected at {ts}:\n\n"
        for c in changes:
            comment_body += f"- `{c['old']}` -> `{c['new']}` in `{c['file']}`\n"

        try:
            subprocess.run(
                ["gh", "pr", "comment", str(pr_num), "--body", comment_body],
                capture_output=True, text=True, cwd=PROJECT_DIR)
            log(f"Added comment to existing PR #{pr_num}")
        except Exception as e:
            log(f"Failed to comment on PR #{pr_num}: {e}")
        return None

    # Create new branch and PR
    try:
        subprocess.run(["git", "checkout", "-b", branch],
                        capture_output=True, text=True, cwd=PROJECT_DIR, check=True)
    except subprocess.CalledProcessError as e:
        log(f"Failed to create branch: {e}")
        return None

    # Stage changed files
    changed_files = list({c["file"] for c in changes})
    for f in changed_files:
        filepath = f if f == "CLAUDE.md" else f"scripts/{f}"
        subprocess.run(["git", "add", filepath],
                        capture_output=True, text=True, cwd=PROJECT_DIR)

    # Build commit message
    drift_summary = []
    for d in drifts:
        if d.new_value and d.confidence >= 0.8:
            drift_summary.append(f"{d.api}: {d.old_value} -> {d.new_value}")

    commit_msg = (f"fix(api): Auto-heal API drift\n\n"
                  f"Detected and patched:\n" +
                  "\n".join(f"  - {s}" for s in drift_summary) +
                  f"\n\nAffected files: {', '.join(changed_files)}")

    subprocess.run(["git", "commit", "-m", commit_msg],
                    capture_output=True, text=True, cwd=PROJECT_DIR)

    # Push
    push_result = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        capture_output=True, text=True, cwd=PROJECT_DIR)
    if push_result.returncode != 0:
        log(f"Push failed: {push_result.stderr}")
        subprocess.run(["git", "checkout", "main"],
                        capture_output=True, text=True, cwd=PROJECT_DIR)
        return None

    # Build PR body
    table_rows = ""
    for c in changes:
        table_rows += (f"| {c['api']} | field rename | `{c['old']}` | "
                       f"`{c['new']}` | `{c['file']}` |\n")

    pr_body = (
        "## Summary\n"
        "- API contract tests detected field name drift\n"
        "- Auto-patched affected scripts with correct field names\n"
        "- All changes are word-boundary replacements (no logic changes)\n\n"
        "## Drift Details\n"
        "| API | Type | Old | New | File |\n"
        "|-----|------|-----|-----|------|\n"
        f"{table_rows}\n"
        "## Test plan\n"
        "- [ ] Review changed scripts for correctness\n"
        "- [ ] Run `python scripts/api-health-monitor.py --test` locally\n"
        "- [ ] Verify lead-monitor still processes leads correctly\n\n"
        "Generated by `api-health-monitor.py --heal`"
    )

    # Create PR
    pr_result = subprocess.run(
        ["gh", "pr", "create",
         "--title", f"fix(api): Drift detected — {drift_summary[0] if drift_summary else 'field changes'}",
         "--body", pr_body],
        capture_output=True, text=True, cwd=PROJECT_DIR)

    # Return to main
    subprocess.run(["git", "checkout", "main"],
                    capture_output=True, text=True, cwd=PROJECT_DIR)

    if pr_result.returncode == 0:
        pr_url = pr_result.stdout.strip()
        log(f"PR created: {pr_url}")
        return pr_url
    else:
        log(f"PR creation failed: {pr_result.stderr}")
        return None


# ============================================================
# Entry Point
# ============================================================

def get_api_filter():
    """Parse --api flag. Returns api name or None."""
    if "--api" in sys.argv:
        idx = sys.argv.index("--api")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


def main():
    api_filter = get_api_filter()
    mode = "test"
    if "--heal" in sys.argv:
        mode = "heal"
    elif "--diagnose" in sys.argv:
        mode = "diagnose"

    log(f"Mode: {mode}" + (f" (api={api_filter})" if api_filter else "") +
        (" [DRY RUN]" if DRY_RUN else ""))

    # Step 1: Run contract tests
    results = run_all_tests(api_filter)
    print_results(results)

    failed = [r for r in results if not r.passed]
    if not failed:
        log("All tests passed. Nothing to diagnose or heal.")
        return 0

    if mode == "test":
        return 1

    # Step 2: Diagnose failures
    log(f"\nDiagnosing {len(failed)} failure(s)...")
    drifts = run_diagnostics(failed)
    print_drifts(drifts)

    if mode == "diagnose":
        return 1 if drifts else 0

    # Step 3: Heal — apply patches and create PR
    if not drifts:
        log("Tests failed but no actionable drift detected. Manual investigation needed.")
        return 1

    patchable = [d for d in drifts if d.drift_type == "field_rename"
                 and d.new_value and d.confidence >= 0.8]
    low_conf = [d for d in drifts if d.confidence < 0.8 and d.new_value]

    if low_conf:
        log("Low-confidence drift (manual review needed):")
        for d in low_conf:
            log(f"  {d.api}: {d.old_value} -> {d.new_value} "
                f"(confidence: {d.confidence * 100:.0f}%)")

    if not patchable:
        log("No high-confidence patches to apply.")
        return 1

    log(f"\nApplying {len(patchable)} patch(es)..." + (" [DRY RUN]" if DRY_RUN else ""))
    changes = apply_patches(drifts, dry_run=DRY_RUN)

    if not changes:
        log("No changes needed (fields not found in target scripts).")
        return 1

    if DRY_RUN:
        log("Dry run complete. No files modified, no PR created.")
        return 0

    log("Creating branch and PR...")
    pr_url = create_heal_branch(drifts, changes)
    return 0 if pr_url else 1


if __name__ == "__main__":
    sys.exit(main())
