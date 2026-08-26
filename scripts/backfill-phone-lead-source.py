#!/usr/bin/env python3
"""Fill Aspire Lead Source for phone leads where WhatConverts knows the source.

THE JOIN THIS EXPLOITS

WhatConverts sees the call and knows where it came from, but never knows
whether it became a customer. Carlos's phone form creates the Aspire contact
and so knows it became a customer, but not where it came from. Neither system
can answer "did our Google Business Profile produce revenue". The phone number
is the join, and this writes the result of that join back into Aspire.

SCOPE, DELIBERATELY NARROW

  - only contacts already linked to a WhatConverts phone lead
  - only where Lead Source is currently blank or absent -- an existing value is
    never overwritten, because a human may have set it deliberately and they
    know things WhatConverts does not
  - only confident mappings. "(direct) / (none)" carries no information,
    "blackhilllandscaping.com / referral" is a session-stitching artifact
    rather than a real referral, and both are skipped rather than guessed.

READ-BACK VERIFICATION IS MANDATORY HERE

Aspire returns HTTP 200 for a value that is not on the picklist and silently
stores an empty string. 68 contacts currently have a blank Lead Source, 47 of
them created since June, which is what that failure mode looks like at scale:
every write "succeeded". So every write here is read back and compared, and a
mismatch is reported as a failure rather than counted as a success.

Note the trailing space in 'Phone Call ' if it is ever added to the map -- the
picklist value genuinely contains it.

USAGE
    python3 scripts/backfill-phone-lead-source.py --dry-run
    python3 scripts/backfill-phone-lead-source.py
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

DRY_RUN = "--dry-run" in sys.argv
CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
LEAD_SOURCE_DEFINITION_ID = 34

# Exact picklist strings, confirmed against the values already in use.
# Anything not here is skipped, not guessed.
SOURCE_MAP = {
    "google / cpc":     "Google Ads",
    "google / organic": "Google Organic",
    "gmb / organic":    "Google Business Profile",
    "bing / cpc":       "Bing Ads",
    "bing / organic":   "Bing Organic",
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    cid, sec = os.environ.get("ASPIRE_CLIENT_ID"), os.environ.get("ASPIRE_SECRET")
    if cid and sec:
        return {"api_base_url": os.environ.get("ASPIRE_API_URL",
                                               "https://cloud-api.youraspire.com"),
                "api_client_id": cid, "api_secret": sec}
    return json.load(open(CONFIG_FILE))


def get_token(config):
    body = json.dumps({"ClientId": config["api_client_id"],
                       "Secret": config["api_secret"]}).encode()
    req = urllib.request.Request(f"{config['api_base_url']}/Authorization",
                                 data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["Token"]


def api(config, token, method, endpoint, body=None):
    path, _, query = endpoint.partition("?")
    url = f"{config['api_base_url']}{path}"
    if query:
        url += "?" + urllib.parse.quote(query, safe="=&$,'()@")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode()
        return (json.loads(raw) if raw.strip() else None), resp.status


def current_row(config, token, contact_id):
    rows, _ = api(config, token, "GET",
                  f"/ContactCustomFields?$filter=ContactID eq {int(contact_id)} and "
                  f"ContactCustomFieldDefinitionID eq {LEAD_SOURCE_DEFINITION_ID}&$top=1")
    return (rows or [None])[0] if isinstance(rows, list) else None


def set_lead_source(config, token, contact_id, value):
    """Write, then READ BACK. Returns (ok, stored_value)."""
    existing = current_row(config, token, contact_id)
    body = {
        "ContactID": int(contact_id),
        "ContactCustomFieldDefinitionID": LEAD_SOURCE_DEFINITION_ID,
        "ColumnValue": value,
    }
    if existing and existing.get("ContactCustomFieldValueID"):
        body["ContactCustomFieldValueID"] = existing["ContactCustomFieldValueID"]
        api(config, token, "PUT", "/ContactCustomFields", body)
    else:
        api(config, token, "POST", "/ContactCustomFields", body)

    # The whole point. A 200 above proves nothing.
    after = current_row(config, token, contact_id)
    stored = ((after or {}).get("ColumnValue") or "").strip()
    return stored == value.strip(), stored


def main():
    config = load_config()
    token = get_token(config)

    leads = db._request(
        "GET",
        "leads?lead_type=eq.Phone%20Call&aspire_contact_id=not.is.null"
        "&select=aspire_contact_id,traffic_source,captured_at"
        "&order=captured_at.asc&limit=2000")

    planned, seen = [], set()
    for l in leads:
        cid, src = l["aspire_contact_id"], l["traffic_source"]
        if cid in seen or src not in SOURCE_MAP:
            continue
        row = current_row(config, token, cid)
        if row and (row.get("ColumnValue") or "").strip():
            continue                      # already set by a human; leave it
        seen.add(cid)
        planned.append((cid, SOURCE_MAP[src], src))

    log(f"{len(planned)} contacts would be set:")
    for cid, val, src in planned:
        log(f"  contact {cid}: blank -> {val!r}   (from {src})")

    if DRY_RUN:
        log("DRY RUN - Aspire was only read.")
        return 0

    ok = fail = 0
    for cid, val, src in planned:
        try:
            good, stored = set_lead_source(config, token, cid, val)
        except urllib.error.HTTPError as e:
            log(f"  contact {cid}: HTTP {e.code} - FAILED")
            fail += 1
            continue
        if good:
            log(f"  contact {cid}: set to {val!r}, verified")
            ok += 1
        else:
            log(f"  contact {cid}: WROTE {val!r} BUT ASPIRE STORED {stored!r} - "
                f"off-picklist coercion, treat as failed")
            fail += 1

    log(f"\n{ok} verified, {fail} failed.")
    if fail:
        log("A failure here means Aspire accepted the write and stored something "
            "else. Check the value against the live picklist before retrying.")
    return 1 if fail else 0


if __name__ == "__main__":
    with db.track("backfill-phone-lead-source"):
        sys.exit(main())
