#!/usr/bin/env python3
"""Shared auth module for Google Business Profile API.

Import:
  from gbp_auth import get_service, get_config

Test:
  python3 scripts/gbp-auth.py --test
"""

import json, os, sys
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import requests as _requests

CONFIG_FILE = os.path.expanduser("~/.config/gbp/config.json")
GMAIL_CONFIG_FILE = os.path.expanduser("~/.config/gbp/gmail-config.json")
CLOUD_MODE = bool(os.environ.get("GBP_CLIENT_ID"))


def get_config():
    """Load GBP config from disk or environment."""
    if CLOUD_MODE:
        return {
            "client_id": os.environ["GBP_CLIENT_ID"],
            "client_secret": os.environ["GBP_CLIENT_SECRET"],
            "refresh_token": os.environ["GBP_REFRESH_TOKEN"],
            "account_id": os.environ["GBP_ACCOUNT_ID"],
            "location_id": os.environ["GBP_LOCATION_ID"],
        }
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: Config not found at {CONFIG_FILE}")
        print("Run: python3 ~/.config/gbp/get-refresh-token.py")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_credentials():
    """Build OAuth credentials from config."""
    config = get_config()
    return Credentials(
        token=None,
        refresh_token=config["refresh_token"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )


def get_service(api="mybusinessbusinessinformation", version="v1"):
    """Build an authenticated GBP API service.

    Common APIs:
      mybusinessbusinessinformation v1 - Profile data, locations
      mybusinessaccountmanagement v1   - Account management
      mybusinessverifications v1       - Verification
    """
    return build(api, version, credentials=get_credentials())


def get_location_service():
    """Shortcut: return (service, account_id, location_id)."""
    config = get_config()
    service = get_service()
    return service, config["account_id"], config["location_id"]


def get_v4_auth():
    """Get authenticated headers and base path for My Business v4 REST API.

    Returns (headers, base_path) where base_path is:
      https://mybusiness.googleapis.com/v4/accounts/.../locations/...
    """
    config = get_config()
    creds = get_credentials()
    creds.refresh(Request())
    headers = {"Authorization": f"Bearer {creds.token}"}
    base = f"https://mybusiness.googleapis.com/v4/{config['account_id']}/{config['location_id']}"
    return headers, base


def v4_get(endpoint="", params=None):
    """GET request to My Business v4 REST API. Returns parsed JSON."""
    headers, base = get_v4_auth()
    url = f"{base}/{endpoint}" if endpoint else base
    r = _requests.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.json()


def v4_post(endpoint, body):
    """POST request to My Business v4 REST API. Returns parsed JSON."""
    headers, base = get_v4_auth()
    r = _requests.post(f"{base}/{endpoint}", headers=headers, json=body)
    r.raise_for_status()
    return r.json()


def v4_put(url, body):
    """PUT request to My Business v4 REST API (full URL). Returns parsed JSON."""
    headers, _ = get_v4_auth()
    r = _requests.put(f"https://mybusiness.googleapis.com/v4/{url}", headers=headers, json=body)
    r.raise_for_status()
    return r.json()


def get_gmail_config():
    """Load Gmail config from disk or environment."""
    if CLOUD_MODE and os.environ.get("GBP_GMAIL_CLIENT_ID"):
        return {
            "client_id": os.environ["GBP_GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GBP_GMAIL_CLIENT_SECRET"],
            "refresh_token": os.environ["GBP_GMAIL_REFRESH_TOKEN"],
        }
    if not os.path.exists(GMAIL_CONFIG_FILE):
        print(f"ERROR: Gmail config not found at {GMAIL_CONFIG_FILE}")
        print("Run: python3 ~/.config/gbp/get-gmail-token.py")
        sys.exit(1)
    with open(GMAIL_CONFIG_FILE) as f:
        return json.load(f)


def get_gmail_credentials():
    """Build OAuth credentials for Gmail API."""
    config = get_gmail_config()
    return Credentials(
        token=None,
        refresh_token=config["refresh_token"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )


def get_gmail_service():
    """Build an authenticated Gmail API service."""
    return build("gmail", "v1", credentials=get_gmail_credentials())


def gmail_search(query, max_results=10):
    """Search Gmail inbox by query string. Returns list of message stubs."""
    service = get_gmail_service()
    result = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    return result.get("messages", [])


def gmail_get_message(msg_id):
    """Get full message content by ID. Returns the message resource."""
    service = get_gmail_service()
    return service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()


def gmail_mark_read(msg_id):
    """Mark a Gmail message as read by removing UNREAD label."""
    service = get_gmail_service()
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


if __name__ == "__main__" and "--test" in sys.argv:
    print("Testing GBP API connection...")
    config = get_config()
    service = get_service()
    try:
        location = service.locations().get(
            name=config["location_id"],
            readMask="name,title,storefrontAddress,phoneNumbers,websiteUri,regularHours,metadata"
        ).execute()
        print(f"  Business: {location.get('title', 'Unknown')}")
        if location.get("storefrontAddress"):
            addr = location["storefrontAddress"]
            print(f"  Address: {', '.join(addr.get('addressLines', []))}, {addr.get('locality', '')}")
        if location.get("phoneNumbers", {}).get("primaryPhone"):
            print(f"  Phone: {location['phoneNumbers']['primaryPhone']}")
        if location.get("websiteUri"):
            print(f"  Website: {location['websiteUri']}")
        if location.get("metadata", {}).get("totalReviewCount"):
            print(f"  Reviews: {location['metadata']['totalReviewCount']}")
        print("\nGBP API connection successful.")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__" and "--test-gmail" in sys.argv:
    print("Testing Gmail API connection...")
    try:
        msgs = gmail_search("is:inbox", max_results=3)
        print(f"  Found {len(msgs)} recent message(s) in inbox.")
        for m in msgs:
            full = gmail_get_message(m["id"])
            headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            print(f"  - {headers.get('Subject', '(no subject)')}")
        print("\nGmail API connection successful.")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
