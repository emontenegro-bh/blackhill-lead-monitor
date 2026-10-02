#!/usr/bin/env python3
"""Sync new marketable Aspire contacts to Mailchimp MASTER LIST 2025.

Closes the gap where contacts created in Aspire outside the WhatConverts pipeline
(manual entry, Microsoft Bookings, builder/realtor outreach, HubSpot sync) never
reach the Mailchimp audience.

Covers customers (type 6), prospects (type 8) and untyped contacts. Until
2026-10-02 this queried type 8 alone, so no customer ever reached Mailchimp --
324 of them were missing when that was measured. Employees, vendors and subs are
still excluded, as are role/AP inboxes and the municipal domains in
mailchimp_filters.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

from mailchimp_filters import normalize_email, suppression_reason

STATE_NAME = "aspire-mailchimp-backfill"
ASPIRE_API_URL = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")

# ContactTypeID -> the tag that says where the contact came from. Anything not
# listed here (7=Employee, 9=Vendor, 10=Sub) is not marketed to.
TYPE_TAGS = {6: "aspire-customer", 8: "web-lead", None: "aspire-unclassified"}

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
    """Return contacts with ContactID > last_contact_id.

    Type is filtered client-side in main(). OData comparisons against a null
    ContactTypeID are unreliable here and untyped contacts are a real group --
    89 of them were missing from Mailchimp on 2026-10-02 -- so the query stays
    broad and the filtering stays in Python where it is testable.
    """
    out = []
    skip = 0
    while True:
        filt = f"ContactID gt {last_contact_id}"
        select = ("ContactID,ContactTypeID,FirstName,LastName,Email,MobilePhone,"
                  "Notes,Active")
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

def mailchimp_upsert(email, first_name, last_name, phone, service_tag=None,
                     type_tag="web-lead"):
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
        "tags": [type_tag, "aspire-sync"] + ([service_tag] if service_tag else []),
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
    """Resume point for the Aspire -> Mailchimp sync.

    The default here is a loaded gun and deserves an explanation. 2644 was the
    highest ContactID synced in the 2026-05-29 catch-up; it is only correct as
    a cold start. If it were ever returned while the real cursor sits far
    ahead -- 2820 as of 2026-08-22 -- the next run would re-sync every contact
    in between, re-tagging them in Mailchimp and potentially re-enrolling real
    customers into the sprinkler and irrigation-plan journeys. Duplicate
    marketing email to customers is the failure mode, not a duplicate row.

    That is survivable precisely because db.load_state() raises rather than
    returning the default when Supabase is unreachable. The default is reached
    only when the row genuinely does not exist, which after the 2026-08-22
    migration means someone deleted it.
    """
    return db.load_state(STATE_NAME, default={
        "last_contact_id": 2644, "last_run": None, "last_synced_count": 0,
    })


def save_state(state):
    db.save_state(STATE_NAME, state)


# --- Main ---

def main():
    state = load_state()
    last_id = state.get("last_contact_id", 0)
    log(f"Last synced ContactID: {last_id}")

    token = aspire_authenticate()
    contacts = aspire_get_new_contacts(token, last_id)
    log(f"Aspire returned {len(contacts)} new contacts")

    synced = 0
    skipped = {}
    errors = []
    max_id = last_id

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for c in contacts:
        cid = c.get("ContactID", 0)
        # The cursor advances past every contact seen, including skipped ones.
        # Leaving it behind would re-examine employees and AP inboxes forever.
        if cid > max_id:
            max_id = cid

        ctype = c.get("ContactTypeID")
        if ctype not in TYPE_TAGS:
            skip("type-not-marketed")
            continue
        if not c.get("Active"):
            skip("inactive")
            continue

        email = normalize_email(c.get("Email"))
        reason = suppression_reason(email)
        if reason:
            skip(reason)
            continue

        fname = c.get("FirstName") or ""
        lname = c.get("LastName") or ""
        phone = c.get("MobilePhone") or ""
        service_tag = service_tag_from_notes(c.get("Notes") or "")
        type_tag = TYPE_TAGS[ctype]

        if DRY_RUN:
            log(f"  DRY RUN: would sync ContactID={cid} {email} "
                f"type={type_tag} service={service_tag}")
            synced += 1
            continue

        try:
            status = mailchimp_upsert(email, fname, lname, phone, service_tag, type_tag)
            log(f"  ContactID={cid} {email} -> {status} "
                f"(type={type_tag} service={service_tag})")
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
        "skipped": skipped,
        "errors": errors,
        "new_last_contact_id": max_id,
        "dry_run": DRY_RUN,
    }
    log("SUMMARY: " + json.dumps(summary))

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    with db.track("aspire-mailchimp-backfill"):
        main()
