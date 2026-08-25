#!/usr/bin/env python3
"""Watch Aspire's Lead Source field and record every change.

THE PROBLEM

Aspire stores Lead Source (contact custom field 34) as a single mutable value.
Correct it in August and June's value is gone -- so "what did we believe about
this lead at the time" becomes unanswerable, and any historical attribution
report silently changes meaning depending on when it is run. That is not a
hypothetical: the 2026-08-12 attribution work backfilled 105 contacts, and
whatever those fields said beforehand no longer exists anywhere.

WHAT THIS DOES

Reads Lead Source for every contact linked to a row in `leads`, and:

  first time seen   writes it to leads.aspire_lead_source and stamps
                    aspire_lead_source_first_seen. This is the baseline.
  changed since     appends a row to lead_source_history. leads.aspire_lead_source
                    is NOT updated -- it means "as first observed", on purpose.
                    Current value = baseline, plus the newest history row.
  unchanged         writes nothing at all

Writing only on change is what keeps this honest and small. Recording every
observation would add ~160 identical rows a day and bury the handful that are
real events.

RUN IT BEFORE EDITING LEAD SOURCES IN BULK. A baseline captured after the edit
records the new value as if it had always been there.

USAGE
    python3 scripts/lead-source-observer.py --dry-run
    python3 scripts/lead-source-observer.py
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

DRY_RUN = "--dry-run" in sys.argv

CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
LEAD_SOURCE_DEFINITION_ID = 34   # "Lead Source" picklist, looked up 2026-05-13


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    client_id = os.environ.get("ASPIRE_CLIENT_ID")
    secret = os.environ.get("ASPIRE_SECRET")
    if client_id and secret:
        return {
            "api_base_url": os.environ.get("ASPIRE_API_URL",
                                           "https://cloud-api.youraspire.com"),
            "api_client_id": client_id,
            "api_secret": secret,
        }
    if os.path.exists(CONFIG_FILE):
        return json.load(open(CONFIG_FILE))
    raise SystemExit("No Aspire credentials in env or ~/.config/aspire/config.json")


def get_token(config):
    body = json.dumps({
        "ClientId": config["api_client_id"],
        "Secret": config["api_secret"],
    }).encode()
    req = urllib.request.Request(
        f"{config['api_base_url']}/Authorization",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["Token"]


def api_get(config, token, endpoint):
    path, _, query = endpoint.partition("?")
    url = f"{config['api_base_url']}{path}"
    if query:
        # NO space in the safe set. OData filters contain literal spaces
        # ("ContactID in (...) and ..."), and leaving them unencoded makes
        # http.client reject the URL outright as containing control
        # characters. Matches the safe set aspire-api-sync.py already uses.
        url += "?" + urllib.parse.quote(query, safe="=&$,'()@")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def fetch_lead_sources(config, token, contact_ids):
    """{contact_id(str): value} for the Lead Source field, one page per 50 ids.

    Batched by id rather than pulled wholesale: the field definition is shared
    across every contact type, so an unfiltered query returns far more rows
    than there are leads and most of them are irrelevant.
    """
    out = {}
    ids = [str(c) for c in contact_ids if c]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        id_list = ",".join(chunk)
        endpoint = (
            f"/ContactCustomFields?$filter=ContactID in ({id_list}) and "
            f"ContactCustomFieldDefinitionID eq {LEAD_SOURCE_DEFINITION_ID}"
            f"&$top=200"
        )
        try:
            rows = api_get(config, token, endpoint)
        except urllib.error.HTTPError as e:
            log(f"  batch {i // 50}: HTTP {e.code}, skipped")
            continue
        for r in rows if isinstance(rows, list) else []:
            cid = str(r.get("ContactID"))
            # ColumnValue is the field. NOT "Value" or "TextValue" -- those
            # do not exist on this endpoint, and reading them returns None for
            # every contact, which looks exactly like "nobody has a Lead
            # Source" instead of "you are reading the wrong key". That wrote
            # 152 false baselines before the result was checked against
            # reality rather than against the row count.
            val = (r.get("ColumnValue") or "").strip() or None
            if cid:
                out[cid] = val
    return out


def main():
    config = load_config()
    token = get_token(config)

    leads = db.leads_with_aspire_contact()
    log(f"{len(leads)} leads are linked to an Aspire contact.")
    if not leads:
        log("Nothing to observe.")
        return 0

    contact_ids = {l["aspire_contact_id"] for l in leads}
    observed = fetch_lead_sources(config, token, contact_ids)
    log(f"Read Lead Source for {len(observed)} of {len(contact_ids)} contacts.")

    baselines, changes, unchanged, missing = [], [], 0, 0
    for lead in leads:
        cid = lead["aspire_contact_id"]
        if cid not in observed:
            missing += 1
            continue
        now_val = observed[cid]
        was = lead.get("aspire_lead_source")
        if lead.get("aspire_lead_source_first_seen") is None:
            baselines.append((lead["id"], now_val))
        elif (was or None) != (now_val or None):
            changes.append((lead["id"], was, now_val, cid))
        else:
            unchanged += 1

    log(f"  baseline to record : {len(baselines)}")
    log(f"  changed            : {len(changes)}")
    log(f"  unchanged          : {unchanged}")
    log(f"  no field on contact: {missing}")

    for lead_id, was, now_val, cid in changes[:10]:
        log(f"    contact {cid}: {was!r} -> {now_val!r}")

    if DRY_RUN:
        log("DRY RUN - nothing written.")
        return 0

    if baselines:
        db.set_lead_source_baseline(baselines)
        log(f"Recorded {len(baselines)} baselines.")
    for lead_id, was, now_val, cid in changes:
        db.record_lead_source_change(lead_id, was, now_val,
                                     note=f"observed on contact {cid}")
    if changes:
        log(f"Appended {len(changes)} change rows to lead_source_history.")
    return 0


if __name__ == "__main__":
    with db.track("lead-source-observer"):
        sys.exit(main())
