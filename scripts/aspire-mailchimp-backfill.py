#!/usr/bin/env python3
"""Sync new Aspire web-lead contacts (ContactTypeID 8) to Mailchimp MASTER LIST 2025.

Closes the gap where contacts created in Aspire outside the WhatConverts pipeline
(manual entry, Microsoft Bookings, builder/realtor outreach, HubSpot sync) never
reach the Mailchimp audience.

Runs daily via GitHub Actions. Stateful: tracks the highest ContactID synced in
data/aspire-mailchimp-state.json and only queries new contacts each run.

Environment:
  ASPIRE_CLIENT_ID, ASPIRE_SECRET    Aspire API credentials
  MAILCHIMP_API_KEY                  Mailchimp API key
  MAILCHIMP_SERVER                   e.g. 'us20'
  MAILCHIMP_LIST_ID                  Master list id

Output: JSON summary to stdout.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SERVICE_LINE_RE = re.compile(r"^Service:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def service_tag_from_notes(notes):
    """Extract a slugified service tag from a contact's Notes field.

    Returns None when no Service line is found or the value is a non-actionable
    catch-all like "General Inquiry".
    """
    if not notes:
        return None
    m = SERVICE_LINE_RE.search(notes)
    if not m:
        return None
    value = m.group(1).strip()
    if not value or value.lower() in {"general inquiry", "general", "n/a", "unknown"}:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or None

STATE_FILE = "data/aspire-mailchimp-state.json"
ASPIRE_API_URL = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
CONTACT_TYPE_PROSPECT = 8
DRY_RUN = "--dry-run" in sys.argv


def log(msg):
    print(msg, flush=True)


# --- Aspire ---

def aspire_authenticate():
    client_id = (os.environ.get("ASPIRE_CLIENT_ID") or "").strip()
    secret = (os.environ.get("ASPIRE_SECRET") or "").strip()
    if not client_id or not secret:
        raise RuntimeError("ASPIRE_CLIENT_ID / ASPIRE_SECRET not set")

    data = json.dumps({"ClientId": client_id, "Secret": secret}).encode()
    req = urllib.request.Request(
        f"{ASPIRE_API_URL}/Authorization",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    token = body.get("Token", "")
    if not token:
        raise RuntimeError("Aspire auth returned no token")
    return token


def aspire_get_new_contacts(token, last_contact_id, page_size=200):
    """Return list of contacts with ContactID > last_contact_id, ContactTypeID=8."""
    out = []
    skip = 0
    while True:
        filt = f"ContactTypeID eq {CONTACT_TYPE_PROSPECT} and ContactID gt {last_contact_id}"
        select = "ContactID,FirstName,LastName,Email,MobilePhone,Notes"
        query = (
            f"$filter={filt}&$select={select}"
            f"&$orderby=ContactID asc&$top={page_size}&$skip={skip}"
        )
        safe = urllib.parse.quote(query, safe="=&$,'()@")
        req = urllib.request.Request(
            f"{ASPIRE_API_URL}/Contacts?{safe}",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = json.loads(resp.read().decode())
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
    return out


# --- Mailchimp ---

def mailchimp_upsert(email, first_name, last_name, phone, service_tag=None):
    api_key = os.environ.get("MAILCHIMP_API_KEY", "").strip()
    server = os.environ.get("MAILCHIMP_SERVER", "").strip()
    list_id = os.environ.get("MAILCHIMP_LIST_ID", "").strip()
    if not api_key or not server or not list_id:
        raise RuntimeError("Mailchimp env vars not fully set")

    email_hash = hashlib.md5(email.lower().encode()).hexdigest()
    url = f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/members/{email_hash}"

    payload = {
        "email_address": email,
        "status_if_new": "subscribed",
        "merge_fields": {
            "FNAME": first_name or "",
            "LNAME": last_name or "",
            "PHONE": phone or "",
        },
        "tags": ["web-lead", "aspire-sync"] + ([service_tag] if service_tag else []),
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    return body.get("status", "unknown")


# --- State ---

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    # Initial state: 2644 is the highest ContactID synced in the 2026-05-29 catch-up.
    return {"last_contact_id": 2644, "last_run": None, "last_synced_count": 0}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- Main ---

def main():
    state = load_state()
    last_id = state.get("last_contact_id", 0)
    log(f"Last synced ContactID: {last_id}")

    token = aspire_authenticate()
    contacts = aspire_get_new_contacts(token, last_id)
    log(f"Aspire returned {len(contacts)} new contacts")

    synced = 0
    skipped_no_email = 0
    errors = []
    max_id = last_id

    for c in contacts:
        cid = c.get("ContactID", 0)
        if cid > max_id:
            max_id = cid
        email = (c.get("Email") or "").strip()
        if not email or "@" not in email:
            skipped_no_email += 1
            continue

        fname = c.get("FirstName") or ""
        lname = c.get("LastName") or ""
        phone = c.get("MobilePhone") or ""
        service_tag = service_tag_from_notes(c.get("Notes") or "")

        if DRY_RUN:
            log(f"  DRY RUN: would sync ContactID={cid} {email} service={service_tag}")
            synced += 1
            continue

        try:
            status = mailchimp_upsert(email, fname, lname, phone, service_tag)
            log(f"  ContactID={cid} {email} -> {status} (service={service_tag})")
            synced += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300] if e.fp else ""
            errors.append({"contact_id": cid, "email": email, "code": e.code, "body": body})
            log(f"  ERROR ContactID={cid} {email}: {e.code} {body}")
        except Exception as e:
            errors.append({"contact_id": cid, "email": email, "error": str(e)})
            log(f"  ERROR ContactID={cid} {email}: {e}")
        time.sleep(0.1)

    state["last_contact_id"] = max_id
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["last_synced_count"] = synced
    if not DRY_RUN:
        save_state(state)

    summary = {
        "found": len(contacts),
        "synced": synced,
        "skipped_no_email": skipped_no_email,
        "errors": errors,
        "new_last_contact_id": max_id,
        "dry_run": DRY_RUN,
    }
    log("SUMMARY: " + json.dumps(summary))

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
