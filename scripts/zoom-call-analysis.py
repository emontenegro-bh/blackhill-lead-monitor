#!/usr/bin/env python3
"""One-shot Zoom Phone call-log analysis.

Pulls all inbound call logs for the Black Hill account from Zoom Phone
(ground truth - includes voicemails that WhatConverts hides in its
'Answered' bucket) and prints a markdown report for the Twilio vs Zoom
Virtual Agent decision.

Auth: ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET env vars.
S2S OAuth app needs `phone:read:admin` or `phone:read:list_call_logs:admin`.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")


def get_access_token(account_id, client_id, client_secret):
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    url = f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={account_id}"
    req = urllib.request.Request(url, data=b"", headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return data["access_token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"Zoom OAuth {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


def fetch_call_history(token, from_date, to_date):
    """Paginate /phone/call_history for the given date range."""
    results = []
    next_page_token = ""
    page = 0
    while True:
        page += 1
        params = {
            "from": from_date,
            "to": to_date,
            "page_size": 300,
        }
        if next_page_token:
            params["next_page_token"] = next_page_token
        url = "https://api.zoom.us/v2/phone/call_history?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            print(f"Zoom call_history {e.code} (page {page}): {body}", file=sys.stderr)
            sys.exit(1)
        batch = data.get("call_logs") or data.get("call_history") or []
        results.extend(batch)
        next_page_token = data.get("next_page_token", "")
        print(f"  {from_date}->{to_date} page {page}: +{len(batch)} (running {len(results)})", file=sys.stderr)
        if not next_page_token:
            break
    return results


def parse_dt(raw):
    if not raw:
        return None
    s = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CENTRAL)


def is_business_hours(dt_local):
    if dt_local.weekday() >= 5:
        return False
    return 8 <= dt_local.hour < 18


def bucket_result(raw):
    """Map Zoom's many result strings into Answered / Voicemail / Missed / Other."""
    s = (raw or "").strip().lower()
    if "voicemail" in s:
        return "voicemail"
    if "answered" in s or s in ("call connected", "connected"):
        return "answered"
    if "missed" in s or "no answer" in s or "ringing" in s or "cancelled" in s or "canceled" in s or "rejected" in s:
        return "missed"
    return "other"


def main():
    account_id = os.environ["ZOOM_ACCOUNT_ID"].strip()
    client_id = os.environ["ZOOM_CLIENT_ID"].strip()
    client_secret = os.environ["ZOOM_CLIENT_SECRET"].strip()

    print("Authenticating with Zoom...", file=sys.stderr)
    token = get_access_token(account_id, client_id, client_secret)

    # Pull from Feb 1, 2026 (matches WC tracking start) through today, in 30-day chunks.
    # Zoom's call_history endpoint caps `from`/`to` range; 30 days is safe.
    today = datetime.now(timezone.utc).date()
    start = datetime(2026, 2, 1, tzinfo=timezone.utc).date()
    print(f"Fetching call history {start} -> {today}...", file=sys.stderr)

    all_calls = []
    win_start = start
    while win_start <= today:
        win_end = min(win_start + timedelta(days=29), today)
        all_calls.extend(fetch_call_history(
            token,
            win_start.strftime("%Y-%m-%d"),
            win_end.strftime("%Y-%m-%d"),
        ))
        win_start = win_end + timedelta(days=1)

    print(f"Fetched {len(all_calls)} total call records.", file=sys.stderr)
    if not all_calls:
        print("No call records.", file=sys.stderr)
        sys.exit(1)

    # Diagnostic: full field structure of a sample call
    sample = all_calls[0]
    print(f"DIAGNOSTIC: sample call fields: {sorted(sample.keys())}", file=sys.stderr)
    result_field_counter = Counter()
    direction_counter = Counter()
    for c in all_calls:
        result_field_counter[c.get("call_result") or c.get("result") or "(none)"] += 1
        direction_counter[c.get("direction") or "(none)"] += 1
    print(f"DIAGNOSTIC: result distribution: {dict(result_field_counter)}", file=sys.stderr)
    print(f"DIAGNOSTIC: direction distribution: {dict(direction_counter)}", file=sys.stderr)

    # Filter to inbound only
    inbound = [c for c in all_calls if (c.get("direction") or "").lower() == "inbound"]
    print(f"Inbound calls: {len(inbound)}", file=sys.stderr)

    # Bucket and analyze
    rows = []
    for c in inbound:
        dt_local = parse_dt(c.get("date_time") or c.get("start_time"))
        if dt_local is None:
            continue
        result_raw = c.get("call_result") or c.get("result") or ""
        rows.append({
            "dt_local": dt_local,
            "duration_s": int(c.get("duration") or 0),
            "result_raw": result_raw,
            "bucket": bucket_result(result_raw),
            "caller": c.get("caller_number") or c.get("caller_did_number") or "",
            "callee": c.get("callee_number") or c.get("callee_did_number") or "",
        })

    if not rows:
        print("No inbound rows.", file=sys.stderr)
        sys.exit(1)

    rows.sort(key=lambda r: r["dt_local"])
    earliest = rows[0]["dt_local"]
    latest = rows[-1]["dt_local"]

    by_bucket = Counter(r["bucket"] for r in rows)
    raw_results = Counter(r["result_raw"] or "(blank)" for r in rows)

    voicemails = [r for r in rows if r["bucket"] == "voicemail"]
    answered = [r for r in rows if r["bucket"] == "answered"]
    missed = [r for r in rows if r["bucket"] == "missed"]

    # All "uncovered" calls = voicemails + missed (those are the responder opportunity)
    uncovered = voicemails + missed

    biz_calls = [r for r in rows if is_business_hours(r["dt_local"])]
    after_calls = [r for r in rows if not is_business_hours(r["dt_local"])]
    uncovered_biz = [r for r in uncovered if is_business_hours(r["dt_local"])]
    uncovered_after = [r for r in uncovered if not is_business_hours(r["dt_local"])]

    answered_durations = [r["duration_s"] for r in answered if r["duration_s"] > 0]
    avg_dur = sum(answered_durations) / len(answered_durations) if answered_durations else 0
    sorted_dur = sorted(answered_durations)
    median_dur = sorted_dur[len(sorted_dur) // 2] if sorted_dur else 0

    by_month = defaultdict(lambda: Counter())
    for r in rows:
        key = r["dt_local"].strftime("%Y-%m")
        by_month[key]["total"] += 1
        by_month[key][r["bucket"]] += 1
        if not is_business_hours(r["dt_local"]):
            by_month[key]["after_hours"] += 1

    months_sorted = sorted(by_month.keys())
    span_months = max(1, len(months_sorted))
    avg_per_month = len(rows) / span_months
    voicemails_per_month = len(voicemails) / span_months
    missed_per_month = len(missed) / span_months
    uncovered_per_month = len(uncovered) / span_months

    va_proxy_min = max(2.0, avg_dur / 60.0) if avg_dur else 2.0
    va_uncovered_cost = uncovered_per_month * va_proxy_min * 0.30
    va_all_cost = avg_per_month * va_proxy_min * 0.30
    twilio_uncovered_cost = uncovered_per_month * 0.10

    print()
    print("# Zoom Phone Inbound Call Analysis")
    print()
    print(f"**Window:** {earliest.strftime('%Y-%m-%d')} to {latest.strftime('%Y-%m-%d')} "
          f"({span_months} months of data)")
    print(f"**Total inbound calls:** {len(rows)}")
    print()
    print("## Result Breakdown")
    print()
    print(f"- **Answered:** {len(answered)} ({100*len(answered)/len(rows):.1f}%)")
    print(f"- **Voicemail:** {len(voicemails)} ({100*len(voicemails)/len(rows):.1f}%)")
    print(f"- **Missed (no voicemail):** {len(missed)} ({100*len(missed)/len(rows):.1f}%)")
    print(f"- Other / unknown: {by_bucket.get('other', 0)}")
    print()
    print("### Raw Zoom `result` distribution")
    print()
    for status, count in raw_results.most_common():
        print(f"- `{status}`: {count}")
    print()
    print("## Volume")
    print()
    print(f"- Calls per month: **{avg_per_month:.1f}**")
    print(f"- Voicemails per month: **{voicemails_per_month:.1f}**")
    print(f"- Missed (no VM) per month: **{missed_per_month:.1f}**")
    print(f"- **Total uncovered per month (VM + missed): {uncovered_per_month:.1f}**")
    print()
    print("## Business Hours vs After Hours")
    print("(Business hours = Mon-Fri 8a-6p Central)")
    print()
    print(f"- Business-hours inbound: **{len(biz_calls)}** ({100*len(biz_calls)/len(rows):.1f}%)")
    print(f"- After-hours inbound:    **{len(after_calls)}** ({100*len(after_calls)/len(rows):.1f}%)")
    print(f"- Uncovered during business hours: **{len(uncovered_biz)}**")
    print(f"- Uncovered after hours:           **{len(uncovered_after)}**")
    print()
    print("## Answered Call Duration")
    print()
    if answered_durations:
        print(f"- Avg: **{avg_dur:.0f}s** ({avg_dur/60:.1f} min)")
        print(f"- Median: **{median_dur}s** ({median_dur/60:.1f} min)")
        print(f"- Min / Max: {sorted_dur[0]}s / {sorted_dur[-1]}s")
    print()
    print("## Monthly Trend")
    print()
    print("| Month | Total | Answered | Voicemail | Missed | After-hours |")
    print("|-------|-------|----------|-----------|--------|-------------|")
    for m in months_sorted:
        d = by_month[m]
        print(f"| {m} | {d.get('total', 0)} | {d.get('answered', 0)} | "
              f"{d.get('voicemail', 0)} | {d.get('missed', 0)} | {d.get('after_hours', 0)} |")
    print()
    print("## Cost Projections")
    print()
    print(f"VA proxy: {va_proxy_min:.1f} min/call (max of 2 min or avg answered duration)")
    print()
    print(f"- **Zoom VA, voicemails + missed only:** ~${va_uncovered_cost:.2f}/mo "
          f"({uncovered_per_month:.1f} calls x {va_proxy_min:.1f} min x $0.30)")
    print(f"- **Zoom VA, every inbound call:** ~${va_all_cost:.2f}/mo "
          f"({avg_per_month:.1f} calls x {va_proxy_min:.1f} min x $0.30)")
    print(f"- **Twilio SMS, voicemails + missed only:** ~${twilio_uncovered_cost:.2f}/mo "
          f"({uncovered_per_month:.1f} calls x ~$0.10 per SMS conversation)")
    print()
    print("_(Plus Zoom VA base license fee - still unknown - and Twilio phone number rental ~$1/mo)_")
    print()


if __name__ == "__main__":
    main()
