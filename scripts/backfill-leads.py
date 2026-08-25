#!/usr/bin/env python3
"""One-time backfill of the `leads` table from WhatConverts history.

WHY MONTH BY MONTH

The WhatConverts API silently caps a query at the 750 newest leads. It does
not error, and it still reports a plausible total_pages, so a paging loop over
a wide date range looks like it finished and quietly returns a fraction of the
data. That exact trap produced 120 phone numbers on 2026-08-12 where a
month-by-month pull found 425. Never widen these windows.

WHAT IT WRITES

One `leads` row per WhatConverts lead, keyed (whatconverts, lead_id). Upserts,
so re-running is safe and will not duplicate. aspire_contact_id is filled from
the existing lead_mappings table where a match exists.

Nothing here UPDATEs a lead's capture-time facts on a second run: the whole
point of the table is that a row records what was true on arrival.

USAGE
    python3 scripts/backfill-leads.py --dry-run     # count and show, write nothing
    python3 scripts/backfill-leads.py --months 12   # how far back to walk
    python3 scripts/backfill-leads.py               # default 12 months, writes
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

DRY_RUN = "--dry-run" in sys.argv
MONTHS = 12
if "--months" in sys.argv:
    MONTHS = int(sys.argv[sys.argv.index("--months") + 1])

CONFIG_PATH = os.path.expanduser("~/.config/whatconverts/config.json")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def wc_config():
    if os.environ.get("WC_API_TOKEN"):
        return os.environ["WC_API_TOKEN"], os.environ["WC_API_SECRET"]
    cfg = json.load(open(CONFIG_PATH))
    return cfg["api_token"], cfg["api_secret"]


def wc_get(token, secret, endpoint, params):
    url = f"https://app.whatconverts.com/api/v1{endpoint}?" + urllib.parse.urlencode(params)
    cred = base64.b64encode(f"{token}:{secret}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {cred}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def month_windows(months):
    """[(start_date, end_date)] one per calendar month, oldest first."""
    today = date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        start = date(y, m, 1)
        end = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))
        out.append((start, min(end, today)))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def fetch_month(token, secret, start, end):
    """Every lead in one month, paged. Returns a list."""
    leads, page = [], 1
    while True:
        data = wc_get(token, secret, "/leads", {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "leads_per_page": 250,
            "page_number": page,
        })
        batch = data.get("leads", []) if isinstance(data, dict) else []
        leads.extend(batch)
        total_pages = (data or {}).get("total_pages", 1)
        if page >= total_pages or not batch:
            break
        page += 1
    # If a single month ever approaches the cap, the window is too wide and
    # the data is silently incomplete. Say so rather than return a nice number.
    if len(leads) >= 740:
        log(f"  WARNING {start:%Y-%m}: {len(leads)} leads, at or near the 750 "
            f"cap -- this month is probably truncated. Split it by week.")
    return leads


def _source_medium(lead):
    src = (lead.get("lead_source") or "").strip() or "(direct)"
    med = (lead.get("lead_medium") or "").strip() or "(none)"
    return f"{src} / {med}"


def to_row(lead, aspire_by_wcid):
    wcid = str(lead.get("lead_id") or "")
    if not wcid:
        return None
    fields = lead.get("additional_fields") or {}
    name = (fields.get("Name") or lead.get("contact_name")
            or lead.get("caller_name") or "").strip() or None
    email = (lead.get("email_address") or fields.get("Email") or "").strip().lower() or None
    phone = (lead.get("phone_number") or lead.get("caller_number") or "").strip() or None
    captured = lead.get("date_created") or lead.get("date") or ""
    return {
        "source_system": "whatconverts",
        "source_id": wcid,
        "captured_at": captured,
        "name": name,
        "email": email,
        "phone": phone,
        "lead_type": lead.get("lead_type"),
        # "source / medium", matching the shape lead_mappings already uses
        # ("google / cpc"). lead_source alone loses the medium, which is the
        # difference between paid and organic Google -- the single most useful
        # distinction in the whole table.
        "traffic_source": _source_medium(lead),
        "campaign": lead.get("lead_campaign"),
        "landing_page": lead.get("landing_url") or lead.get("lead_page"),
        "aspire_contact_id": aspire_by_wcid.get(wcid),
        # raw keeps the whole payload so a question nobody has asked yet does
        # not require re-pulling an API that caps at 750 and loses history.
        "raw": lead,
    }


def main():
    token, secret = wc_config()
    log(f"Backfilling {MONTHS} months, one query per month.")

    mappings = db.load_lead_mappings() or {}
    aspire_by_wcid = {
        k: (v.get("aspire_contact_id") if isinstance(v, dict) else None)
        for k, v in mappings.items()
    }
    log(f"lead_mappings gives {sum(1 for v in aspire_by_wcid.values() if v)} "
        f"known Aspire links.")

    rows, seen = [], set()
    for start, end in month_windows(MONTHS):
        try:
            batch = fetch_month(token, secret, start, end)
        except urllib.error.HTTPError as e:
            log(f"  {start:%Y-%m}: HTTP {e.code}, skipped")
            continue
        fresh = 0
        for lead in batch:
            row = to_row(lead, aspire_by_wcid)
            if not row or not row["captured_at"]:
                continue
            if row["source_id"] in seen:
                continue
            seen.add(row["source_id"])
            rows.append(row)
            fresh += 1
        log(f"  {start:%Y-%m}: {len(batch):>4} returned, {fresh:>4} new")

    log(f"\n{len(rows)} distinct leads ready.")
    linked = sum(1 for r in rows if r["aspire_contact_id"])
    with_email = sum(1 for r in rows if r["email"])
    log(f"  {linked} linked to an Aspire contact, {with_email} with an email.")

    if DRY_RUN:
        log("DRY RUN - nothing written.")
        for r in rows[:3]:
            safe = {k: v for k, v in r.items() if k not in ("raw", "name", "email", "phone")}
            log(f"  sample: {json.dumps(safe)}")
        return 0

    written = db.upsert_leads(rows)
    log(f"Wrote {written} rows to leads.")
    return 0


if __name__ == "__main__":
    with db.track("backfill-leads"):
        sys.exit(main())
