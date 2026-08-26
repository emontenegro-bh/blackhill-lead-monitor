#!/usr/bin/env python3
"""Link phone-call leads to Aspire contacts that already exist. Creates nothing.

WHY THIS IS NOT A REVERSAL OF THE "NO PHONE CALL LEADS" RULE

The standing rule (2026-03-30, re-confirmed 2026-05-13) is that
whatconverts-lead-monitor must not PROCESS phone calls: no Aspire contact, no
HubSpot deal, no owner assignment, no notifications. The reason is workflow --
the branch admin owns inbound calls, and auto-assignment was routing his calls
to Evelin and Denisse.

That rule is about ACTING on a call. This script only OBSERVES one. It reads
Aspire, matches on phone number, and writes a single foreign key into our own
leads table. It creates nothing in Aspire, assigns nobody, and notifies no one.
Carlos still owns the call and still decides whether a contact gets made; this
just notices afterwards that he made one.

WHY IT MATTERS

551 of 824 leads are phone calls. Their linkage rate is 3%, and zero for four
of the last five months, so every channel comparison over that period is
web-only by construction. The Google Business Profile alone is 333 calls --
40% of all lead volume -- with 1% traceable, which makes the single biggest
source of leads look like the worst performing one.

GUARDS

Two, both learned from real data:

  Shared numbers. 817-995-0324 is Black Hill's own office line and sits on
  dozens of contacts. Matching on it would invent a conversion for every one.
  Any number resolving to more than MAX_CONTACTS_PER_NUMBER contacts is
  skipped entirely.

  Employees. Crew contacts carry the office line and each other's numbers.
  Contacts whose type is Employee are never match targets.

USAGE
    python3 scripts/link-phone-leads.py --dry-run
    python3 scripts/link-phone-leads.py
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

DRY_RUN = "--dry-run" in sys.argv
CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")

# A number on more than this many contacts is a shared line, not a customer.
MAX_CONTACTS_PER_NUMBER = 3

# Numbers that are ours and must never match, regardless of contact count.
OWN_NUMBERS = {"8179950324"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def norm_phone(raw):
    """Last 10 digits, or None. Handles +1, dashes, parens, extensions."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def load_config():
    cid, sec = os.environ.get("ASPIRE_CLIENT_ID"), os.environ.get("ASPIRE_SECRET")
    if cid and sec:
        return {"api_base_url": os.environ.get("ASPIRE_API_URL",
                                               "https://cloud-api.youraspire.com"),
                "api_client_id": cid, "api_secret": sec}
    if os.path.exists(CONFIG_FILE):
        return json.load(open(CONFIG_FILE))
    raise SystemExit("No Aspire credentials")


def get_token(config):
    body = json.dumps({"ClientId": config["api_client_id"],
                       "Secret": config["api_secret"]}).encode()
    req = urllib.request.Request(f"{config['api_base_url']}/Authorization",
                                 data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["Token"]


def api_get(config, token, endpoint):
    path, _, query = endpoint.partition("?")
    url = f"{config['api_base_url']}{path}"
    if query:
        # No space in the safe set; http.client rejects a URL containing one.
        url += "?" + urllib.parse.quote(query, safe="=&$,'()@")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def all_contacts(config, token):
    """Every active contact, paged. Aspire caps $top at 1000 and 400s above it."""
    out, skip = [], 0
    while True:
        rows = api_get(config, token,
                       "/Contacts?$filter=Active eq true"
                       "&$select=ContactID,FirstName,LastName,ContactTypeName,"
                       "MobilePhone,HomePhone,OfficePhone"
                       f"&$top=1000&$skip={skip}")
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        skip += 1000
    return out


def build_phone_index(contacts):
    """{normalised phone: [ContactID]} excluding employees and shared lines."""
    idx = defaultdict(list)
    employees = 0
    for c in contacts:
        if (c.get("ContactTypeName") or "").lower() == "employee":
            employees += 1
            continue
        for field in ("MobilePhone", "HomePhone", "OfficePhone"):
            p = norm_phone(c.get(field))
            if p and c["ContactID"] not in idx[p]:
                idx[p].append(c["ContactID"])
    log(f"  skipped {employees} Employee contacts as match targets")

    shared = {p: ids for p, ids in idx.items()
              if len(ids) > MAX_CONTACTS_PER_NUMBER or p in OWN_NUMBERS}
    for p in shared:
        del idx[p]
    if shared:
        log(f"  excluded {len(shared)} shared/own numbers "
            f"(largest sat on {max(len(v) for v in shared.values())} contacts)")
    return idx


def main():
    config = load_config()
    token = get_token(config)

    leads = db._request(
        "GET",
        "leads?lead_type=eq.Phone%20Call&aspire_contact_id=is.null"
        "&select=id,phone,captured_at,traffic_source&order=captured_at.asc&limit=2000")
    log(f"{len(leads)} phone leads have no Aspire link yet.")

    contacts = all_contacts(config, token)
    log(f"Loaded {len(contacts)} active Aspire contacts.")
    idx = build_phone_index(contacts)
    log(f"  {len(idx)} distinct usable phone numbers indexed")

    matches, no_phone, no_match = [], 0, 0
    by_source = defaultdict(int)
    for lead in leads:
        p = norm_phone(lead.get("phone"))
        if not p:
            no_phone += 1
            continue
        ids = idx.get(p)
        if not ids:
            no_match += 1
            continue
        matches.append((lead["id"], str(ids[0])))
        by_source[lead.get("traffic_source")] += 1

    log(f"\n  matched          : {len(matches)}")
    log(f"  no phone on lead : {no_phone}")
    log(f"  no contact found : {no_match}")
    if by_source:
        log("  matched by channel:")
        for s, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            log(f"    {n:>4}  {s}")

    if DRY_RUN:
        log("DRY RUN - nothing written. Aspire was only read.")
        return 0

    db.link_leads_to_contacts(matches)
    log(f"Linked {len(matches)} phone leads. Nothing was created in Aspire.")
    return 0


if __name__ == "__main__":
    with db.track("link-phone-leads"):
        sys.exit(main())
