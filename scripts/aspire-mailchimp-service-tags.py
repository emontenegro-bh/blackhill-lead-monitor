#!/usr/bin/env python3
"""Tag Mailchimp contacts with the service divisions they have actually bought.

The contact-level sync carries a service tag only for web leads, parsed from the
"Service:" line WhatConverts writes into Aspire Notes. Customers entered directly
in Aspire have no such line, so until 2026-10-02 none of them carried a service
tag -- including all 482 added by the one-time backfill that day.

Purchase history is the better source anyway. This walks won opportunities ->
property -> property contacts -> email, and tags each contact with the divisions
on their won work.

Tag names are deliberately new (svc-*) rather than the pre-existing
'irrigation-customer'. A Customer Journey trigger cannot be read through the API,
and journey 9056 (sprinkler drip) may listen on that tag; applying it to 158 new
people would enrol them and send. A tag that has never existed cannot trigger a
journey that already does. Merging svc-irrigation into irrigation-customer is a
deliberate, separate decision.

Dry run by default. Pass --live to write.
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
HOME = os.path.expanduser("~")

# Opportunity statuses that mean the customer actually bought the work.
WON_STATUSES = {"Won", "Delivered", "Approved"}

DIVISION_TAGS = {
    "Irrigation": "svc-irrigation",
    "Maintenance": "svc-maintenance",
    "Landscape": "svc-landscape",
    "Construction": "svc-construction",
}


def log(msg):
    print(msg, flush=True)


def _cfg(service, keys):
    out = {}
    path = os.path.join(HOME, ".config", service, "config.json")
    disk = {}
    if os.path.exists(path):
        with open(path) as fh:
            disk = json.load(fh)
    for env_name, disk_name in keys:
        val = (os.environ.get(env_name) or "").strip() or str(disk.get(disk_name) or "").strip()
        if not val:
            raise RuntimeError(f"missing {env_name} (and {service} config '{disk_name}')")
        out[env_name] = val
    return out


def aspire_token(cfg):
    data = json.dumps({"ClientId": cfg["ASPIRE_CLIENT_ID"],
                       "Secret": cfg["ASPIRE_SECRET"]}).encode()
    req = urllib.request.Request(f"{ASPIRE_API_URL}/Authorization", data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read().decode()).get("Token", "")
    if not token:
        raise RuntimeError("Aspire auth returned no token")
    return token


def aspire_page(token, path, page_size=500):
    """Full pagination over an Aspire collection.

    No $select: narrowing these two endpoints returns HTTP 400 on some field
    combinations, and the payload is small enough that it is not worth the
    fragility.
    """
    out = []
    skip = 0
    while True:
        query = f"$top={page_size}&$skip={skip}"
        req = urllib.request.Request(f"{ASPIRE_API_URL}/{path}?{query}",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            page = json.loads(resp.read().decode())
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
    return out


def mc_request(cfg, path, method="GET", payload=None):
    url = f"https://{cfg['MAILCHIMP_SERVER']}.api.mailchimp.com/3.0/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"anystring:{cfg['MAILCHIMP_API_KEY']}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def mc_members(cfg, list_id):
    """email -> set of tag names, for contacts that can still be mailed."""
    out = {}
    offset = 0
    while True:
        d = mc_request(cfg, f"lists/{list_id}/members?status=subscribed&count=1000"
                            f"&offset={offset}&fields=members.email_address,members.tags")
        members = d.get("members", [])
        if not members:
            break
        for m in members:
            out[m["email_address"].lower()] = {t["name"] for t in m.get("tags", [])}
        offset += len(members)
        if len(members) < 1000:
            break
    return out


def mc_add_tags(cfg, list_id, email, tags):
    email_hash = hashlib.md5(email.lower().encode()).hexdigest()
    mc_request(cfg, f"lists/{list_id}/members/{email_hash}/tags", "POST",
               {"tags": [{"name": t, "status": "active"} for t in sorted(tags)]})


def build_service_map(token):
    """email -> set of svc-* tags, from won opportunities."""
    contacts = aspire_page(token, "Contacts")
    opps = aspire_page(token, "Opportunities")
    props = aspire_page(token, "Properties")

    cid_email = {}
    for c in contacts:
        email = normalize_email(c.get("Email"))
        if email and c.get("Active") and not suppression_reason(email):
            cid_email[c["ContactID"]] = email

    prop_contacts = collections.defaultdict(set)
    for p in props:
        for pc in (p.get("PropertyContacts") or []):
            if pc.get("ContactID"):
                prop_contacts[p["PropertyID"]].add(pc["ContactID"])

    service = collections.defaultdict(set)
    for o in opps:
        if o.get("OpportunityStatusName") not in WON_STATUSES:
            continue
        tag = DIVISION_TAGS.get(o.get("DivisionName"))
        if not tag:
            continue
        for cid in prop_contacts.get(o.get("PropertyID"), ()):
            email = cid_email.get(cid)
            if email:
                service[email].add(tag)
    log(f"Aspire: {len(contacts)} contacts, {len(opps)} opportunities, "
        f"{len(props)} properties")
    return service


def main():
    aspire_cfg = _cfg("aspire", [("ASPIRE_CLIENT_ID", "api_client_id"),
                                 ("ASPIRE_SECRET", "api_secret")])
    mc_cfg = _cfg("mailchimp", [("MAILCHIMP_API_KEY", "api_key")])
    mc_cfg["MAILCHIMP_SERVER"] = (os.environ.get("MAILCHIMP_SERVER")
                                  or mc_cfg["MAILCHIMP_API_KEY"].split("-")[-1]).strip()
    list_id = (os.environ.get("MAILCHIMP_LIST_ID") or "4a35469b32").strip()
    log(f"mode: {'LIVE' if LIVE else 'DRY RUN'}  list: {list_id}")

    service = build_service_map(aspire_token(aspire_cfg))
    log(f"contacts with won work in a tagged division: {len(service)}")

    members = mc_members(mc_cfg, list_id)
    log(f"Mailchimp subscribed members: {len(members)}")

    work = {}
    not_in_mc = 0
    for email, tags in service.items():
        if email not in members:
            not_in_mc += 1
            continue
        missing = tags - members[email]
        if missing:
            work[email] = missing

    totals = collections.Counter(t for tags in work.values() for t in tags)
    log(f"\nnot in Mailchimp (skipped): {not_in_mc}")
    log(f"contacts needing tags: {len(work)}")
    for tag, n in totals.most_common():
        log(f"  {tag:24s} +{n}")

    if not LIVE:
        for email, tags in list(work.items())[:10]:
            log(f"  DRY RUN would tag {email} {sorted(tags)}")
        log("\nDry run. Re-run with --live to write.")
        return 0

    done, errors = 0, []
    for email, tags in work.items():
        try:
            mc_add_tags(mc_cfg, list_id, email, tags)
            done += 1
            if done % 50 == 0:
                log(f"  ...{done}/{len(work)}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200] if e.fp else ""
            errors.append({"email": email, "code": e.code, "body": body})
            log(f"  ERROR {email}: {e.code} {body}")
        except Exception as e:  # noqa: BLE001
            errors.append({"email": email, "error": str(e)})
            log(f"  ERROR {email}: {e}")
        time.sleep(0.1)

    # Verify against the audience rather than trusting the write loop.
    after = mc_members(mc_cfg, list_id)
    unverified = [e for e, tags in work.items() if tags - after.get(e, set())]
    log(f"\ntagged: {done}  errors: {len(errors)}")
    log(f"verified: {len(work) - len(unverified)}/{len(work)}")
    if unverified:
        log(f"UNVERIFIED ({len(unverified)}): {unverified[:10]}")
    final = collections.Counter(t for tags in after.values() for t in tags
                                if t.startswith("svc-"))
    log("\nfinal svc-* tag counts:")
    for tag, n in final.most_common():
        log(f"  {tag:24s} {n}")
    return 1 if unverified else 0


if __name__ == "__main__":
    sys.exit(main())
