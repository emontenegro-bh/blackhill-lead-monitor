#!/usr/bin/env python3
"""One-time catch-up: add Aspire contacts that Mailchimp never received.

The daily sync (aspire-mailchimp-backfill.py) is forward-only -- it stores the
highest ContactID it has seen and never looks back -- and until 2026-10-02 it
queried ContactTypeID 8 (Prospect) only. Customers are type 6 and were never
synced by anything. The result, measured 2026-10-02: 546 active Aspire contacts
with a valid email address had no Mailchimp record at all, 324 of them
customers.

This script closes that historical gap once. The daily sync handles everything
created from here on.

Dry run by default. Pass --live to write.

Environment:
  ASPIRE_CLIENT_ID, ASPIRE_SECRET    Aspire API credentials
  MAILCHIMP_API_KEY                  Mailchimp API key
  MAILCHIMP_SERVER                   e.g. 'us20'
  MAILCHIMP_LIST_ID                  Master list id

Falls back to ~/.config/aspire/config.json and ~/.config/mailchimp/config.json
when the env vars are absent, matching the other scripts in this repo.
"""

import base64
import collections
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mailchimp_filters import normalize_email, suppression_reason

ASPIRE_API_URL = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
LIVE = "--live" in sys.argv

# Types worth marketing to. 7=Employee, 9=Vendor, 10=Sub are deliberately out.
TYPE_TAGS = {
    6: "aspire-customer",
    8: "web-lead",
    None: "aspire-unclassified",
}

# New tag, created by this run. Deliberately one that has never existed on the
# audience: a tag-added trigger only fires on a tag a journey is listening for,
# so a brand-new name cannot enrol 500 people into the sprinkler drip or the
# plans sequence. It also gives Evelin a handle to watch this cohort's
# unsubscribe and complaint rates separately on its first send.
BACKFILL_TAG = "aspire-backfill-2026-10"

HOME = os.path.expanduser("~")


def log(msg):
    print(msg, flush=True)


def _cfg(service, keys):
    """Env vars first, then ~/.config/<service>/config.json. Same shape as db.py."""
    out = {}
    path = os.path.join(HOME, ".config", service, "config.json")
    disk = {}
    if os.path.exists(path):
        with open(path) as fh:
            disk = json.load(fh)
    for env_name, disk_name in keys:
        val = (os.environ.get(env_name) or "").strip()
        if not val:
            val = str(disk.get(disk_name) or "").strip()
        if not val:
            raise RuntimeError(f"missing {env_name} (and {service} config '{disk_name}')")
        out[env_name] = val
    return out


# --- Aspire ---

def aspire_token(cfg):
    data = json.dumps({
        "ClientId": cfg["ASPIRE_CLIENT_ID"], "Secret": cfg["ASPIRE_SECRET"],
    }).encode()
    req = urllib.request.Request(
        f"{ASPIRE_API_URL}/Authorization", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read().decode()).get("Token", "")
    if not token:
        raise RuntimeError("Aspire auth returned no token")
    return token


def aspire_all_contacts(token, page_size=500):
    out = []
    skip = 0
    while True:
        query = (
            "$select=ContactID,ContactTypeID,ContactTypeName,FirstName,LastName,"
            "Email,MobilePhone,Active"
            f"&$orderby=ContactID asc&$top={page_size}&$skip={skip}"
        )
        safe = urllib.parse.quote(query, safe="=&$,'()@")
        req = urllib.request.Request(
            f"{ASPIRE_API_URL}/Contacts?{safe}",
            headers={"Authorization": f"Bearer {token}"}, method="GET")
        with urllib.request.urlopen(req, timeout=90) as resp:
            page = json.loads(resp.read().decode())
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
    return out


# --- Mailchimp ---

def mc_request(cfg, path, method="GET", payload=None):
    key = cfg["MAILCHIMP_API_KEY"]
    url = f"https://{cfg['MAILCHIMP_SERVER']}.api.mailchimp.com/3.0/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"anystring:{key}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode())


def mc_known_emails(cfg, list_id):
    """Every address the audience has ever held, in any status.

    Unsubscribed, cleaned and archived contacts must count as 'known' or the
    backfill would re-add people who already opted out.
    """
    known = set()
    for status in ("subscribed", "unsubscribed", "cleaned", "archived",
                   "transactional", "pending"):
        offset = 0
        while True:
            d = mc_request(cfg, f"lists/{list_id}/members?status={status}"
                                f"&count=1000&offset={offset}"
                                f"&fields=members.email_address")
            members = d.get("members", [])
            if not members:
                break
            known.update(m["email_address"].lower() for m in members)
            offset += len(members)
            if len(members) < 1000:
                break
    return known


def mc_upsert(cfg, list_id, email, first, last, phone, tags):
    email_hash = hashlib.md5(email.lower().encode()).hexdigest()
    payload = {
        "email_address": email,
        "status_if_new": "subscribed",
        "merge_fields": {"FNAME": first or "", "LNAME": last or "", "PHONE": phone or ""},
        "tags": tags,
    }
    body = mc_request(cfg, f"lists/{list_id}/members/{email_hash}", "PUT", payload)
    return body.get("status", "unknown")


# --- Main ---

def main():
    aspire_cfg = _cfg("aspire", [("ASPIRE_CLIENT_ID", "api_client_id"),
                                 ("ASPIRE_SECRET", "api_secret")])
    mc_cfg = _cfg("mailchimp", [("MAILCHIMP_API_KEY", "api_key")])
    mc_cfg["MAILCHIMP_SERVER"] = (os.environ.get("MAILCHIMP_SERVER")
                                  or mc_cfg["MAILCHIMP_API_KEY"].split("-")[-1]).strip()
    list_id = (os.environ.get("MAILCHIMP_LIST_ID") or "4a35469b32").strip()

    log(f"mode: {'LIVE' if LIVE else 'DRY RUN'}  list: {list_id}")

    contacts = aspire_all_contacts(aspire_token(aspire_cfg))
    log(f"Aspire contacts: {len(contacts)}")

    known = mc_known_emails(mc_cfg, list_id)
    log(f"Mailchimp knows {len(known)} addresses (all statuses)")

    reasons = collections.Counter()
    queue = {}
    for c in contacts:
        ctype = c.get("ContactTypeID")
        if ctype not in TYPE_TAGS:
            reasons["type-not-marketed"] += 1
            continue
        if not c.get("Active"):
            reasons["inactive"] += 1
            continue
        email = normalize_email(c.get("Email"))
        reason = suppression_reason(email)
        if reason:
            reasons[reason] += 1
            continue
        if email in known:
            reasons["already-in-mailchimp"] += 1
            continue
        if email in queue:
            reasons["duplicate-in-aspire"] += 1
            continue
        queue[email] = {
            "contact_id": c.get("ContactID"),
            "first": c.get("FirstName") or "",
            "last": c.get("LastName") or "",
            "phone": c.get("MobilePhone") or "",
            "tags": ["aspire-sync", BACKFILL_TAG, TYPE_TAGS[ctype]],
        }

    log("\nskipped:")
    for reason, n in reasons.most_common():
        log(f"  {reason:24s} {n}")
    by_tag = collections.Counter(v["tags"][2] for v in queue.values())
    log(f"\nto add: {len(queue)}")
    for tag, n in by_tag.most_common():
        log(f"  {tag:24s} {n}")

    if not LIVE:
        for email, v in list(queue.items())[:10]:
            log(f"  DRY RUN would add {email} ({v['tags'][2]})")
        log("\nDry run. Re-run with --live to write.")
        return 0

    added, errors = 0, []
    for email, v in queue.items():
        try:
            status = mc_upsert(mc_cfg, list_id, email, v["first"], v["last"],
                               v["phone"], v["tags"])
            added += 1
            if added % 50 == 0:
                log(f"  ...{added}/{len(queue)}")
            if status != "subscribed":
                errors.append({"email": email, "status": status})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200] if e.fp else ""
            errors.append({"email": email, "code": e.code, "body": body})
            log(f"  ERROR {email}: {e.code} {body}")
        except Exception as e:  # noqa: BLE001 - want the whole run to survive one bad row
            errors.append({"email": email, "error": str(e)})
            log(f"  ERROR {email}: {e}")
        time.sleep(0.1)

    # Verify against the audience rather than trusting the write loop.
    after = mc_known_emails(mc_cfg, list_id)
    still_missing = [e for e in queue if e not in after]
    log(f"\nattempted: {len(queue)}  added: {added}  errors: {len(errors)}")
    log(f"verified present after run: {len(queue) - len(still_missing)}/{len(queue)}")
    if still_missing:
        log(f"STILL MISSING ({len(still_missing)}): {still_missing[:10]}")
    return 1 if still_missing else 0


if __name__ == "__main__":
    sys.exit(main())
