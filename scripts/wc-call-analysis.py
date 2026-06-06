#!/usr/bin/env python3
"""One-shot WhatConverts phone-call analysis.

Pulls every phone call lead in the WhatConverts profile and prints a
markdown report sized for the Twilio vs Zoom Virtual Agent decision:

  - Total calls since WC started
  - Answered vs missed split
  - Business-hours vs after-hours mix (M-F 8a-6p Central)
  - Avg duration of answered calls (drives Zoom VA $0.30/min cost projection)
  - Monthly volume trend (last 24 months)

Auth: WC_API_TOKEN, WC_API_SECRET, WC_PROFILE_ID env vars (same as the
lead monitor).
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

# Black Hill own phone numbers (tracking + business line) - skip these.
# Mirrors OWN_PHONE_NUMBERS in whatconverts-lead-monitor.py.
OWN_PHONE_NUMBERS = {
    "+18179950324", "+18174056883", "+18174054340", "+18174054439",
    "+18172904711", "+18173456954", "+18173808161", "+18173829016",
}


def wc_request(token, secret, endpoint, params):
    url = f"https://app.whatconverts.com/api/v1{endpoint}?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{token}:{secret}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:600]
        print(f"WC API {e.code} on {endpoint} params={params}: {body}", file=sys.stderr)
        raise


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return raw or ""


def parse_created(raw):
    if not raw:
        return None
    s = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CENTRAL)


def is_business_hours(dt_local):
    if dt_local.weekday() >= 5:
        return False
    hour = dt_local.hour
    return 8 <= hour < 18


def fetch_all_calls(token, secret, profile_id, start_date):
    # Diagnostic: get one page with no lead_type filter so we can see which
    # lead types WhatConverts actually has data for.
    diag = wc_request(token, secret, "/leads", {
        "profile_id": profile_id,
        "start_date": start_date,
        "leads_per_page": 50,
        "page_number": 1,
    })
    type_counter = Counter()
    for lead in diag.get("leads", []):
        type_counter[lead.get("lead_type") or "(none)"] += 1
    print(f"DIAGNOSTIC: total_leads={diag.get('total_leads')}, "
          f"total_pages={diag.get('total_pages')}, "
          f"lead_type sample (first page): {dict(type_counter)}",
          file=sys.stderr)

    page = 1
    out = []
    while True:
        params = {
            "profile_id": profile_id,
            "start_date": start_date,
            "lead_type": "phone_call",
            "leads_per_page": 250,
            "page_number": page,
        }
        try:
            data = wc_request(token, secret, "/leads", params)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:400]
            print(f"ERROR page {page}: HTTP {e.code} {body}", file=sys.stderr)
            sys.exit(1)
        leads = data.get("leads", [])
        out.extend(leads)
        total_pages = data.get("total_pages", 1)
        print(f"  page {page}/{total_pages}: +{len(leads)} (running {len(out)})", file=sys.stderr)
        if page >= total_pages or not leads:
            break
        page += 1
    return out


def classify_answer_status(status):
    s = (status or "").strip().lower()
    if s in ("answered", "completed"):
        return "answered"
    if s in ("", "unknown"):
        return "unknown"
    return "missed"


def main():
    token = os.environ["WC_API_TOKEN"].strip()
    secret = os.environ["WC_API_SECRET"].strip()
    profile_id = os.environ.get("WC_PROFILE_ID", "162442").strip()

    # WhatConverts requires start_date. Try YYYY-MM-DD (date-only) which the
    # API docs list as the canonical format. Go back 5 years for full history.
    start_date = "2021-01-01"
    print(f"Fetching all phone-call leads for profile {profile_id} since {start_date}...", file=sys.stderr)
    calls = fetch_all_calls(token, secret, profile_id, start_date)
    print(f"Fetched {len(calls)} raw call records.", file=sys.stderr)

    # Filter own numbers and parse
    own_filtered = 0
    parse_failed = 0
    rows = []
    for c in calls:
        caller_raw = c.get("caller_number") or c.get("contact_phone_number") or ""
        if normalize_phone(caller_raw) in OWN_PHONE_NUMBERS:
            own_filtered += 1
            continue
        dt_local = parse_created(c.get("date_created"))
        if dt_local is None:
            parse_failed += 1
            continue
        rows.append({
            "dt_local": dt_local,
            "duration_s": int(c.get("call_duration_seconds") or 0),
            "answer_status_raw": c.get("answer_status") or "",
            "answer_bucket": classify_answer_status(c.get("answer_status")),
            "state": (c.get("caller_state") or c.get("state") or "").strip(),
            "lead_status": (c.get("lead_status") or "").strip(),
            "spam_flag": bool(c.get("spam")),
            "duplicate_flag": bool(c.get("duplicate")),
            "lead_source": (c.get("lead_source") or "").strip(),
            "phone_name": (c.get("phone_name") or "").strip(),
            "has_transcription": bool((c.get("call_transcription") or "").strip()),
        })

    if not rows:
        print("No call rows after filtering.", file=sys.stderr)
        sys.exit(1)

    rows.sort(key=lambda r: r["dt_local"])
    earliest = rows[0]["dt_local"]
    latest = rows[-1]["dt_local"]

    # Strip spam for the main analysis
    non_spam = [r for r in rows if not r["spam_flag"]]
    spam_count = len(rows) - len(non_spam)

    # Buckets
    answered = [r for r in non_spam if r["answer_bucket"] == "answered"]
    missed = [r for r in non_spam if r["answer_bucket"] == "missed"]
    unknown = [r for r in non_spam if r["answer_bucket"] == "unknown"]

    biz_calls = [r for r in non_spam if is_business_hours(r["dt_local"])]
    after_calls = [r for r in non_spam if not is_business_hours(r["dt_local"])]

    missed_biz = [r for r in missed if is_business_hours(r["dt_local"])]
    missed_after = [r for r in missed if not is_business_hours(r["dt_local"])]

    answered_durations = [r["duration_s"] for r in answered if r["duration_s"] > 0]
    avg_dur = sum(answered_durations) / len(answered_durations) if answered_durations else 0
    sorted_dur = sorted(answered_durations)
    median_dur = sorted_dur[len(sorted_dur) // 2] if sorted_dur else 0

    # Monthly trend
    by_month = defaultdict(lambda: {"total": 0, "answered": 0, "missed": 0, "after_hours": 0})
    for r in non_spam:
        key = r["dt_local"].strftime("%Y-%m")
        by_month[key]["total"] += 1
        if r["answer_bucket"] == "answered":
            by_month[key]["answered"] += 1
        elif r["answer_bucket"] == "missed":
            by_month[key]["missed"] += 1
        if not is_business_hours(r["dt_local"]):
            by_month[key]["after_hours"] += 1

    months_sorted = sorted(by_month.keys())
    span_months = max(1, len(months_sorted))
    avg_per_month = len(non_spam) / span_months
    missed_per_month = len(missed) / span_months

    # Answer status raw distribution (sanity check)
    raw_status_counts = Counter(r["answer_status_raw"] or "(blank)" for r in non_spam)

    # Cost projections
    # Zoom VA: $0.30/min. Bill on duration; assume VA conversation lasts ~ avg of answered+missed proxy.
    # Use 2 min as a conservative default if no duration data, else avg of answered calls as proxy.
    va_proxy_min = max(2.0, avg_dur / 60.0) if avg_dur else 2.0
    missed_monthly_va_cost_min = (missed_per_month) * va_proxy_min * 0.30
    all_monthly_va_cost_min = (avg_per_month) * va_proxy_min * 0.30
    # Twilio: SMS conversation ~ 6-12 segments per lead, $0.0083/seg outbound + $0.0079/seg inbound, ~$0.10/lead.
    twilio_monthly = missed_per_month * 0.10

    # Output: markdown report
    print()
    print("# WhatConverts Phone Call Analysis")
    print()
    print(f"**Window:** {earliest.strftime('%Y-%m-%d')} to {latest.strftime('%Y-%m-%d')} "
          f"({span_months} months of data)")
    print(f"**Total call records returned by API:** {len(calls)}")
    print(f"**Filtered out:** {own_filtered} own-number, {parse_failed} unparseable dates, {spam_count} flagged spam")
    print(f"**Analyzed (non-spam, non-own):** {len(non_spam)} calls")
    print()
    print("## Volume")
    print()
    print(f"- Average per month: **{avg_per_month:.1f}** calls")
    print(f"- Average missed per month: **{missed_per_month:.1f}** calls")
    print()
    print("## Answered vs Missed")
    print()
    print(f"- Answered: **{len(answered)}** ({100*len(answered)/len(non_spam):.1f}%)")
    print(f"- Missed:   **{len(missed)}** ({100*len(missed)/len(non_spam):.1f}%)")
    print(f"- Unknown / blank status: {len(unknown)} ({100*len(unknown)/len(non_spam):.1f}%)")
    print()
    print("### Raw answer_status distribution (sanity check)")
    print()
    for status, count in raw_status_counts.most_common():
        print(f"- `{status}`: {count}")
    print()
    print("## Business Hours vs After Hours")
    print("(Business hours = Mon-Fri 8a-6p Central)")
    print()
    print(f"- Business-hours calls: **{len(biz_calls)}** ({100*len(biz_calls)/len(non_spam):.1f}%)")
    print(f"- After-hours calls:    **{len(after_calls)}** ({100*len(after_calls)/len(non_spam):.1f}%)")
    print()
    print(f"- Missed during business hours: **{len(missed_biz)}**")
    print(f"- Missed after hours:           **{len(missed_after)}**")
    print()
    print("## Answered Call Duration (Zoom VA cost proxy)")
    print()
    if answered_durations:
        print(f"- Answered calls with duration data: {len(answered_durations)}")
        print(f"- Avg duration: **{avg_dur:.0f}s** ({avg_dur/60:.1f} min)")
        print(f"- Median duration: **{median_dur}s** ({median_dur/60:.1f} min)")
        print(f"- Min / Max: {sorted_dur[0]}s / {sorted_dur[-1]}s")
    else:
        print("- No answered-call duration data available")
    print()
    print("## Monthly Trend (last 24 months)")
    print()
    print("| Month | Total | Answered | Missed | After-hours |")
    print("|-------|-------|----------|--------|-------------|")
    for m in months_sorted[-24:]:
        d = by_month[m]
        print(f"| {m} | {d['total']} | {d['answered']} | {d['missed']} | {d['after_hours']} |")
    print()
    print("## Cost Projections")
    print()
    print(f"Using {va_proxy_min:.1f} min/call as VA conversation length proxy (max of 2 min or avg answered duration):")
    print()
    print(f"- **Zoom VA, missed calls only:** ~${missed_monthly_va_cost_min:.2f}/mo "
          f"({missed_per_month:.1f} calls x {va_proxy_min:.1f} min x $0.30)")
    print(f"- **Zoom VA, all incoming calls:** ~${all_monthly_va_cost_min:.2f}/mo "
          f"({avg_per_month:.1f} calls x {va_proxy_min:.1f} min x $0.30)")
    print(f"- **Twilio SMS, missed calls only:** ~${twilio_monthly:.2f}/mo "
          f"({missed_per_month:.1f} calls x ~$0.10 per SMS conversation)")
    print()
    print("_(Plus Zoom VA base license fee - still unknown - and Twilio phone number rental ~$1/mo)_")
    print()


if __name__ == "__main__":
    main()
