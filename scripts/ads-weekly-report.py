#!/usr/bin/env python3
"""Weekly Google Ads report - runs via launchd every Sunday at 9 AM.
Rebuilt to focus on actionable metrics that move the needle.

Structure:
  Section 1: Did we move the needle? (3 KPIs + trend)
  Section 2: Where's the money going? (campaign breakdown, top spenders, waste)
  Section 3: Quality Score tracker (the leading indicator)

Scheduler: ~/Library/LaunchAgents/com.blackhill.ads-weekly-report.plist
Manual run: python3 ~/projects/scripts/ads-weekly-report.py
"""

import json, warnings, smtplib, os, sys, signal
import urllib.request, urllib.error, urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date

# --- Global timeout: kill the process if it runs longer than 5 minutes ---
SCRIPT_TIMEOUT = 420  # seconds

def _timeout_handler(signum, frame):
    print(f"ERROR: Script timed out after {SCRIPT_TIMEOUT}s", file=sys.stderr)
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(SCRIPT_TIMEOUT)

warnings.filterwarnings("ignore")
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# --- Config ---
TO_EMAIL = "evelin@blackhilltx.com"
TARGET_CPA = 80.0
TARGET_IMPR_SHARE = 50.0

# Repo root (works both locally and on GitHub Actions)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# QS history file (tracks Quality Scores week over week)
QS_HISTORY_FILE = os.path.join(
    REPO_ROOT, ".claude", "reports", "marketing", "google-ads", "weekly", "qs-history.json"
)

# --- Load credentials ---
# Prefer environment variables (GitHub Actions), fall back to local config file
config_file = os.path.expanduser("~/.config/google-ads/config.json")
if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
    ads_config = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        "customer_id": os.environ["GOOGLE_ADS_CUSTOMER_ID"],
    }
else:
    with open(config_file) as f:
        ads_config = json.load(f)

credentials = {
    "developer_token": ads_config["developer_token"],
    "client_id": ads_config["client_id"],
    "client_secret": ads_config["client_secret"],
    "refresh_token": ads_config["refresh_token"],
    "login_customer_id": ads_config["login_customer_id"],
    "use_proto_plus": True,
    "timeout": 60,
}
client = GoogleAdsClient.load_from_dict(credentials)
ga_service = client.get_service("GoogleAdsService")
customer_id = ads_config["customer_id"]

# --- Date ranges ---
now = datetime.now()
this_week_end = now.strftime("%Y-%m-%d")
this_week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
prev_week_end = (now - timedelta(days=7)).strftime("%Y-%m-%d")
prev_week_start = (now - timedelta(days=13)).strftime("%Y-%m-%d")
four_weeks_start = (now - timedelta(days=27)).strftime("%Y-%m-%d")
today_fmt = now.strftime("%B %d, %Y")
week_ago_fmt = (now - timedelta(days=6)).strftime("%b %d")

# --- Helpers ---
def delta_pct(current, previous):
    if previous == 0:
        return None
    return ((current - previous) / previous) * 100

def safe_query(query_str):
    """Run a GAQL query and return rows, silencing errors."""
    try:
        return list(ga_service.search(customer_id=customer_id, query=query_str))
    except (GoogleAdsException, Exception) as e:
        print(f"Query warning: {e}", file=sys.stderr)
        return []


# ============================================================
# DATA COLLECTION
# ============================================================

# --- 1. Account summary: this week, last week, 4-week avg ---
def get_account_metrics(date_start, date_end):
    rows = safe_query(f"""
        SELECT metrics.cost_micros, metrics.impressions, metrics.clicks,
               metrics.ctr, metrics.conversions, metrics.cost_per_conversion
        FROM customer
        WHERE segments.date BETWEEN '{date_start}' AND '{date_end}'
    """)
    for row in rows:
        m = row.metrics
        conv = m.conversions
        return {
            "spend": m.cost_micros / 1_000_000,
            "impressions": m.impressions,
            "clicks": m.clicks,
            "ctr": m.ctr * 100,
            "conversions": conv,
            "cpa": m.cost_per_conversion / 1_000_000 if conv > 0 else 0,
        }
    return {}

acct_this = get_account_metrics(this_week_start, this_week_end)
acct_prev = get_account_metrics(prev_week_start, prev_week_end)
acct_4wk = get_account_metrics(four_weeks_start, this_week_end)
# Normalize 4-week to weekly average
if acct_4wk:
    for k in ["spend", "impressions", "clicks", "conversions"]:
        acct_4wk[k] = acct_4wk[k] / 4

# --- 2. Campaign performance with impression share breakdown ---
camp_data = {}
for label, ds, de in [("this", this_week_start, this_week_end), ("prev", prev_week_start, prev_week_end)]:
    rows = safe_query(f"""
        SELECT campaign.name, campaign.id,
               metrics.cost_micros, metrics.clicks, metrics.impressions,
               metrics.ctr, metrics.conversions, metrics.cost_per_conversion,
               metrics.search_impression_share,
               metrics.search_rank_lost_impression_share,
               metrics.search_budget_lost_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{ds}' AND '{de}'
          AND campaign.status = 'ENABLED'
          AND metrics.impressions > 0
        ORDER BY metrics.cost_micros DESC
    """)
    for row in rows:
        name = row.campaign.name
        m = row.metrics
        if name not in camp_data:
            camp_data[name] = {"this": {}, "prev": {}}
        camp_data[name][label] = {
            "spend": m.cost_micros / 1_000_000,
            "clicks": m.clicks,
            "impressions": m.impressions,
            "ctr": m.ctr * 100,
            "conversions": m.conversions,
            "cpa": m.cost_per_conversion / 1_000_000 if m.conversions > 0 else 0,
            "impr_share": m.search_impression_share * 100 if m.search_impression_share else 0,
            "lost_rank": m.search_rank_lost_impression_share * 100 if m.search_rank_lost_impression_share else 0,
            "lost_budget": m.search_budget_lost_impression_share * 100 if m.search_budget_lost_impression_share else 0,
        }

# --- 3. Top keywords by spend (what's eating the budget) ---
top_keywords = []
rows = safe_query(f"""
    SELECT ad_group_criterion.keyword.text, campaign.name,
           metrics.cost_micros, metrics.clicks, metrics.impressions,
           metrics.conversions, metrics.ctr
    FROM keyword_view
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND campaign.status = 'ENABLED'
      AND ad_group_criterion.status = 'ENABLED'
      AND metrics.impressions > 0
    ORDER BY metrics.cost_micros DESC
    LIMIT 10
""")
for row in rows:
    top_keywords.append({
        "keyword": row.ad_group_criterion.keyword.text,
        "campaign": row.campaign.name,
        "spend": row.metrics.cost_micros / 1_000_000,
        "clicks": row.metrics.clicks,
        "conversions": row.metrics.conversions,
        "ctr": row.metrics.ctr * 100,
    })

# --- 4. Budget waste: non-converting search terms ---
waste_terms = []
rows = safe_query(f"""
    SELECT search_term_view.search_term, campaign.name,
           metrics.cost_micros, metrics.clicks, metrics.conversions
    FROM search_term_view
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND campaign.status = 'ENABLED'
      AND metrics.clicks >= 2
      AND metrics.conversions = 0
    ORDER BY metrics.cost_micros DESC
    LIMIT 10
""")
for row in rows:
    waste_terms.append({
        "term": row.search_term_view.search_term,
        "campaign": row.campaign.name,
        "spend": row.metrics.cost_micros / 1_000_000,
        "clicks": row.metrics.clicks,
    })

# --- 5. Quality Score tracker ---
qs_keywords = []
rows = safe_query(f"""
    SELECT ad_group_criterion.keyword.text, campaign.name, ad_group.name,
           ad_group_criterion.quality_info.quality_score,
           ad_group_criterion.quality_info.search_predicted_ctr,
           ad_group_criterion.quality_info.creative_quality_score,
           ad_group_criterion.quality_info.post_click_quality_score
    FROM keyword_view
    WHERE campaign.status = 'ENABLED'
      AND ad_group.status = 'ENABLED'
      AND ad_group_criterion.status = 'ENABLED'
      AND segments.date DURING LAST_7_DAYS
""")
seen = set()
for row in rows:
    kw = row.ad_group_criterion.keyword.text
    if kw in seen:
        continue
    seen.add(kw)
    qi = row.ad_group_criterion.quality_info
    if qi.quality_score and qi.quality_score > 0:
        qs_keywords.append({
            "keyword": kw,
            "campaign": row.campaign.name,
            "qs": qi.quality_score,
            "ctr": qi.search_predicted_ctr.name if qi.search_predicted_ctr else "N/A",
            "relevance": qi.creative_quality_score.name if qi.creative_quality_score else "N/A",
            "landing": qi.post_click_quality_score.name if qi.post_click_quality_score else "N/A",
        })

# Load QS history for comparison
qs_history = {}
if os.path.exists(QS_HISTORY_FILE):
    try:
        with open(QS_HISTORY_FILE) as f:
            qs_history = json.load(f)
    except:
        pass

prior_qs = {}
today_key = now.strftime("%Y-%m-%d")
sorted_dates = sorted(d for d in qs_history.keys() if d != today_key)
if sorted_dates:
    prior_qs = qs_history[sorted_dates[-1]]

# Save current QS
current_qs = {kw["keyword"]: kw["qs"] for kw in qs_keywords}
qs_history[today_key] = current_qs
if len(qs_history) > 12:
    for old in sorted(qs_history.keys())[:-12]:
        del qs_history[old]
os.makedirs(os.path.dirname(QS_HISTORY_FILE), exist_ok=True)
with open(QS_HISTORY_FILE, "w") as f:
    json.dump(qs_history, f, indent=2)

# --- 6. Converting search terms (winners) ---
converting_terms = []
rows = safe_query(f"""
    SELECT search_term_view.search_term, campaign.name,
           metrics.cost_micros, metrics.clicks, metrics.conversions, metrics.ctr
    FROM search_term_view
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND metrics.conversions >= 1
    ORDER BY metrics.conversions DESC
    LIMIT 10
""")
for row in rows:
    converting_terms.append({
        "term": row.search_term_view.search_term,
        "campaign": row.campaign.name,
        "spend": row.metrics.cost_micros / 1_000_000,
        "clicks": row.metrics.clicks,
        "conversions": row.metrics.conversions,
        "ctr": row.metrics.ctr * 100,
    })

# --- 7. RSA ad copy (headline/description) performance ---
asset_agg = {}
rows = safe_query(f"""
    SELECT asset.text_asset.text, ad_group_ad_asset_view.field_type,
           metrics.impressions, metrics.clicks, metrics.ctr, metrics.conversions
    FROM ad_group_ad_asset_view
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND ad_group_ad_asset_view.field_type IN ('HEADLINE', 'DESCRIPTION')
      AND metrics.impressions > 0
    ORDER BY metrics.impressions DESC
""")
for row in rows:
    text = row.asset.text_asset.text
    ftype = row.ad_group_ad_asset_view.field_type.name
    key = (text, ftype)
    if key not in asset_agg:
        asset_agg[key] = {"text": text, "type": ftype, "impressions": 0, "clicks": 0, "conversions": 0}
    asset_agg[key]["impressions"] += row.metrics.impressions
    asset_agg[key]["clicks"] += row.metrics.clicks
    asset_agg[key]["conversions"] += row.metrics.conversions

for v in asset_agg.values():
    v["ctr"] = (v["clicks"] / v["impressions"] * 100) if v["impressions"] > 0 else 0.0

headlines = sorted([v for v in asset_agg.values() if v["type"] == "HEADLINE"], key=lambda x: -x["impressions"])
descriptions = sorted([v for v in asset_agg.values() if v["type"] == "DESCRIPTION"], key=lambda x: -x["impressions"])

# --- 8. Device breakdown ---
device_data = {}
rows = safe_query(f"""
    SELECT segments.device,
           metrics.cost_micros, metrics.clicks, metrics.conversions, metrics.impressions
    FROM campaign
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND campaign.status = 'ENABLED'
""")
for row in rows:
    dev = row.segments.device.name
    if dev not in device_data:
        device_data[dev] = {"spend": 0, "clicks": 0, "conversions": 0, "impressions": 0}
    device_data[dev]["spend"] += row.metrics.cost_micros / 1_000_000
    device_data[dev]["clicks"] += row.metrics.clicks
    device_data[dev]["conversions"] += row.metrics.conversions
    device_data[dev]["impressions"] += row.metrics.impressions

# --- 9. Hour-of-day breakdown ---
hour_data = {}
rows = safe_query(f"""
    SELECT segments.hour,
           metrics.cost_micros, metrics.clicks, metrics.conversions
    FROM campaign
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND campaign.status = 'ENABLED'
""")
for row in rows:
    hr = row.segments.hour
    if hr not in hour_data:
        hour_data[hr] = {"spend": 0, "clicks": 0, "conversions": 0}
    hour_data[hr]["spend"] += row.metrics.cost_micros / 1_000_000
    hour_data[hr]["clicks"] += row.metrics.clicks
    hour_data[hr]["conversions"] += row.metrics.conversions

CST_OFFSET = -5  # CDT (April = daylight saving)
def bucket_hours(hdata):
    blocks = [
        ("12a-4a", range(0, 4)),
        ("4a-8a", range(4, 8)),
        ("8a-12p", range(8, 12)),
        ("12p-4p", range(12, 16)),
        ("4p-8p", range(16, 20)),
        ("8p-12a", range(20, 24)),
    ]
    result = []
    for label, hrs in blocks:
        b = {"label": label, "spend": 0, "clicks": 0, "conversions": 0}
        for utc_hr in range(24):
            cst_hr = (utc_hr + CST_OFFSET) % 24
            if cst_hr in hrs and utc_hr in hdata:
                b["spend"] += hdata[utc_hr]["spend"]
                b["clicks"] += hdata[utc_hr]["clicks"]
                b["conversions"] += hdata[utc_hr]["conversions"]
        result.append(b)
    return result

hour_blocks = bucket_hours(hour_data)

# --- 10. Aspire won revenue from WhatConverts leads only ---
def _load_wc_contact_map():
    """Load WhatConverts lead mappings → {aspire_contact_id: traffic_source}."""
    state_path = os.path.join(REPO_ROOT, "data", "processed-state.json")
    if not os.path.exists(state_path):
        return {}
    with open(state_path) as f:
        state = json.load(f)
    contact_map = {}
    for _wc_id, info in state.get("lead_mappings", {}).items():
        cid = info.get("aspire_contact_id")
        if cid:
            contact_map[int(cid)] = info.get("traffic_source", "unknown")
    return contact_map


def get_aspire_revenue(start_date, end_date):
    try:
        client_id = (os.environ.get("ASPIRE_REPORTING_CLIENT_ID")
                     or os.environ.get("ASPIRE_CLIENT_ID"))
        secret = (os.environ.get("ASPIRE_REPORTING_SECRET")
                  or os.environ.get("ASPIRE_SECRET"))
        if not client_id or not secret:
            cfg_path = os.path.expanduser("~/.config/aspire/config.json")
            if not os.path.exists(cfg_path):
                return None
            with open(cfg_path) as f:
                cfg = json.load(f)
            client_id = cfg.get("reporting_client_id", cfg.get("client_id"))
            secret = cfg.get("reporting_secret", cfg.get("secret"))
        base_url = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
        auth_data = json.dumps({"ClientId": client_id, "Secret": secret}).encode()
        auth_req = urllib.request.Request(
            f"{base_url}/Authorization",
            data=auth_data, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(auth_req, timeout=15) as resp:
            token = json.loads(resp.read().decode()).get("Token", "")
        if not token:
            return None
        odata_filter = (f"OpportunityStatusName eq 'Won' "
                        f"and WonDate ge {start_date}T00:00:00Z "
                        f"and WonDate le {end_date}T23:59:59Z")
        params = f"$filter={odata_filter}&$select=WonDollars,OpportunityName,BillingContactID"
        url = f"{base_url}/Opportunities?{urllib.parse.quote(params, safe='=&$,()/%:@')}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            opps = data if isinstance(data, list) else [data]

        # Cross-reference with WhatConverts lead mappings
        wc_map = _load_wc_contact_map()
        cpc_won = 0.0
        cpc_count = 0
        organic_won = 0.0
        organic_count = 0
        other_won = 0.0
        other_count = 0
        for o in opps:
            dollars = float(o.get("WonDollars", 0) or 0)
            cid = o.get("BillingContactID")
            if not cid or int(cid) not in wc_map:
                continue
            source = wc_map[int(cid)].lower()
            if "cpc" in source:
                cpc_won += dollars
                cpc_count += 1
            elif "organic" in source:
                organic_won += dollars
                organic_count += 1
            else:
                other_won += dollars
                other_count += 1

        total_wc = cpc_won + organic_won + other_won
        total_count = cpc_count + organic_count + other_count
        return {
            "total_won": total_wc,
            "count": total_count,
            "cpc_won": cpc_won,
            "cpc_count": cpc_count,
            "organic_won": organic_won,
            "organic_count": organic_count,
            "other_won": other_won,
            "other_count": other_count,
        }
    except Exception as e:
        print(f"Aspire revenue query skipped: {e}", file=sys.stderr)
        return None

aspire_revenue = get_aspire_revenue(this_week_start, this_week_end)


# ============================================================
# IMPRESSION SHARE (weighted average)
# ============================================================
total_impr_this = sum(c.get("this", {}).get("impressions", 0) for c in camp_data.values())
total_impr_prev = sum(c.get("prev", {}).get("impressions", 0) for c in camp_data.values())
avg_is_this = 0
avg_is_prev = 0
if total_impr_this > 0:
    avg_is_this = sum(
        c.get("this", {}).get("impr_share", 0) * c.get("this", {}).get("impressions", 0)
        for c in camp_data.values()
    ) / total_impr_this
if total_impr_prev > 0:
    avg_is_prev = sum(
        c.get("prev", {}).get("impr_share", 0) * c.get("prev", {}).get("impressions", 0)
        for c in camp_data.values()
    ) / total_impr_prev


# ============================================================
# RECOMMENDATIONS ENGINE
# ============================================================

def generate_recommendations():
    recs = []
    brand_patterns = ["black hill", "blackhill", "bh landscaping", "bh landscape"]
    for w in waste_terms:
        if w["spend"] >= 15:
            recs.append({"priority": "high", "action": "Add as negative keyword",
                         "detail": f'"{w["term"]}" spent ${w["spend"]:.0f} with 0 conversions'})
    for w in waste_terms:
        if any(bp in w["term"].lower() for bp in brand_patterns):
            recs.append({"priority": "medium", "action": "Brand term in waste",
                         "detail": f'"{w["term"]}" (${w["spend"]:.2f}) - add as negative or create brand campaign'})
    for name, cd in camp_data.items():
        t = cd.get("this", {})
        if t.get("lost_budget", 0) > 30:
            recs.append({"priority": "high", "action": "Consider budget increase",
                         "detail": f'{name}: losing {t["lost_budget"]:.0f}% impression share to budget'})
    for name, cd in camp_data.items():
        t = cd.get("this", {})
        if t.get("lost_rank", 0) > 50:
            recs.append({"priority": "medium", "action": "Improve QS or increase bids",
                         "detail": f'{name}: losing {t["lost_rank"]:.0f}% impression share to ad rank'})
    for kw in qs_keywords:
        if kw["qs"] <= 3:
            recs.append({"priority": "high", "action": "Priority QS fix needed",
                         "detail": f'"{kw["keyword"]}" has QS {kw["qs"]} in {kw["campaign"]}'})
    priority_order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: priority_order.get(r["priority"], 9))
    return recs[:10]

recommendations = generate_recommendations()


# ============================================================
# NEEDLE-MOVER VERDICT
# ============================================================

def generate_verdict():
    """Generate a plain-English verdict: did we move the needle, and why?"""
    if not acct_this:
        return None

    t = acct_this
    p = acct_prev or {}
    spend = t["spend"]
    conv_this = t["conversions"]
    conv_prev = p.get("conversions", 0)
    cpa_this = t["cpa"]
    cpa_prev = p.get("cpa", 0)
    is_this = avg_is_this
    is_prev = avg_is_prev

    # Revenue context
    cpc_rev = aspire_revenue["cpc_won"] if aspire_revenue else 0
    org_rev = aspire_revenue["organic_won"] if aspire_revenue else 0
    other_rev = aspire_revenue["other_won"] if aspire_revenue else 0
    total_wc_rev = aspire_revenue["total_won"] if aspire_revenue else 0
    cpc_count = aspire_revenue["cpc_count"] if aspire_revenue else 0

    lines = []

    # -- Verdict --
    if conv_this == 0:
        verdict = "no"
        verdict_color = "#e74c3c"
        lines.append(f"We spent ${spend:.0f} on ads this week and got zero conversions.")
    elif conv_this > conv_prev and conv_prev > 0:
        pct_up = ((conv_this - conv_prev) / conv_prev) * 100 if conv_prev else 0
        if cpa_this <= TARGET_CPA:
            verdict = "yes"
            verdict_color = "#27ae60"
            lines.append(f"Conversions up {pct_up:.0f}% ({conv_prev:.0f} to {conv_this:.0f}) and CPA is ${cpa_this:.0f}, under our ${TARGET_CPA:.0f} target.")
        else:
            verdict = "mixed"
            verdict_color = "#f39c12"
            lines.append(f"Conversions up {pct_up:.0f}% ({conv_prev:.0f} to {conv_this:.0f}), but CPA is ${cpa_this:.0f} -- above our ${TARGET_CPA:.0f} target.")
    elif conv_this == conv_prev and conv_this > 0:
        verdict = "flat"
        verdict_color = "#f39c12"
        lines.append(f"Conversions held flat at {conv_this:.0f} on ${spend:.0f} in ad spend.")
    elif conv_this < conv_prev and conv_prev > 0:
        pct_down = ((conv_prev - conv_this) / conv_prev) * 100
        verdict = "no"
        verdict_color = "#e74c3c"
        lines.append(f"Conversions dropped {pct_down:.0f}% ({conv_prev:.0f} to {conv_this:.0f}) on ${spend:.0f} in spend.")
    elif conv_prev == 0 and conv_this > 0:
        verdict = "yes"
        verdict_color = "#27ae60"
        lines.append(f"Got {conv_this:.0f} conversion{'s' if conv_this != 1 else ''} this week vs zero last week.")
    else:
        verdict = "flat"
        verdict_color = "#f39c12"
        lines.append(f"Spent ${spend:.0f} with {conv_this:.0f} conversions this week.")

    # -- Why: impression share driver --
    is_delta = is_this - is_prev
    if abs(is_delta) > 3:
        direction = "up" if is_delta > 0 else "down"
        lines.append(f"Impression share moved {direction} ({is_prev:.0f}% to {is_this:.0f}%).")
        # Diagnose the biggest driver
        biggest_rank_loss = max((cd.get("this", {}).get("lost_rank", 0) for cd in camp_data.values()), default=0)
        biggest_budget_loss = max((cd.get("this", {}).get("lost_budget", 0) for cd in camp_data.values()), default=0)
        if biggest_rank_loss > 40:
            lines.append(f"Ad rank is the main blocker -- losing up to {biggest_rank_loss:.0f}% to low QS or bids.")
        if biggest_budget_loss > 30:
            lines.append(f"Budget is capping visibility -- losing up to {biggest_budget_loss:.0f}% to daily budget limits.")

    # -- Revenue tie-in --
    if cpc_rev > 0:
        roi = ((cpc_rev - spend) / spend * 100) if spend > 0 else 0
        lines.append(f"Ad-sourced leads closed ${cpc_rev:,.0f} in revenue ({cpc_count} opp{'s' if cpc_count != 1 else ''}). ROI: {roi:+.0f}%.")
    elif spend > 0:
        lines.append(f"No ad-sourced revenue closed this week. ${spend:.0f} spent with no return yet.")

    if org_rev > 0:
        lines.append(f"Meanwhile, organic leads closed ${org_rev:,.0f} at zero ad cost.")

    # -- Verdict label --
    verdict_labels = {
        "yes": "Yes -- we moved the needle.",
        "no": "No -- we did not move the needle.",
        "mixed": "Mixed -- progress but not where we need to be.",
        "flat": "Flat -- no meaningful change this week.",
    }

    return {
        "verdict": verdict,
        "verdict_label": verdict_labels.get(verdict, ""),
        "verdict_color": verdict_color,
        "explanation": " ".join(lines),
    }

verdict_data = generate_verdict()


# ============================================================
# HTML EMAIL
# ============================================================

STYLES = """
body { margin:0; padding:0; background:#111; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color:#e0e0e0; }
.wrap { max-width:680px; margin:0 auto; background:#1a1a1a; }
.header { background:linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%); padding:28px 32px 20px; border-bottom:2px solid #c8963e; }
.header h1 { margin:0 0 4px; font-size:20px; color:#fff; font-weight:700; letter-spacing:0.5px; }
.header .period { font-size:13px; color:#c8963e; font-weight:500; }
.section { padding:24px 32px; border-bottom:1px solid #2a2a2a; }
.section h2 { margin:0 0 16px; font-size:15px; color:#c8963e; text-transform:uppercase; letter-spacing:1px; font-weight:600; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; padding:10px 12px; background:#222; color:#c8963e; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; border-bottom:2px solid #333; }
td { padding:10px 12px; border-bottom:1px solid #2a2a2a; color:#ccc; }
tr:hover td { background:#222; }
.right { text-align:right; }
.footer { padding:20px 32px; text-align:center; font-size:11px; color:#555; }
"""

def color_val(val, good_fn):
    """Color a value green/red based on good_fn."""
    color = "#27ae60" if good_fn(val) else "#e74c3c"
    return f'<span style="color:{color};font-weight:600;">{val}</span>'

def change_arrow(current, previous, inverse=False):
    pct = delta_pct(current, previous)
    if pct is None:
        return '<span style="color:#888;">--</span>'
    arrow = "&#9650;" if pct >= 0 else "&#9660;"
    if inverse:
        color = "#e74c3c" if pct > 0 else "#27ae60"
    else:
        color = "#27ae60" if pct > 0 else "#e74c3c"
    return f'<span style="color:{color};">{arrow} {abs(pct):.0f}%</span>'

def qs_component_short(name):
    """Shorten QS component names."""
    return {"BELOW_AVERAGE": "Low", "AVERAGE": "Avg", "ABOVE_AVERAGE": "High"}.get(name, "—")

def qs_component_color(name):
    colors = {"BELOW_AVERAGE": "#e74c3c", "AVERAGE": "#f39c12", "ABOVE_AVERAGE": "#27ae60"}
    return colors.get(name, "#666")

h_parts = []
h = h_parts.append

h(f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>{STYLES}</style></head><body>')
h('<div class="wrap">')

# --- Header ---
h(f'<div class="header"><h1>Weekly Google Ads Report</h1>')
h(f'<div class="period">{week_ago_fmt} &mdash; {today_fmt}</div></div>')

# ============================================================
# SECTION 1: DID WE MOVE THE NEEDLE?
# ============================================================
if acct_this:
    t = acct_this
    p = acct_prev if acct_prev else {}
    avg = acct_4wk if acct_4wk else {}

    h('<div class="section">')
    h('<h2>Did We Move the Needle?</h2>')

    # Verdict narrative
    if verdict_data:
        h(f'<div style="background:#222;border-radius:8px;padding:16px 20px;margin-bottom:16px;border-left:4px solid {verdict_data["verdict_color"]};">')
        h(f'<div style="font-size:16px;font-weight:700;color:{verdict_data["verdict_color"]};margin-bottom:8px;">{verdict_data["verdict_label"]}</div>')
        h(f'<div style="font-size:13px;color:#ccc;line-height:1.6;">{verdict_data["explanation"]}</div>')
        h(f'</div>')

    # Three KPI cards in a table for email compatibility
    h('<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>')

    # Conversions
    conv_color = "#27ae60" if t["conversions"] >= (avg.get("conversions", 10)) else "#e74c3c"
    h(f'<td width="33%" style="padding:0 6px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">Conversions</div>')
    h(f'<div style="font-size:28px;font-weight:700;color:{conv_color};">{t["conversions"]:.0f}</div>')
    h(f'<div style="font-size:12px;margin-top:4px;">{change_arrow(t["conversions"], p.get("conversions", 0))} vs last week</div>')
    h(f'<div style="font-size:11px;color:#666;margin-top:4px;">4-wk avg: {avg.get("conversions", 0):.1f}/wk</div>')
    h(f'</div></td>')

    # CPA
    cpa_color = "#27ae60" if t["cpa"] <= TARGET_CPA and t["cpa"] > 0 else "#e74c3c" if t["cpa"] > 0 else "#666"
    cpa_display = f'${t["cpa"]:.0f}' if t["cpa"] > 0 else "N/A"
    h(f'<td width="33%" style="padding:0 6px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">CPA</div>')
    h(f'<div style="font-size:28px;font-weight:700;color:{cpa_color};">{cpa_display}</div>')
    h(f'<div style="font-size:12px;margin-top:4px;">{change_arrow(t["cpa"], p.get("cpa", 0), inverse=True)} vs last week</div>')
    h(f'<div style="font-size:11px;color:#666;margin-top:4px;">Target: ${TARGET_CPA:.0f}</div>')
    h(f'</div></td>')

    # Impression Share
    is_color = "#27ae60" if avg_is_this >= TARGET_IMPR_SHARE else "#f39c12" if avg_is_this >= 30 else "#e74c3c"
    h(f'<td width="33%" style="padding:0 6px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">Impression Share</div>')
    h(f'<div style="font-size:28px;font-weight:700;color:{is_color};">{avg_is_this:.0f}%</div>')
    h(f'<div style="font-size:12px;margin-top:4px;">{change_arrow(avg_is_this, avg_is_prev)} vs last week</div>')
    h(f'<div style="font-size:11px;color:#666;margin-top:4px;">Target: {TARGET_IMPR_SHARE:.0f}%</div>')
    h(f'</div></td>')

    h('</tr></table>')

    # Spend context line
    h(f'<div style="text-align:center;margin-top:12px;font-size:12px;color:#888;">')
    h(f'Spend: ${t["spend"]:.0f} this week &bull; {t["clicks"]:,} clicks &bull; {t["ctr"]:.1f}% CTR &bull; {t["impressions"]:,} impressions')
    h(f'</div>')

    h('</div>')

# ============================================================
# SECTION 2: REVENUE CONTEXT (WhatConverts leads only)
# ============================================================
if aspire_revenue and acct_this:
    h('<div class="section">')
    h('<h2>Revenue from Website Leads</h2>')
    h('<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>')
    h(f'<td width="33%" style="padding:0 4px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">Ad Spend</div>')
    h(f'<div style="font-size:26px;font-weight:700;color:#e74c3c;">${acct_this["spend"]:.0f}</div>')
    h(f'</div></td>')
    cpc_color = "#27ae60" if aspire_revenue["cpc_won"] > 0 else "#888"
    h(f'<td width="33%" style="padding:0 4px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">Won from Ads</div>')
    h(f'<div style="font-size:26px;font-weight:700;color:{cpc_color};">${aspire_revenue["cpc_won"]:,.0f}</div>')
    h(f'<div style="font-size:11px;color:#666;margin-top:4px;">{aspire_revenue["cpc_count"]} opp{"s" if aspire_revenue["cpc_count"] != 1 else ""}</div>')
    h(f'</div></td>')
    org_color = "#3498db" if aspire_revenue["organic_won"] > 0 else "#888"
    h(f'<td width="33%" style="padding:0 4px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">Won from Organic</div>')
    h(f'<div style="font-size:26px;font-weight:700;color:{org_color};">${aspire_revenue["organic_won"]:,.0f}</div>')
    h(f'<div style="font-size:11px;color:#666;margin-top:4px;">{aspire_revenue["organic_count"]} opp{"s" if aspire_revenue["organic_count"] != 1 else ""}</div>')
    h(f'</div></td>')
    h('</tr></table>')
    if aspire_revenue["other_won"] > 0:
        h(f'<div style="text-align:center;margin-top:6px;font-size:11px;color:#666;">+ ${aspire_revenue["other_won"]:,.0f} from other sources ({aspire_revenue["other_count"]} opps)</div>')
    h(f'<div style="text-align:center;margin-top:6px;font-size:11px;color:#555;">Only counting revenue from leads tracked in WhatConverts</div>')
    h('</div>')

# ============================================================
# SECTION 3: WHAT TO DO THIS WEEK
# ============================================================
if recommendations:
    h('<div class="section">')
    h('<h2>What to Do This Week</h2>')
    h(f'<div style="font-size:12px;color:#888;margin-bottom:12px;">{len(recommendations)} action items from this week\'s data</div>')
    for rec in recommendations:
        border_color = "#e74c3c" if rec["priority"] == "high" else "#f39c12"
        h(f'<div style="margin-bottom:8px;padding:10px 14px;background:#222;border-radius:6px;border-left:3px solid {border_color};">')
        h(f'<div style="font-size:13px;color:#fff;font-weight:600;">{rec["action"]}</div>')
        h(f'<div style="font-size:12px;color:#aaa;margin-top:4px;">{rec["detail"]}</div>')
        h(f'</div>')
    h('</div>')
else:
    h('<div class="section">')
    h('<h2>What to Do This Week</h2>')
    h('<div style="padding:16px;background:#1a2a1a;border-radius:8px;text-align:center;color:#27ae60;font-size:14px;">No urgent actions this week. Everything looks healthy.</div>')
    h('</div>')

# ============================================================
# SECTION 4: WHERE'S THE MONEY GOING?
# ============================================================

# --- Campaign breakdown with impression share diagnosis ---
if camp_data:
    h('<div class="section">')
    h('<h2>Where\'s the Money Going?</h2>')

    h('<table><tr><th>Campaign</th><th class="right">Spend</th><th class="right">Conv</th><th class="right">CPA</th><th class="right">Impr Share</th><th class="right">Lost to Rank</th><th class="right">Lost to Budget</th></tr>')

    for name in sorted(camp_data.keys(), key=lambda n: -camp_data[n].get("this", {}).get("spend", 0)):
        t = camp_data[name].get("this", {})
        if not t:
            continue
        p = camp_data[name].get("prev", {})
        cpa_str = f"${t['cpa']:.0f}" if t['conversions'] > 0 else '<span style="color:#666;">—</span>'
        cpa_color = "#27ae60" if t['conversions'] > 0 and t['cpa'] <= TARGET_CPA else "#e74c3c" if t['conversions'] > 0 else "#666"

        is_color = "#27ae60" if t['impr_share'] >= TARGET_IMPR_SHARE else "#f39c12" if t['impr_share'] >= 30 else "#e74c3c"
        rank_color = "#e74c3c" if t['lost_rank'] > 40 else "#f39c12" if t['lost_rank'] > 20 else "#27ae60"
        budget_color = "#e74c3c" if t['lost_budget'] > 40 else "#f39c12" if t['lost_budget'] > 20 else "#27ae60"

        spend_arrow = f' {change_arrow(t["spend"], p.get("spend", 0), inverse=True)}' if p else ""
        conv_arrow = f' {change_arrow(t["conversions"], p.get("conversions", 0))}' if p else ""
        is_arrow = f' {change_arrow(t["impr_share"], p.get("impr_share", 0))}' if p else ""

        h(f'<tr>')
        h(f'<td style="font-weight:500;color:#fff;">{name}</td>')
        h(f'<td class="right">${t["spend"]:.0f}{spend_arrow}</td>')
        h(f'<td class="right">{t["conversions"]:.0f}{conv_arrow}</td>')
        h(f'<td class="right"><span style="color:{cpa_color};">{cpa_str}</span></td>')
        h(f'<td class="right"><span style="color:{is_color};font-weight:600;">{t["impr_share"]:.0f}%</span>{is_arrow}</td>')
        h(f'<td class="right"><span style="color:{rank_color};">{t["lost_rank"]:.0f}%</span></td>')
        h(f'<td class="right"><span style="color:{budget_color};">{t["lost_budget"]:.0f}%</span></td>')
        h(f'</tr>')

    h('</table>')
    h('<div style="font-size:11px;color:#555;margin-top:8px;">Lost to Rank = Quality Score / bid too low &bull; Lost to Budget = daily budget ran out</div>')
    h('</div>')

# --- Converting search terms (winners) ---
if converting_terms:
    h('<div class="section">')
    h('<h2>Converting Search Terms</h2>')
    h('<div style="font-size:12px;color:#888;margin-bottom:12px;">What people searched to find us &mdash; and converted.</div>')
    h('<table><tr><th>Search Term</th><th class="right">Conv</th><th class="right">Spend</th><th class="right">Clicks</th><th class="right">CTR</th><th>Campaign</th></tr>')
    for ct in converting_terms[:7]:
        h(f'<tr>')
        h(f'<td style="font-weight:500;color:#fff;">{ct["term"]}</td>')
        h(f'<td class="right"><span style="color:#27ae60;font-weight:600;">{ct["conversions"]:.0f}</span></td>')
        h(f'<td class="right">${ct["spend"]:.2f}</td>')
        h(f'<td class="right">{ct["clicks"]}</td>')
        h(f'<td class="right">{ct["ctr"]:.1f}%</td>')
        h(f'<td style="font-size:12px;color:#888;">{ct["campaign"]}</td>')
        h(f'</tr>')
    h('</table>')
    h('</div>')

# --- Top keywords by spend ---
if top_keywords:
    h('<div class="section">')
    h('<h2>Top Keywords by Spend</h2>')
    h('<div style="font-size:12px;color:#888;margin-bottom:12px;">Are the biggest spenders converting?</div>')
    h('<table><tr><th>Keyword</th><th class="right">Spend</th><th class="right">Clicks</th><th class="right">Conv</th><th class="right">CTR</th></tr>')

    for kw in top_keywords[:7]:
        conv_color = "#27ae60" if kw["conversions"] > 0 else "#e74c3c"
        row_bg = "" if kw["conversions"] > 0 else ' style="background:#1a1212;"'
        h(f'<tr{row_bg}>')
        h(f'<td style="font-weight:500;color:#fff;">{kw["keyword"]}</td>')
        h(f'<td class="right">${kw["spend"]:.2f}</td>')
        h(f'<td class="right">{kw["clicks"]}</td>')
        h(f'<td class="right"><span style="color:{conv_color};font-weight:600;">{kw["conversions"]:.0f}</span></td>')
        h(f'<td class="right">{kw["ctr"]:.1f}%</td>')
        h(f'</tr>')

    h('</table>')
    h('</div>')

# --- Budget waste ---
if waste_terms:
    total_waste = sum(w["spend"] for w in waste_terms)
    h('<div class="section">')
    h('<h2>Budget Waste</h2>')
    h(f'<div style="background:#2a1a1a;border:1px solid #e74c3c;border-radius:8px;padding:14px 16px;margin-bottom:16px;text-align:center;">')
    h(f'<span style="font-size:24px;font-weight:700;color:#e74c3c;">${total_waste:.0f}</span>')
    h(f'<span style="font-size:13px;color:#ccc;"> spent on {len(waste_terms)} non-converting search terms</span>')
    h(f'</div>')

    h('<table><tr><th>Search Term</th><th class="right">Clicks</th><th class="right">Spend</th><th>Campaign</th></tr>')
    for w in waste_terms[:7]:
        h(f'<tr><td style="color:#e74c3c;">{w["term"]}</td><td class="right">{w["clicks"]}</td><td class="right">${w["spend"]:.2f}</td><td style="font-size:12px;color:#888;">{w["campaign"]}</td></tr>')
    h('</table>')
    h('<div style="font-size:11px;color:#555;margin-top:8px;">Search terms with 2+ clicks and 0 conversions this week</div>')
    h('</div>')

# --- Ad copy performance ---
if headlines or descriptions:
    h('<div class="section">')
    h('<h2>Ad Copy Performance</h2>')
    h('<div style="font-size:12px;color:#888;margin-bottom:12px;">RSA asset performance this week</div>')
    if headlines:
        h('<div style="font-size:13px;color:#c8963e;font-weight:600;margin-bottom:8px;">Top Headlines</div>')
        h('<table><tr><th>Headline</th><th class="right">Impr</th><th class="right">Clicks</th><th class="right">CTR</th><th class="right">Conv</th></tr>')
        for hl in headlines[:5]:
            ctr_color = "#27ae60" if hl["ctr"] >= 5 else "#f39c12" if hl["ctr"] >= 3 else "#ccc"
            h(f'<tr><td style="font-weight:500;color:#fff;max-width:250px;overflow:hidden;text-overflow:ellipsis;">{hl["text"]}</td>')
            h(f'<td class="right">{hl["impressions"]:,}</td><td class="right">{hl["clicks"]}</td>')
            h(f'<td class="right"><span style="color:{ctr_color};">{hl["ctr"]:.1f}%</span></td>')
            h(f'<td class="right"><span style="color:#27ae60;font-weight:600;">{hl["conversions"]:.0f}</span></td></tr>')
        h('</table>')
    if descriptions:
        h(f'<div style="font-size:13px;color:#c8963e;font-weight:600;margin:{"16px" if headlines else "0"} 0 8px;">Top Descriptions</div>')
        h('<table><tr><th>Description</th><th class="right">Impr</th><th class="right">Clicks</th><th class="right">CTR</th><th class="right">Conv</th></tr>')
        for desc in descriptions[:5]:
            ctr_color = "#27ae60" if desc["ctr"] >= 5 else "#f39c12" if desc["ctr"] >= 3 else "#ccc"
            h(f'<tr><td style="font-weight:500;color:#fff;max-width:300px;overflow:hidden;text-overflow:ellipsis;font-size:12px;">{desc["text"]}</td>')
            h(f'<td class="right">{desc["impressions"]:,}</td><td class="right">{desc["clicks"]}</td>')
            h(f'<td class="right"><span style="color:{ctr_color};">{desc["ctr"]:.1f}%</span></td>')
            h(f'<td class="right"><span style="color:#27ae60;font-weight:600;">{desc["conversions"]:.0f}</span></td></tr>')
        h('</table>')
    h('</div>')

# --- Device & timing ---
if device_data or hour_blocks:
    DEVICE_NAMES = {"MOBILE": "Mobile", "DESKTOP": "Desktop", "TABLET": "Tablet", "CONNECTED_TV": "Connected TV", "OTHER": "Other"}
    h('<div class="section">')
    h('<h2>Device &amp; Timing</h2>')
    if device_data:
        h('<div style="font-size:13px;color:#c8963e;font-weight:600;margin-bottom:8px;">By Device</div>')
        h('<table><tr><th>Device</th><th class="right">Spend</th><th class="right">Clicks</th><th class="right">Conv</th><th class="right">Conv Rate</th></tr>')
        for dev in sorted(device_data.keys(), key=lambda d: -device_data[d]["spend"]):
            dd = device_data[dev]
            if dd["spend"] < 1:
                continue
            conv_rate = (dd["conversions"] / dd["clicks"] * 100) if dd["clicks"] > 0 else 0
            cr_color = "#27ae60" if conv_rate >= 5 else "#f39c12" if conv_rate >= 2 else "#ccc"
            h(f'<tr><td style="font-weight:500;color:#fff;">{DEVICE_NAMES.get(dev, dev)}</td>')
            h(f'<td class="right">${dd["spend"]:.0f}</td><td class="right">{dd["clicks"]}</td>')
            h(f'<td class="right">{dd["conversions"]:.0f}</td><td class="right"><span style="color:{cr_color};">{conv_rate:.1f}%</span></td></tr>')
        h('</table>')
    if hour_blocks:
        best_block = max(hour_blocks, key=lambda b: b["conversions"])
        quiet_block = min((b for b in hour_blocks if b["spend"] > 0), key=lambda b: b["conversions"], default=None)
        h(f'<div style="font-size:13px;color:#c8963e;font-weight:600;margin:{"16px" if device_data else "0"} 0 8px;">By Time of Day (CST)</div>')
        h('<table><tr><th>Time Block</th><th class="right">Spend</th><th class="right">Clicks</th><th class="right">Conv</th></tr>')
        for blk in hour_blocks:
            if blk["spend"] < 0.50:
                continue
            row_style = ' style="background:#1a2a1a;"' if blk["label"] == best_block["label"] else ""
            h(f'<tr{row_style}><td style="font-weight:500;color:#fff;">{blk["label"]}</td>')
            h(f'<td class="right">${blk["spend"]:.0f}</td><td class="right">{blk["clicks"]}</td><td class="right">{blk["conversions"]:.0f}</td></tr>')
        h('</table>')
        if best_block and quiet_block:
            h(f'<div style="font-size:11px;color:#555;margin-top:8px;">Peak: {best_block["label"]} ({best_block["conversions"]:.0f} conv) &bull; Quiet: {quiet_block["label"]}</div>')
    h('</div>')

# ============================================================
# QUALITY SCORE TRACKER
# ============================================================
if qs_keywords:
    # Sort: lowest QS first (problems at top)
    qs_keywords.sort(key=lambda x: x["qs"])

    h('<div class="section">')
    h('<h2>Quality Score Tracker</h2>')

    # WoW summary: how many keywords below QS 5
    below5_now = sum(1 for kw in qs_keywords if kw["qs"] < 5)
    below5_prev = sum(1 for qs in prior_qs.values() if qs < 5) if prior_qs else None
    total_kw = len(qs_keywords)
    avg_qs = sum(kw["qs"] for kw in qs_keywords) / total_kw if total_kw else 0

    if below5_prev is not None:
        diff = below5_now - below5_prev
        if diff < 0:
            trend_text = f'<span style="color:#27ae60;font-weight:600;">Improved -- {abs(diff)} fewer keyword{"s" if abs(diff) != 1 else ""} below QS 5 vs last week</span>'
        elif diff > 0:
            trend_text = f'<span style="color:#e74c3c;font-weight:600;">Worse -- {diff} more keyword{"s" if diff != 1 else ""} dropped below QS 5 vs last week</span>'
        else:
            trend_text = f'<span style="color:#f39c12;font-weight:600;">No change vs last week</span>'
        h(f'<div style="background:#222;border-radius:8px;padding:14px 18px;margin-bottom:14px;border:1px solid #333;">')
        h(f'<div style="font-size:14px;color:#fff;font-weight:600;margin-bottom:6px;">{below5_now} of {total_kw} keywords below QS 5 <span style="color:#666;font-weight:400;">(was {below5_prev} last week)</span></div>')
        h(f'<div style="font-size:13px;">{trend_text}</div>')
        h(f'<div style="font-size:11px;color:#666;margin-top:4px;">Average QS: {avg_qs:.1f}</div>')
        h(f'</div>')
    else:
        h(f'<div style="background:#222;border-radius:8px;padding:14px 18px;margin-bottom:14px;border:1px solid #333;">')
        h(f'<div style="font-size:14px;color:#fff;font-weight:600;">{below5_now} of {total_kw} keywords below QS 5</div>')
        h(f'<div style="font-size:11px;color:#666;margin-top:4px;">Average QS: {avg_qs:.1f} (no prior week for comparison)</div>')
        h(f'</div>')

    h('<table><tr><th>Keyword</th><th class="right">QS</th><th class="right">Trend</th><th class="right">Exp CTR</th><th class="right">Ad Rel</th><th class="right">Land Page</th></tr>')

    for kw in qs_keywords:
        qs = kw["qs"]
        qs_color = "#e74c3c" if qs <= 4 else "#f39c12" if qs <= 6 else "#27ae60"

        # Week-over-week QS change
        prior = prior_qs.get(kw["keyword"])
        if prior is not None:
            diff = qs - prior
            if diff > 0:
                trend = f'<span style="color:#27ae60;">&#9650; +{diff}</span>'
            elif diff < 0:
                trend = f'<span style="color:#e74c3c;">&#9660; {diff}</span>'
            else:
                trend = '<span style="color:#888;">&#9644;</span>'
        else:
            trend = '<span style="color:#888;">new</span>'

        ctr_color = qs_component_color(kw["ctr"])
        rel_color = qs_component_color(kw["relevance"])
        lp_color = qs_component_color(kw["landing"])

        h(f'<tr>')
        h(f'<td style="font-weight:500;color:#fff;max-width:200px;overflow:hidden;text-overflow:ellipsis;">{kw["keyword"]}</td>')
        h(f'<td class="right"><span style="color:{qs_color};font-weight:700;font-size:16px;">{qs}</span></td>')
        h(f'<td class="right">{trend}</td>')
        h(f'<td class="right"><span style="color:{ctr_color};">{qs_component_short(kw["ctr"])}</span></td>')
        h(f'<td class="right"><span style="color:{rel_color};">{qs_component_short(kw["relevance"])}</span></td>')
        h(f'<td class="right"><span style="color:{lp_color};">{qs_component_short(kw["landing"])}</span></td>')
        h(f'</tr>')

    h('</table>')

    # QS distribution summary
    qs_dist = {}
    for kw in qs_keywords:
        bucket = "1-4" if kw["qs"] <= 4 else "5-6" if kw["qs"] <= 6 else "7-10"
        qs_dist[bucket] = qs_dist.get(bucket, 0) + 1

    h('<div style="margin-top:12px;font-size:12px;color:#888;">')
    bad = qs_dist.get("1-4", 0)
    mid = qs_dist.get("5-6", 0)
    good = qs_dist.get("7-10", 0)
    if bad > 0:
        h(f'<span style="color:#e74c3c;">&#9632;</span> {bad} keywords QS 1-4 (hurting impressions) &nbsp;')
    if mid > 0:
        h(f'<span style="color:#f39c12;">&#9632;</span> {mid} keywords QS 5-6 (average) &nbsp;')
    if good > 0:
        h(f'<span style="color:#27ae60;">&#9632;</span> {good} keywords QS 7-10 (strong)')
    h('</div>')

    # --- Headline changes to watch (auto-expires after 3 weeks) ---
    headline_watch = [
        {
            "date": "2026-04-23",
            "keywords": ["all 11 Low CTR keywords"],
            "ad_group": "Irrigation Repair",
            "changes": "Replaced 11 process/brand headlines with keyword-match + CTAs across 2 RSAs. Control (Ad 791498112705) unchanged.",
        },
        {
            "date": "2026-04-23",
            "keywords": ["french drain fort worth", "yard drainage solutions", "drainage near me"],
            "ad_group": "Drainage Solutions",
            "changes": "Replaced 11 headlines with keyword-match + dynamic insertion across 2 RSAs. Control (Ad 791538738859) unchanged.",
        },
        {
            "date": "2026-04-23",
            "keywords": ["sod installers near me", "landscaping in fort worth", "landscaping near me"],
            "ad_group": "Landscape & Sod Installation",
            "changes": "Replaced 10 headlines: added keyword insertion, 'near me' variants, direct CTAs. Control (Ad 797866469019) unchanged.",
        },
        {
            "date": "2026-04-23",
            "keywords": ["lawn fertilization fort worth", "lawn treatment fort worth", "weed control"],
            "ad_group": "Fertilization & Weed Control",
            "changes": "Replaced 11 headlines with keyword-match + CTAs across 2 RSAs. Control (Ad 797865249975) unchanged.",
        },
        {
            "date": "2026-04-23",
            "keywords": ["sprinkler system installation", "irrigation install near me"],
            "ad_group": "Sprinkler Installations",
            "changes": "Replaced 9 headlines with keyword-match + dynamic insertion across 2 RSAs. Control (Ad 791538623020) unchanged.",
        },
        {
            "date": "2026-04-23",
            "keywords": ["landscape maintenance near me", "commercial landscaping fort worth"],
            "ad_group": "Property Mgr + Commercial",
            "changes": "Replaced 10 headlines with keyword-match + dynamic insertion. 1 RSA per ad group updated.",
        },
    ]
    from datetime import datetime as _dt
    active_watches = [w for w in headline_watch if (_dt.now() - _dt.strptime(w["date"], "%Y-%m-%d")).days <= 21]
    if active_watches:
        h('<div style="margin-top:16px;padding:12px;background:#1a2332;border-left:3px solid #3498db;border-radius:4px;">')
        h('<div style="font-weight:600;color:#3498db;margin-bottom:8px;">Headline Changes to Watch</div>')
        for w in active_watches:
            days_ago = (_dt.now() - _dt.strptime(w["date"], "%Y-%m-%d")).days
            h(f'<div style="font-size:11px;color:#aaa;margin-bottom:6px;">')
            h(f'<strong>{w["ad_group"]}</strong> (changed {days_ago}d ago) &mdash; watching: {", ".join(w["keywords"])}')
            h(f'<br/><span style="color:#888;">{w["changes"]}</span>')
            h(f'</div>')
        h('</div>')

    # --- Impression share strategy tracker (auto-expires) ---
    impr_share_plan = {
        "start_date": "2026-04-23",
        "review_date": "2026-05-07",
        "phase": "Phase 1: QS Improvement",
        "strategy": "62 headlines replaced across 7 ad groups to improve Expected CTR and Ad Relevance. "
                     "Landing page updates sent to web dev. Overnight hours (12a-6a) excluded. "
                     "If impression share hasn't improved by May 7, consider adding target CPA to Maximize Conversions.",
        "current_bidding": "Maximize Conversions (all 4 search campaigns)",
        "target": "Impression share from 14% to 30%+ without bidding changes",
    }
    review_dt = _dt.strptime(impr_share_plan["review_date"], "%Y-%m-%d")
    days_until_review = (review_dt - _dt.now()).days
    if days_until_review >= -7:  # show for 1 week past review date
        review_color = "#f39c12" if days_until_review <= 3 else "#3498db"
        h(f'<div style="margin-top:16px;padding:12px;background:#1a2a1a;border-left:3px solid {review_color};border-radius:4px;">')
        h(f'<div style="font-weight:600;color:{review_color};margin-bottom:8px;">Impression Share Strategy</div>')
        h(f'<div style="font-size:12px;color:#aaa;margin-bottom:4px;"><strong>{impr_share_plan["phase"]}</strong></div>')
        h(f'<div style="font-size:11px;color:#888;margin-bottom:4px;">{impr_share_plan["strategy"]}</div>')
        h(f'<div style="font-size:11px;color:#888;">Bidding: {impr_share_plan["current_bidding"]}</div>')
        if days_until_review > 0:
            h(f'<div style="font-size:11px;color:{review_color};margin-top:6px;">Review in {days_until_review} days ({impr_share_plan["review_date"]})</div>')
        else:
            h(f'<div style="font-size:11px;color:#f39c12;margin-top:6px;font-weight:600;">REVIEW DUE: Check if impression share improved. If not, add target CPA.</div>')
        h('</div>')

    h('</div>')

# --- Footer ---
h('<div class="footer">')
h(f'<div>Targets: CPA &le; ${TARGET_CPA:.0f} &nbsp;|&nbsp; Impr Share &ge; {TARGET_IMPR_SHARE:.0f}%</div>')
h(f'<div style="margin-top:4px;">Black Hill Landscaping &bull; Weekly Google Ads Report</div>')
h('</div>')

h('</div></body></html>')
html_report = "\n".join(h_parts)


# ============================================================
# MARKDOWN REPORT (for file archive)
# ============================================================
md = []
md.append(f"# Black Hill Landscaping - Weekly Google Ads Report")
md.append(f"**Period**: {week_ago_fmt} - {today_fmt}\n")

if acct_this:
    t = acct_this
    p = acct_prev or {}
    avg = acct_4wk or {}

    md.append("## Did We Move the Needle?")
    md.append(f"| Metric | This Week | Last Week | 4-Wk Avg | Target |")
    md.append(f"|--------|-----------|-----------|----------|--------|")
    md.append(f"| Conversions | {t['conversions']:.0f} | {p.get('conversions',0):.0f} | {avg.get('conversions',0):.1f} | 10/wk |")
    cpa_str = f"${t['cpa']:.0f}" if t['cpa'] > 0 else "N/A"
    md.append(f"| CPA | {cpa_str} | ${p.get('cpa',0):.0f} | — | ${TARGET_CPA:.0f} |")
    md.append(f"| Impression Share | {avg_is_this:.0f}% | {avg_is_prev:.0f}% | — | {TARGET_IMPR_SHARE:.0f}% |")
    md.append(f"| Spend | ${t['spend']:.0f} | ${p.get('spend',0):.0f} | ${avg.get('spend',0):.0f} | — |")
    md.append(f"| Clicks | {t['clicks']} | {p.get('clicks',0)} | {avg.get('clicks',0):.0f} | — |")
    md.append(f"| CTR | {t['ctr']:.1f}% | {p.get('ctr',0):.1f}% | — | — |")
    md.append("")

if aspire_revenue and acct_this:
    md.append("## Revenue Context")
    md.append(f"| Metric | Amount |")
    md.append(f"|--------|--------|")
    md.append(f"| Ad Spend This Week | ${acct_this['spend']:.0f} |")
    md.append(f"| Won Revenue This Week | ${aspire_revenue['total_won']:,.0f} ({aspire_revenue['count']} opportunities) |")
    md.append("")
    md.append("*Correlation only -- not direct attribution between ads and won deals*")
    md.append("")

if recommendations:
    md.append("## What to Do This Week")
    for i, rec in enumerate(recommendations, 1):
        tag = rec["priority"].upper()
        md.append(f"{i}. **[{tag}]** {rec['action']} -- {rec['detail']}")
    md.append("")
else:
    md.append("## What to Do This Week")
    md.append("No urgent actions this week. Everything looks healthy.")
    md.append("")

if camp_data:
    md.append("## Where's the Money Going?")
    md.append(f"| Campaign | Spend | Conv | CPA | Impr Share | Lost to Rank | Lost to Budget |")
    md.append(f"|----------|-------|------|-----|------------|-------------|----------------|")
    for name in sorted(camp_data.keys(), key=lambda n: -camp_data[n].get("this", {}).get("spend", 0)):
        t = camp_data[name].get("this", {})
        if not t:
            continue
        p = camp_data[name].get("prev", {})
        cpa_str = f"${t['cpa']:.0f}" if t['conversions'] > 0 else "---"
        spend_delta, conv_delta, is_delta = "", "", ""
        if p:
            sd = delta_pct(t["spend"], p.get("spend", 0))
            cd = delta_pct(t["conversions"], p.get("conversions", 0))
            isd = delta_pct(t["impr_share"], p.get("impr_share", 0))
            if sd is not None:
                spend_delta = f" ({'+' if sd >= 0 else ''}{sd:.0f}%)"
            if cd is not None:
                conv_delta = f" ({'+' if cd >= 0 else ''}{cd:.0f}%)"
            if isd is not None:
                is_delta = f" ({'+' if isd >= 0 else ''}{isd:.0f}%)"
        md.append(f"| {name} | ${t['spend']:.0f}{spend_delta} | {t['conversions']:.0f}{conv_delta} | {cpa_str} | {t['impr_share']:.0f}%{is_delta} | {t['lost_rank']:.0f}% | {t['lost_budget']:.0f}% |")
    md.append("")

if converting_terms:
    md.append("## Converting Search Terms")
    md.append(f"| Search Term | Conv | Spend | Clicks | CTR | Campaign |")
    md.append(f"|-------------|------|-------|--------|-----|----------|")
    for ct in converting_terms[:7]:
        md.append(f"| {ct['term']} | {ct['conversions']:.0f} | ${ct['spend']:.2f} | {ct['clicks']} | {ct['ctr']:.1f}% | {ct['campaign']} |")
    md.append("")

if top_keywords:
    md.append("## Top Keywords by Spend")
    md.append(f"| Keyword | Spend | Clicks | Conv | CTR |")
    md.append(f"|---------|-------|--------|------|-----|")
    for kw in top_keywords[:7]:
        md.append(f"| {kw['keyword']} | ${kw['spend']:.2f} | {kw['clicks']} | {kw['conversions']:.0f} | {kw['ctr']:.1f}% |")
    md.append("")

if waste_terms:
    total_waste = sum(w["spend"] for w in waste_terms)
    md.append(f"## Budget Waste: ${total_waste:.0f} on {len(waste_terms)} non-converting terms")
    md.append(f"| Search Term | Clicks | Spend | Campaign |")
    md.append(f"|-------------|--------|-------|----------|")
    for w in waste_terms[:7]:
        md.append(f"| {w['term']} | {w['clicks']} | ${w['spend']:.2f} | {w['campaign']} |")
    md.append("")

if headlines or descriptions:
    md.append("## Ad Copy Performance")
    if headlines:
        md.append("### Top Headlines")
        md.append(f"| Headline | Impr | Clicks | CTR | Conv |")
        md.append(f"|----------|------|--------|-----|------|")
        for hl in headlines[:5]:
            md.append(f"| {hl['text']} | {hl['impressions']:,} | {hl['clicks']} | {hl['ctr']:.1f}% | {hl['conversions']:.0f} |")
    if descriptions:
        md.append("### Top Descriptions")
        md.append(f"| Description | Impr | Clicks | CTR | Conv |")
        md.append(f"|-------------|------|--------|-----|------|")
        for desc in descriptions[:5]:
            md.append(f"| {desc['text']} | {desc['impressions']:,} | {desc['clicks']} | {desc['ctr']:.1f}% | {desc['conversions']:.0f} |")
    md.append("")

if device_data or hour_blocks:
    md.append("## Device & Timing")
    if device_data:
        DEVICE_NAMES_MD = {"MOBILE": "Mobile", "DESKTOP": "Desktop", "TABLET": "Tablet", "CONNECTED_TV": "Connected TV", "OTHER": "Other"}
        md.append("### By Device")
        md.append(f"| Device | Spend | Clicks | Conv | Conv Rate |")
        md.append(f"|--------|-------|--------|------|-----------|")
        for dev in sorted(device_data.keys(), key=lambda d: -device_data[d]["spend"]):
            dd = device_data[dev]
            if dd["spend"] < 1:
                continue
            conv_rate = (dd["conversions"] / dd["clicks"] * 100) if dd["clicks"] > 0 else 0
            md.append(f"| {DEVICE_NAMES_MD.get(dev, dev)} | ${dd['spend']:.0f} | {dd['clicks']} | {dd['conversions']:.0f} | {conv_rate:.1f}% |")
    if hour_blocks:
        md.append("### By Time of Day (CST)")
        md.append(f"| Time Block | Spend | Clicks | Conv |")
        md.append(f"|------------|-------|--------|------|")
        for blk in hour_blocks:
            if blk["spend"] < 0.50:
                continue
            md.append(f"| {blk['label']} | ${blk['spend']:.0f} | {blk['clicks']} | {blk['conversions']:.0f} |")
        best_block_md = max(hour_blocks, key=lambda b: b["conversions"])
        md.append(f"\n*Peak: {best_block_md['label']} ({best_block_md['conversions']:.0f} conversions)*")
    md.append("")

if qs_keywords:
    md.append("## Quality Score Tracker")
    md.append(f"| Keyword | QS | Exp CTR | Ad Rel | Landing Page |")
    md.append(f"|---------|----|---------| -------|-------------|")
    for kw in qs_keywords:
        prior = prior_qs.get(kw["keyword"])
        trend = ""
        if prior is not None:
            diff = kw["qs"] - prior
            if diff != 0:
                trend = f" ({'+' if diff > 0 else ''}{diff})"
        md.append(f"| {kw['keyword']} | {kw['qs']}{trend} | {qs_component_short(kw['ctr'])} | {qs_component_short(kw['relevance'])} | {qs_component_short(kw['landing'])} |")
    md.append("")

if days_until_review >= -7:
    md.append("## Impression Share Strategy")
    md.append(f"**{impr_share_plan['phase']}** (review: {impr_share_plan['review_date']})")
    md.append(f"- {impr_share_plan['strategy']}")
    md.append(f"- Bidding: {impr_share_plan['current_bidding']}")
    if days_until_review > 0:
        md.append(f"- Review in {days_until_review} days")
    else:
        md.append(f"- **REVIEW DUE**: Check if impression share improved. If not, add target CPA.")
    md.append("")

md.append(f"---\n*Targets: CPA <= ${TARGET_CPA:.0f} | Impr Share >= {TARGET_IMPR_SHARE:.0f}%*")

report_text = "\n".join(md)


# ============================================================
# SAVE & SEND
# ============================================================

report_dir = os.path.join(REPO_ROOT, ".claude", "reports", "marketing", "google-ads", "weekly")
os.makedirs(report_dir, exist_ok=True)
report_file = os.path.join(report_dir, f"{now.strftime('%Y-%m-%d')}.md")
with open(report_file, "w") as f:
    f.write(report_text)
print(f"Report saved: {report_file}")

# Send email via Gmail
gmail_email = os.environ.get("GMAIL_EMAIL", "")
gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
if not gmail_email or not gmail_password:
    print("No GMAIL_EMAIL / GMAIL_APP_PASSWORD configured. Report saved but email not sent.")
    sys.exit(0)

from email.utils import formataddr
msg = MIMEMultipart("alternative")
msg["Subject"] = f"Weekly Google Ads Report - {today_fmt}"
msg["From"] = formataddr(("Black Hill Assistant", gmail_email))
msg["To"] = TO_EMAIL

msg.attach(MIMEText(report_text, "plain"))
msg.attach(MIMEText(html_report, "html"))

try:
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(gmail_email, gmail_password)
        server.sendmail(gmail_email, TO_EMAIL, msg.as_string())
    print("Email sent successfully via Gmail!")
except Exception as e:
    print(f"Email send failed: {e}")
    print("Report was saved to file but email delivery failed.")
