#!/usr/bin/env python3
"""Mirror Aspire opportunities into Supabase, and record what changed.

WHAT THIS BUYS

Aspire holds only current state. Ask it "what was our win rate in June" and you
get today's answer applied to June: an opportunity revised in August looks as
though it was always that amount, and one open in June but won since counts as
a June win. Every historical revenue report therefore changes meaning depending
on the day it runs.

`opportunities` mirrors current state; `opportunity_snapshots` records each
move. Together they answer both "what is true now" and "what did we believe
then", which no single Aspire query can.

TWO TRAPS IN THIS DATA, BOTH ALREADY COSTLY

WonDollars is populated on Lost and Delivered rows too -- it mirrors
EstimatedDollars regardless of outcome. It is NOT revenue on its own. Always
filter on status first. Reading it unfiltered produced a wrong revenue figure
once already.

OpportunityType 'Contract' carries ANNUAL value; one-time work does not.
Summing them together is how a single $30,759 annual maintenance agreement
outweighs twelve real installs and flatters whichever channel happened to win
it. Every channel revenue number produced before this table existed has that
flaw baked in.

API LIMITS, learned the hard way

Aspire caps $top at 1000 and returns 400 above it, so this pages with $skip.
PropertyContacts cannot appear in $select (400) but arrives anyway in the full
payload. OData URLs must not contain literal spaces.

USAGE
    python3 scripts/sync-opportunities.py --dry-run
    python3 scripts/sync-opportunities.py
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
PAGE = 1000          # Aspire's hard ceiling; 1001 returns 400.


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    cid = os.environ.get("ASPIRE_REPORTING_CLIENT_ID") or os.environ.get("ASPIRE_CLIENT_ID")
    sec = os.environ.get("ASPIRE_REPORTING_SECRET") or os.environ.get("ASPIRE_SECRET")
    if cid and sec:
        return {"api_base_url": os.environ.get("ASPIRE_API_URL",
                                               "https://cloud-api.youraspire.com"),
                "api_client_id": cid, "api_secret": sec}
    cfg = json.load(open(CONFIG_FILE))
    return {
        "api_base_url": cfg.get("api_base_url", "https://cloud-api.youraspire.com"),
        "api_client_id": cfg.get("reporting_client_id", cfg.get("client_id",
                                 cfg.get("api_client_id"))),
        "api_secret": cfg.get("reporting_secret", cfg.get("secret",
                              cfg.get("api_secret"))),
    }


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
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode())


def fetch_all(config, token):
    out, skip = [], 0
    while True:
        rows = api_get(config, token, f"/Opportunities?$top={PAGE}&$skip={skip}")
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        log(f"  fetched {len(out)}")
        if len(rows) < PAGE:
            break
        skip += PAGE
    return out


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def to_row(o):
    return {
        "opportunity_id":     o.get("OpportunityID"),
        "opportunity_number": o.get("OpportunityNumber"),
        "name":               o.get("OpportunityName"),
        "property_id":        o.get("PropertyID"),
        "property_name":      o.get("PropertyName"),
        "billing_contact_id": (str(o["BillingContactID"])
                               if o.get("BillingContactID") else None),
        "status":             o.get("OpportunityStatusName"),
        "stage":              o.get("OpportunityStageName"),
        "opportunity_type":   o.get("OpportunityType"),
        "sales_type":         o.get("SalesTypeName"),
        "division":           o.get("DivisionName"),
        "estimated_dollars":  _num(o.get("EstimatedDollars")),
        "won_dollars":        _num(o.get("WonDollars")),
        "actual_revenue":     _num(o.get("ActualEarnedRevenue")),
        "estimated_margin":   _num(o.get("EstimatedGrossMarginDollars")),
        "actual_margin":      _num(o.get("ActualGrossMarginDollars")),
        "created_at_aspire":  o.get("CreatedDateTime"),
        "proposed_date":      o.get("ProposedDate"),
        "won_date":           o.get("WonDate"),
        "lost_date":          o.get("LostDate"),
        "complete_date":      o.get("CompleteDate"),
        "start_date":         o.get("StartDate"),
        "end_date":           o.get("EndDate"),
        "lost_reason":        o.get("LostReason"),
        "aspire_lead_source": o.get("LeadSourceName"),
        "sales_rep":          o.get("SalesRepContactName"),
        "raw":                o,
        "synced_at":          datetime.now(timezone.utc).isoformat(),
    }


def main():
    config = load_config()
    token = get_token(config)

    log("Fetching opportunities from Aspire...")
    raw = fetch_all(config, token)
    rows = [to_row(o) for o in raw if o.get("OpportunityID")]
    log(f"{len(rows)} opportunities.")

    from collections import Counter
    log("  by status: " + ", ".join(
        f"{k}={v}" for k, v in Counter(r["status"] for r in rows).most_common(6)))
    log("  by type:   " + ", ".join(
        f"{k}={v}" for k, v in Counter(r["opportunity_type"] for r in rows).most_common(4)))

    # Change detection against what we already hold.
    before = db.opportunities_current() if not DRY_RUN else {}
    if DRY_RUN:
        try:
            before = db.opportunities_current()
        except Exception as e:
            log(f"  (could not read existing rows: {str(e)[:80]})")

    changes, new = [], 0
    for r in rows:
        prev = before.get(r["opportunity_id"])
        if prev is None:
            new += 1
            continue
        moved_status = (prev["status"] or None) != (r["status"] or None)
        pd, nd = _num(prev["estimated_dollars"]), r["estimated_dollars"]
        moved_money = (pd or 0) != (nd or 0)
        if moved_status or moved_money:
            changes.append({
                "opportunity_id": r["opportunity_id"],
                "status": r["status"],
                "estimated_dollars": nd,
                "prev_status": prev["status"],
                "prev_dollars": pd,
                "note": ("status" if moved_status else "")
                        + ("+amount" if moved_status and moved_money
                           else ("amount" if moved_money else "")),
            })

    log(f"  new to us: {new}   changed since last sync: {len(changes)}")
    for c in changes[:8]:
        log(f"    #{c['opportunity_id']}: {c['prev_status']!r} {c['prev_dollars']} "
            f"-> {c['status']!r} {c['estimated_dollars']}")

    if DRY_RUN:
        log("DRY RUN - nothing written.")
        return 0

    written = db.upsert_opportunities(rows)
    log(f"Upserted {written} opportunities.")
    if changes:
        db.record_opportunity_changes(changes)
        log(f"Recorded {len(changes)} changes in opportunity_snapshots.")
    return 0


if __name__ == "__main__":
    with db.track("sync-opportunities"):
        sys.exit(main())
