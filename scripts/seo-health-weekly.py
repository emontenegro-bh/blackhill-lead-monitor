#!/usr/bin/env python3
"""Weekly Local SEO Health Check for Black Hill Landscaping.

Pulls data from:
  - Google Search Console (keyword positions, impressions, clicks, CTR)
  - GBP Performance API (listing views, calls, direction requests)

Compares this week vs. last week, flags meaningful changes,
and emails a scannable report every Monday morning.

Schedule: Monday 7:30 AM CST via GitHub Actions
Manual run: python3 scripts/seo-health-weekly.py
"""

import json, warnings, smtplib, os, sys, signal
import urllib.request, urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from datetime import datetime, timedelta, date

# --- Global timeout ---
SCRIPT_TIMEOUT = 300

def _timeout_handler(signum, frame):
    print(f"ERROR: Script timed out after {SCRIPT_TIMEOUT}s", file=sys.stderr)
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(SCRIPT_TIMEOUT)

warnings.filterwarnings("ignore")

# --- Config ---
TO_EMAIL = "evelin@blackhilltx.com"
GSC_SITE = "sc-domain:blackhilllandscaping.com"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
REPORT_DIR = os.path.join(REPO_ROOT, ".claude", "reports", "marketing", "seo", "weekly")
HISTORY_FILE = os.path.join(REPORT_DIR, "seo-health-history.json")

# Keywords we care most about (tracked individually for trend alerts)
PRIORITY_KEYWORDS = [
    "landscaping",
    "lawn care",
    "lawn mowing",
    "sod installation",
    "sprinkler repair",
    "irrigation",
    "tree trimming",
    "tree removal",
    "mulch",
    "landscape design",
    "commercial landscaping",
]

# DFW cities we serve
SERVICE_CITIES = [
    "fort worth", "arlington", "aledo", "white settlement",
    "watauga", "saginaw", "benbrook", "weatherford",
    "hudson oaks", "willow park", "annetta",
]


# ============================================================
# AUTH HELPERS
# ============================================================

def get_gsc_token():
    """Get fresh access token for Search Console API."""
    import requests
    config_file = os.path.expanduser("~/.config/gsc/config.json")

    if os.environ.get("GSC_CLIENT_ID"):
        config = {
            "client_id": os.environ["GSC_CLIENT_ID"],
            "client_secret": os.environ["GSC_CLIENT_SECRET"],
            "refresh_token": os.environ["GSC_REFRESH_TOKEN"],
            "site_url": os.environ.get("GSC_SITE_URL", GSC_SITE),
        }
    else:
        with open(config_file) as f:
            config = json.load(f)

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": config["refresh_token"],
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"], config.get("site_url", GSC_SITE)


def get_gbp_token():
    """Get fresh access token for GBP Performance API."""
    import requests
    config_file = os.path.expanduser("~/.config/gbp/config.json")

    if os.environ.get("GBP_CLIENT_ID"):
        config = {
            "client_id": os.environ["GBP_CLIENT_ID"],
            "client_secret": os.environ["GBP_CLIENT_SECRET"],
            "refresh_token": os.environ["GBP_REFRESH_TOKEN"],
            "location_id": os.environ["GBP_LOCATION_ID"],
        }
    else:
        with open(config_file) as f:
            config = json.load(f)

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": config["refresh_token"],
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"], config.get("location_id", "")


# ============================================================
# DATA COLLECTION: SEARCH CONSOLE
# ============================================================

def gsc_query(token, site_url, start_date, end_date, dimensions=None, row_limit=100, dim_filter=None):
    """Query Search Console Search Analytics API."""
    import requests
    url = f"https://www.googleapis.com/webmasters/v3/sites/{requests.utils.quote(site_url, safe='')}/searchAnalytics/query"
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions or ["query"],
        "rowLimit": row_limit,
    }
    if dim_filter:
        body["dimensionFilterGroups"] = [{"filters": dim_filter}]

    resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    if resp.status_code != 200:
        print(f"GSC query error ({resp.status_code}): {resp.text[:200]}", file=sys.stderr)
        return []
    return resp.json().get("rows", [])


def collect_gsc_data(token, site_url):
    """Collect all Search Console data for the report."""
    # GSC has ~3 day data lag
    end_this = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    start_this = (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d")
    end_prev = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    start_prev = (datetime.now() - timedelta(days=16)).strftime("%Y-%m-%d")

    data = {}

    # 1. Overall totals this week vs last
    for label, s, e in [("this", start_this, end_this), ("prev", start_prev, end_prev)]:
        rows = gsc_query(token, site_url, s, e, dimensions=["date"], row_limit=10)
        totals = {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}
        for r in rows:
            totals["clicks"] += r["clicks"]
            totals["impressions"] += int(r["impressions"])
        if rows:
            totals["ctr"] = totals["clicks"] / totals["impressions"] * 100 if totals["impressions"] > 0 else 0
            # Average position is weighted by impressions
            total_impr = sum(r["impressions"] for r in rows)
            if total_impr > 0:
                totals["position"] = sum(r["position"] * r["impressions"] for r in rows) / total_impr
        data[f"totals_{label}"] = totals

    # 2. Top queries this week (full list)
    data["top_queries"] = gsc_query(token, site_url, start_this, end_this, dimensions=["query"], row_limit=50)

    # 3. Top queries last week (for comparison)
    prev_queries = gsc_query(token, site_url, start_prev, end_prev, dimensions=["query"], row_limit=50)
    data["prev_queries"] = {r["keys"][0]: r for r in prev_queries}

    # 4. Top pages
    data["top_pages"] = gsc_query(token, site_url, start_this, end_this, dimensions=["page"], row_limit=20)

    # 5. Priority keyword tracking (match against our target keywords + cities)
    data["priority_matches"] = []
    for row in data["top_queries"]:
        query = row["keys"][0].lower()
        is_priority = any(kw in query for kw in PRIORITY_KEYWORDS)
        has_city = any(city in query for city in SERVICE_CITIES)
        if is_priority or has_city:
            prev = data["prev_queries"].get(row["keys"][0], {})
            data["priority_matches"].append({
                "query": row["keys"][0],
                "clicks": row["clicks"],
                "impressions": int(row["impressions"]),
                "ctr": row["ctr"] * 100,
                "position": row["position"],
                "prev_position": prev.get("position"),
                "prev_clicks": prev.get("clicks"),
                "prev_impressions": int(prev["impressions"]) if prev.get("impressions") else None,
                "is_priority": is_priority,
                "has_city": has_city,
            })

    data["date_range_this"] = f"{start_this} to {end_this}"
    data["date_range_prev"] = f"{start_prev} to {end_prev}"

    return data


# ============================================================
# DATA COLLECTION: GBP PERFORMANCE
# ============================================================

GBP_METRICS = [
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    "BUSINESS_DIRECTION_REQUESTS",
    "CALL_CLICKS",
    "WEBSITE_CLICKS",
]

GBP_METRIC_LABELS = {
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": "Desktop Maps Views",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": "Desktop Search Views",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS": "Mobile Maps Views",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": "Mobile Search Views",
    "BUSINESS_DIRECTION_REQUESTS": "Direction Requests",
    "CALL_CLICKS": "Phone Call Clicks",
    "WEBSITE_CLICKS": "Website Clicks",
}


def collect_gbp_data(token, location_id):
    """Collect GBP Performance metrics. Returns None if API is not enabled."""
    import requests

    end = datetime.now() - timedelta(days=1)
    start_this = end - timedelta(days=6)
    start_prev = end - timedelta(days=13)
    end_prev = end - timedelta(days=7)

    def fetch_metric(metric, start_dt, end_dt):
        url = f"https://businessprofileperformance.googleapis.com/v1/{location_id}:getDailyMetricsTimeSeries"
        params = {
            "dailyMetric": metric,
            "dailyRange.startDate.year": start_dt.year,
            "dailyRange.startDate.month": start_dt.month,
            "dailyRange.startDate.day": start_dt.day,
            "dailyRange.endDate.year": end_dt.year,
            "dailyRange.endDate.month": end_dt.month,
            "dailyRange.endDate.day": end_dt.day,
        }
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        if resp.status_code == 403:
            return None  # API not enabled
        if resp.status_code != 200:
            print(f"GBP metric {metric} error ({resp.status_code})", file=sys.stderr)
            return 0
        data = resp.json()
        total = 0
        for series in data.get("timeSeries", {}).get("datedValues", []):
            total += int(series.get("value", 0))
        return total

    data = {"this": {}, "prev": {}}
    api_available = True

    for metric in GBP_METRICS:
        val_this = fetch_metric(metric, start_this, end)
        if val_this is None:
            api_available = False
            break
        val_prev = fetch_metric(metric, start_prev, end_prev)
        data["this"][metric] = val_this
        data["prev"][metric] = val_prev if val_prev is not None else 0

    if not api_available:
        return None

    # Roll up totals
    data["this"]["TOTAL_IMPRESSIONS"] = sum(
        data["this"].get(m, 0) for m in GBP_METRICS if "IMPRESSIONS" in m
    )
    data["prev"]["TOTAL_IMPRESSIONS"] = sum(
        data["prev"].get(m, 0) for m in GBP_METRICS if "IMPRESSIONS" in m
    )

    return data


# ============================================================
# REPORT GENERATION
# ============================================================

def delta_str(current, previous, fmt="d", better="up"):
    """Format a change indicator: +12% or -5%."""
    if previous is None or previous == 0:
        return ""
    pct = ((current - previous) / previous) * 100
    arrow = "+" if pct >= 0 else ""
    # For position, lower is better
    if better == "down":
        emoji = " (improving)" if pct < 0 else (" (declining)" if pct > 5 else "")
    else:
        emoji = " (improving)" if pct > 0 else (" (declining)" if pct < -5 else "")
    if fmt == "d":
        return f" ({arrow}{pct:.0f}%{emoji})"
    return f" ({arrow}{pct:.1f}%{emoji})"


def build_report(gsc_data, gbp_data):
    """Build the markdown report."""
    lines = []
    today_fmt = datetime.now().strftime("%B %d, %Y")
    lines.append(f"# Local SEO Health Check - {today_fmt}")
    lines.append("")
    lines.append(f"Data: {gsc_data['date_range_this']} vs {gsc_data['date_range_prev']}")
    lines.append("")

    # --- Section 1: The Big Picture ---
    lines.append("## 1. Are More People Finding Us?")
    lines.append("")

    t = gsc_data["totals_this"]
    p = gsc_data["totals_prev"]

    lines.append(f"| Metric | This Week | Last Week | Change |")
    lines.append(f"|--------|-----------|-----------|--------|")

    impr_change = ((t["impressions"] - p["impressions"]) / p["impressions"] * 100) if p["impressions"] > 0 else 0
    click_change = ((t["clicks"] - p["clicks"]) / p["clicks"] * 100) if p["clicks"] > 0 else 0
    ctr_change = t["ctr"] - p["ctr"]
    pos_change = p["position"] - t["position"]  # positive = improved

    lines.append(f"| Search Impressions | {t['impressions']:,} | {p['impressions']:,} | {'+' if impr_change >= 0 else ''}{impr_change:.0f}% |")
    lines.append(f"| Clicks to Site | {t['clicks']:,} | {p['clicks']:,} | {'+' if click_change >= 0 else ''}{click_change:.0f}% |")
    lines.append(f"| Click-Through Rate | {t['ctr']:.1f}% | {p['ctr']:.1f}% | {'+' if ctr_change >= 0 else ''}{ctr_change:.1f}pp |")
    lines.append(f"| Avg Position | {t['position']:.1f} | {p['position']:.1f} | {'improved' if pos_change > 0 else 'declined'} {abs(pos_change):.1f} |")
    lines.append("")

    # Quick verdict
    signals = []
    if impr_change > 5:
        signals.append("more people seeing us in search")
    elif impr_change < -5:
        signals.append("fewer people seeing us in search")
    if click_change > 5:
        signals.append("more clicks")
    elif click_change < -5:
        signals.append("fewer clicks")
    if pos_change > 0.3:
        signals.append("positions improving")
    elif pos_change < -0.3:
        signals.append("positions slipping")

    if signals:
        lines.append(f"**Summary:** {'; '.join(signals)}.")
    else:
        lines.append("**Summary:** Stable week, no major changes.")
    lines.append("")

    # --- Section 2: GBP Performance ---
    if gbp_data:
        lines.append("## 2. Google Business Profile Activity")
        lines.append("")
        lines.append(f"| Metric | This Week | Last Week | Change |")
        lines.append(f"|--------|-----------|-----------|--------|")

        total_this = gbp_data["this"]["TOTAL_IMPRESSIONS"]
        total_prev = gbp_data["prev"]["TOTAL_IMPRESSIONS"]
        total_change = ((total_this - total_prev) / total_prev * 100) if total_prev > 0 else 0
        lines.append(f"| Total Listing Views | {total_this:,} | {total_prev:,} | {'+' if total_change >= 0 else ''}{total_change:.0f}% |")

        for metric in ["CALL_CLICKS", "WEBSITE_CLICKS", "BUSINESS_DIRECTION_REQUESTS"]:
            v_this = gbp_data["this"].get(metric, 0)
            v_prev = gbp_data["prev"].get(metric, 0)
            change = ((v_this - v_prev) / v_prev * 100) if v_prev > 0 else 0
            label = GBP_METRIC_LABELS[metric]
            lines.append(f"| {label} | {v_this:,} | {v_prev:,} | {'+' if change >= 0 else ''}{change:.0f}% |")

        lines.append("")

        # Breakdown by surface
        lines.append("**Views by surface:**")
        for metric in GBP_METRICS:
            if "IMPRESSIONS" in metric:
                v = gbp_data["this"].get(metric, 0)
                label = GBP_METRIC_LABELS[metric]
                lines.append(f"  - {label}: {v:,}")
        lines.append("")
    else:
        lines.append("## 2. Google Business Profile Activity")
        lines.append("")
        lines.append("*GBP Performance API not yet enabled. Enable it at:*")
        lines.append("*console.cloud.google.com > APIs & Services > Library > Business Profile Performance API*")
        lines.append("")

    # --- Section 3: Priority Keywords ---
    section_num = 3
    lines.append(f"## {section_num}. Priority Keyword Tracking")
    lines.append("")

    priority = sorted(gsc_data["priority_matches"], key=lambda x: x["impressions"], reverse=True)

    if priority:
        lines.append(f"| Keyword | Pos | Prev Pos | Clicks | Impressions | CTR |")
        lines.append(f"|---------|-----|----------|--------|-------------|-----|")

        alerts = []
        for kw in priority[:20]:
            pos_str = f"{kw['position']:.1f}"
            prev_pos_str = f"{kw['prev_position']:.1f}" if kw["prev_position"] else "new"
            change_str = ""
            if kw["prev_position"]:
                diff = kw["prev_position"] - kw["position"]
                if abs(diff) >= 1:
                    change_str = f" ({'up' if diff > 0 else 'down'} {abs(diff):.0f})"
                    if diff >= 3:
                        alerts.append(f"'{kw['query']}' jumped up {diff:.0f} positions")
                    elif diff <= -3:
                        alerts.append(f"'{kw['query']}' dropped {abs(diff):.0f} positions")

            lines.append(
                f"| {kw['query'][:40]} | {pos_str} | {prev_pos_str}{change_str} | "
                f"{kw['clicks']} | {kw['impressions']:,} | {kw['ctr']:.1f}% |"
            )

        lines.append("")

        if alerts:
            lines.append("**Alerts:**")
            for a in alerts:
                lines.append(f"  - {a}")
            lines.append("")
    else:
        lines.append("No priority keyword matches found this week.")
        lines.append("")

    # --- Section 4: Top Queries (all) ---
    section_num += 1
    lines.append(f"## {section_num}. Top Queries (All)")
    lines.append("")
    lines.append(f"| Query | Clicks | Impressions | CTR | Position |")
    lines.append(f"|-------|--------|-------------|-----|----------|")

    for row in gsc_data["top_queries"][:25]:
        q = row["keys"][0][:45]
        lines.append(f"| {q} | {row['clicks']} | {int(row['impressions']):,} | {row['ctr']*100:.1f}% | {row['position']:.1f} |")

    lines.append("")

    # --- Section 5: Top Pages ---
    section_num += 1
    lines.append(f"## {section_num}. Top Pages")
    lines.append("")
    lines.append(f"| Page | Clicks | Impressions | CTR | Position |")
    lines.append(f"|------|--------|-------------|-----|----------|")

    for row in gsc_data["top_pages"][:15]:
        page = row["keys"][0].replace("https://blackhilllandscaping.com", "")
        if not page:
            page = "/"
        lines.append(f"| {page[:50]} | {row['clicks']} | {int(row['impressions']):,} | {row['ctr']*100:.1f}% | {row['position']:.1f} |")

    lines.append("")

    # --- Section 6: New Queries ---
    section_num += 1
    lines.append(f"## {section_num}. New Queries This Week")
    lines.append("")

    new_queries = [
        r for r in gsc_data["top_queries"]
        if r["keys"][0] not in gsc_data["prev_queries"] and r["impressions"] >= 3
    ]
    new_queries.sort(key=lambda x: x["impressions"], reverse=True)

    if new_queries:
        for nq in new_queries[:10]:
            lines.append(f"  - **{nq['keys'][0]}** - {int(nq['impressions'])} impressions, pos {nq['position']:.1f}")
        lines.append("")
    else:
        lines.append("No significant new queries this week.")
        lines.append("")

    return "\n".join(lines)


def build_html(markdown_text):
    """Convert the markdown report to simple HTML for email."""
    html = ['<html><body style="font-family: -apple-system, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; color: #333;">']

    in_table = False

    for line in markdown_text.split("\n"):
        # Skip markdown table separator lines
        if line.startswith("|") and set(line.replace("|", "").replace("-", "").strip()) == set():
            continue

        if line.startswith("# "):
            if in_table:
                html.append("</table>")
                in_table = False
            html.append(f'<h1 style="color: #1a5c2e; border-bottom: 2px solid #1a5c2e; padding-bottom: 8px;">{line[2:]}</h1>')
        elif line.startswith("## "):
            if in_table:
                html.append("</table>")
                in_table = False
            html.append(f'<h2 style="color: #2d7a45; margin-top: 24px;">{line[3:]}</h2>')
        elif line.startswith("| "):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not in_table:
                html.append('<table style="border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 13px;">')
                html.append("<tr>" + "".join(
                    f'<th style="border: 1px solid #ddd; padding: 6px 8px; background: #f5f5f5; text-align: left;">{c}</th>'
                    for c in cells
                ) + "</tr>")
                in_table = True
            else:
                row_html = "<tr>"
                for c in cells:
                    style = "border: 1px solid #ddd; padding: 6px 8px;"
                    if "improving" in c.lower() or ("+" in c and "%" in c):
                        style += " color: #1a7a2e; font-weight: bold;"
                    elif "declining" in c.lower() or (c.startswith("-") and "%" in c):
                        style += " color: #c0392b; font-weight: bold;"
                    row_html += f'<td style="{style}">{c}</td>'
                row_html += "</tr>"
                html.append(row_html)
        else:
            if in_table:
                html.append("</table>")
                in_table = False

            if line.startswith("**") and line.endswith("**"):
                html.append(f'<p style="font-weight: bold; margin: 12px 0 4px;">{line.strip("*")}</p>')
            elif line.startswith("**"):
                html.append(f'<p style="margin: 8px 0;">{line.replace("**", "<strong>", 1).replace("**", "</strong>", 1)}</p>')
            elif line.startswith("  - "):
                content = line[4:]
                content = content.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
                html.append(f'<li style="margin: 2px 0; margin-left: 20px;">{content}</li>')
            elif line.startswith("*") and line.endswith("*"):
                html.append(f'<p style="color: #888; font-style: italic;">{line.strip("*")}</p>')
            elif line.startswith("Data:"):
                html.append(f'<p style="color: #666; font-size: 13px;">{line}</p>')
            elif line.strip():
                html.append(f"<p>{line}</p>")

    if in_table:
        html.append("</table>")

    html.append("</body></html>")
    return "\n".join(html)


# ============================================================
# HISTORY TRACKING
# ============================================================

def update_history(gsc_data, gbp_data):
    """Append this week's summary to history file for long-term trending."""
    os.makedirs(REPORT_DIR, exist_ok=True)

    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            history = json.load(f)

    entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "date_range": gsc_data["date_range_this"],
        "gsc": {
            "impressions": gsc_data["totals_this"]["impressions"],
            "clicks": gsc_data["totals_this"]["clicks"],
            "ctr": round(gsc_data["totals_this"]["ctr"], 2),
            "avg_position": round(gsc_data["totals_this"]["position"], 2),
        },
        "priority_keywords": [
            {
                "query": kw["query"],
                "position": round(kw["position"], 1),
                "impressions": kw["impressions"],
                "clicks": kw["clicks"],
            }
            for kw in sorted(gsc_data["priority_matches"], key=lambda x: x["impressions"], reverse=True)[:10]
        ],
    }

    if gbp_data:
        entry["gbp"] = {
            "total_views": gbp_data["this"]["TOTAL_IMPRESSIONS"],
            "calls": gbp_data["this"].get("CALL_CLICKS", 0),
            "website_clicks": gbp_data["this"].get("WEBSITE_CLICKS", 0),
            "direction_requests": gbp_data["this"].get("BUSINESS_DIRECTION_REQUESTS", 0),
        }

    history.append(entry)

    # Keep 52 weeks of history
    history = history[-52:]

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

    return history


# ============================================================
# MAIN
# ============================================================

def main():
    print("Local SEO Health Check - starting...")

    # 1. Collect Search Console data
    print("Fetching Search Console data...")
    gsc_token, site_url = get_gsc_token()
    gsc_data = collect_gsc_data(gsc_token, site_url)
    print(f"  {gsc_data['totals_this']['impressions']:,} impressions, {gsc_data['totals_this']['clicks']} clicks this week")
    print(f"  {len(gsc_data['priority_matches'])} priority keyword matches")

    # 2. Collect GBP data (may fail if API not enabled)
    print("Fetching GBP Performance data...")
    gbp_data = None
    try:
        gbp_token, location_id = get_gbp_token()
        gbp_data = collect_gbp_data(gbp_token, location_id)
        if gbp_data:
            print(f"  {gbp_data['this']['TOTAL_IMPRESSIONS']:,} total listing views")
        else:
            print("  GBP Performance API not enabled - skipping (report will still generate)")
    except Exception as e:
        print(f"  GBP data unavailable: {e}")

    # 3. Build report
    print("Building report...")
    report_text = build_report(gsc_data, gbp_data)
    html_report = build_html(report_text)

    # 4. Save report
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_file = os.path.join(REPORT_DIR, f"health-{datetime.now().strftime('%Y-%m-%d')}.md")
    with open(report_file, "w") as f:
        f.write(report_text)
    print(f"Report saved: {report_file}")

    # 5. Update history
    history = update_history(gsc_data, gbp_data)
    print(f"History updated: {len(history)} weeks tracked")

    # 6. Send email
    gmail_email = os.environ.get("GMAIL_EMAIL", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_email or not gmail_password:
        print("No GMAIL_EMAIL / GMAIL_APP_PASSWORD configured. Report saved but email not sent.")
        sys.exit(0)

    today_fmt = datetime.now().strftime("%B %d, %Y")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Local SEO Health Check - {today_fmt}"
    msg["From"] = formataddr(("Black Hill Assistant", gmail_email))
    msg["To"] = TO_EMAIL

    msg.attach(MIMEText(report_text, "plain"))
    msg.attach(MIMEText(html_report, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(gmail_email, gmail_password)
            server.sendmail(gmail_email, TO_EMAIL, msg.as_string())
        print("Email sent successfully!")
    except Exception as e:
        print(f"Email send failed: {e}")
        print("Report was saved to file but email delivery failed.")


if __name__ == "__main__":
    main()
