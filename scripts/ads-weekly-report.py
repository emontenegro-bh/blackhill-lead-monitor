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


# --- Global timeout: kill the process if it runs longer than 10 minutes ---
SCRIPT_TIMEOUT = 600  # seconds

def _timeout_handler(signum, frame):
    print(f"ERROR: Script timed out after {SCRIPT_TIMEOUT}s", file=sys.stderr)
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(SCRIPT_TIMEOUT)

warnings.filterwarnings("ignore")
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

# Parsed early (before any state is touched) so --dry-run can guard the
# Supabase run-tracking call immediately below, not just the email send.
DRY_RUN = "--dry-run" in sys.argv

# No main() to wrap, so the run is opened here and closed at each
# successful exit below. Anything that leaves without calling done()
# is recorded as an error by db.track_flat's atexit hook.
# Under --dry-run this must not touch Supabase at all, so it gets a
# no-op stand-in instead of a real tracked run.
if DRY_RUN:
    class _NoOpRun:
        def done(self, records=None):
            pass
    _run = _NoOpRun()
else:
    _run = db.track_flat("ads-weekly-report")

# --- Config ---
TO_EMAILS = ["evelin@blackhilltx.com", "Umair@blackhilltx.com", "afaq@blackhilltx.com"]
TO_EMAIL = ", ".join(TO_EMAILS)  # comma-joined for the "To" header
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

# --- 4. Negative keyword recommendations: non-converting search terms (28-day window) ---
# 28-day window so the section is meaningfully populated each week (a single week
# rarely has enough qualifying terms to act on).
waste_terms = []
rows = safe_query(f"""
    SELECT search_term_view.search_term, campaign.name,
           metrics.cost_micros, metrics.clicks, metrics.conversions
    FROM search_term_view
    WHERE segments.date BETWEEN '{four_weeks_start}' AND '{this_week_end}'
      AND campaign.status = 'ENABLED'
      AND metrics.clicks >= 2
      AND metrics.conversions = 0
    ORDER BY metrics.cost_micros DESC
    LIMIT 15
""")
# Exclude own/sister brand variants -- never recommend these as negatives.
BRAND_SAFE = ("black hill", "blackhill", "mean green", "meangreen")

# Exclude core money terms -- a 0-conversion 28-day window on a service phrase
# like "sprinkler repair" is noise, not evidence the term is bad; blocking it
# would cut off the account's own bread-and-butter searches.
CORE_SAFE = (
    "sprinkler repair", "irrigation repair", "sprinkler service", "irrigation service",
    "sprinkler system repair", "irrigation system repair", "drainage", "standing water", "french drain",
    "yard drainage", "sod install", "sod installation", "landscape design", "landscaping",
)

# Pull existing negatives (shared lists + campaign-level) so we never
# re-recommend a term that is already blocked. 28-day spend on a term can
# predate the negative that now blocks it.
existing_negs = []
for r in safe_query("""
    SELECT shared_criterion.keyword.text, shared_criterion.keyword.match_type
    FROM shared_criterion WHERE shared_criterion.type = 'KEYWORD'
"""):
    existing_negs.append((r.shared_criterion.keyword.text.lower(),
                          r.shared_criterion.keyword.match_type.name))
for r in safe_query("""
    SELECT campaign_criterion.keyword.text, campaign_criterion.keyword.match_type
    FROM campaign_criterion
    WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD'
"""):
    existing_negs.append((r.campaign_criterion.keyword.text.lower(),
                          r.campaign_criterion.keyword.match_type.name))

def _already_blocked(term):
    t = term.lower()
    words = t.split()
    for neg, mtype in existing_negs:
        if mtype == "EXACT" and t == neg:
            return True
        if mtype == "PHRASE":
            nwords = neg.split()
            if any(words[i:i + len(nwords)] == nwords for i in range(len(words))):
                return True
        if mtype == "BROAD" and all(w in words for w in neg.split()):
            return True
    return False

already_blocked_terms = []
for row in rows:
    term = row.search_term_view.search_term
    if any(b in term.lower() for b in BRAND_SAFE):
        continue
    if any(c in term.lower() for c in CORE_SAFE):
        continue
    entry = {
        "term": term,
        "campaign": row.campaign.name,
        "spend": row.metrics.cost_micros / 1_000_000,
        "clicks": row.metrics.clicks,
    }
    if _already_blocked(term):
        already_blocked_terms.append(entry)
    else:
        waste_terms.append(entry)

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

# Save current QS. Skipped under --dry-run: this is persisted state (used as
# next week's "prior_qs" comparison), and a test run must not leave a trace.
if not DRY_RUN:
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
      AND metrics.conversions > 0
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

# segments.hour is ALREADY in the account's timezone (America/Chicago) -- Google
# reports hour-of-day in account-local time with DST handled. Do NOT apply an
# offset. (Bug fixed 2026-06-14: previously subtracted 5h assuming the hours were
# UTC, which shifted every block ~5h earlier and invented overnight "waste" that
# did not exist -- live data showed $0 spend 12a-8a, all spend 8a-midnight.)
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
        for hr in hrs:
            if hr in hdata:
                b["spend"] += hdata[hr]["spend"]
                b["clicks"] += hdata[hr]["clicks"]
                b["conversions"] += hdata[hr]["conversions"]
        result.append(b)
    return result

hour_blocks = bucket_hours(hour_data)

# Section 10 removed 2026-08-12: get_aspire_revenue() and its WhatConverts
# contact map were superseded by get_aspire_revenue_v2() below, which reads
# Lead Source from Aspire contact custom field 34 and so catches commercial
# deals where the billing contact differs from the lead contact. The old
# function had already stopped being called.

# --- 10b. Aspire won revenue by Lead Source custom field (def 34) ---
# Customer-origin, property-level: a customer belongs to one origin source and ALL
# their won opps roll up to it. Replaces the WhatConverts billing-contact map (which
# missed commercial deals where the billing contact != the lead contact, e.g. Restore Church).
_PAID_SRC = {"Google Ads", "Bing Ads"}
_ORGANIC_SRC = {"Google Organic", "Bing Organic", "Google Business Profile"}
_SRC_COLOR = {"Google Ads": "#27ae60", "Bing Ads": "#16a085", "Google Organic": "#3498db",
              "Bing Organic": "#2980b9", "Google Business Profile": "#9b59b6",
              "Postcard Mania": "#e91e63", "Referral": "#f39c12", "Website": "#95a5a6",
              "Phone Call": "#e67e22"}


def get_aspire_revenue_v2(start_date, end_date):
    try:
        client_id = (os.environ.get("ASPIRE_REPORTING_CLIENT_ID") or os.environ.get("ASPIRE_CLIENT_ID"))
        secret = (os.environ.get("ASPIRE_REPORTING_SECRET") or os.environ.get("ASPIRE_SECRET"))
        if not client_id or not secret:
            cfg_path = os.path.expanduser("~/.config/aspire/config.json")
            if not os.path.exists(cfg_path):
                return None
            with open(cfg_path) as f:
                cfg = json.load(f)
            client_id = cfg.get("reporting_client_id", cfg.get("client_id"))
            secret = cfg.get("reporting_secret", cfg.get("secret"))
        base = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
        auth = json.dumps({"ClientId": client_id, "Secret": secret}).encode()
        areq = urllib.request.Request(f"{base}/Authorization", data=auth,
                                      headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(areq, timeout=15) as resp:
            token = json.loads(resp.read().decode()).get("Token", "")
        if not token:
            return None

        def paged(entity, params, ps=500):
            out = []
            skip = 0
            while True:
                url = f"{base}/{entity}?" + urllib.parse.quote(params + f"&$top={ps}&$skip={skip}", safe="=&$,()/%:@")
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    d = json.loads(r.read().decode())
                d = d if isinstance(d, list) else [d]
                out += d
                if len(d) < ps:
                    break
                skip += ps
            return out

        # Lead Source per contact (custom field def 34) + created date for first-touch tiebreak
        cf = paged("ContactCustomFields", "$filter=ContactCustomFieldDefinitionID eq 34")
        src = {int(x["ContactID"]): (x.get("ColumnValue") or "").strip()
               for x in cf if (x.get("ColumnValue") or "").strip()}
        created = {c["ContactID"]: str(c.get("CreatedDateTime") or "")
                   for c in paged("Contacts", "$select=ContactID,CreatedDateTime")}
        prop_contacts = {}
        ids = list(src)
        for i in range(0, len(ids), 25):
            filt = "ContactID in (" + ",".join(str(x) for x in ids[i:i + 25]) + ")"
            for pc in paged("PropertyContacts", f"$filter={filt}&$select=PropertyID,ContactID"):
                pid, cid = pc.get("PropertyID"), pc.get("ContactID")
                if pid and cid and int(cid) in src:
                    prop_contacts.setdefault(int(pid), set()).add(int(cid))

        def origin(billing_id, property_id):
            cands = set()
            if billing_id and int(billing_id) in src:
                cands.add(int(billing_id))
            if property_id:
                cands |= prop_contacts.get(int(property_id), set())
            if not cands:
                return None
            best = min(cands, key=lambda c: created.get(c, "9999"))
            return src[best]

        WON = "OpportunityStatusName eq 'Won'"
        sel = ("$select=WonDollars,ActualEarnedRevenue,OpportunityType,CompleteDate,"
               "OpportunityName,OpportunityNumber,OpportunityID,BillingContactID,PropertyID")

        def opp_revenue(o):
            """The honest dollar figure for one won opportunity.

            WonDollars alone is wrong in two different directions, so this
            picks per type rather than applying one rule to both.

            One-time work: WonDollars is what was AGREED, ActualEarnedRevenue
            is what was BILLED, and for irrigation they are deliberately not
            the same number -- the model is a $150 diagnosis followed by the
            repair, invoiced on the same opportunity without the agreed figure
            ever being revised. 14 of 34 paid-search diagnoses billed more than
            they were won for ($150 -> $861 on one). Across all irrigation won
            since February that is $69,070 agreed against $79,392 billed.

            Contracts: the opposite. WonDollars is the ANNUAL value and
            ActualEarnedRevenue is merely how much of the year has elapsed, so
            using "actual" would value a contract by its age. Contracts won in
            2023 show 98% earned, 2024 78%, 2025 72%, 2026 24% -- that gradient
            is the calendar, not performance.
            """
            if (o.get("OpportunityType") or "") == "Contract":
                return float(o.get("WonDollars", 0) or 0)
            # ActualEarnedRevenue is only the finished number once the job IS
            # finished. On work still in progress it is whatever has been
            # earned so far, which is the same "elapsed, not sized" trap that
            # makes it wrong for contracts. Opportunity #3250, "Spiraling
            # Junipers": won $1,100, earned $57.84, no completion date -- the
            # crew has barely started. 148 of 1,680 won work orders are open,
            # 57 of them showing less earned than won, and valuing those at
            # their partial earnings understates by $258,702.
            actual = float(o.get("ActualEarnedRevenue", 0) or 0)
            if o.get("CompleteDate") and actual > 0:
                return actual
            return float(o.get("WonDollars", 0) or 0)

        def attribute(params, want_opps=False):
            # Buckets are [count, one-time cash, annual contract value]. The
            # last two are NEVER added together: one is money received once,
            # the other recurs every year until cancelled. Summing them is what
            # let a single $30,759 maintenance agreement outweigh twelve real
            # installs and flatter whichever channel happened to win it.
            by = {}
            opp_list = []
            for o in paged("Opportunities", params):
                s = origin(o.get("BillingContactID"), o.get("PropertyID"))
                dollars = opp_revenue(o)
                is_contract = (o.get("OpportunityType") or "") == "Contract"
                b = by.setdefault(s or "(no source)", [0, 0.0, 0.0])
                b[0] += 1
                b[2 if is_contract else 1] += dollars
                if want_opps and s:
                    oid = o.get("OpportunityID")
                    opp_list.append({"name": o.get("OpportunityName") or "Unnamed opportunity",
                                     "dollars": dollars, "number": o.get("OpportunityNumber"),
                                     "is_contract": is_contract,
                                     "source_label": s,
                                     "url": f"https://cloud.youraspire.com/app/opportunities/{oid}" if oid else None})
            return by, opp_list

        week_by, week_opps = attribute(
            f"$filter={WON} and WonDate ge {start_date}T00:00:00Z and WonDate le {end_date}T23:59:59Z&{sel}",
            want_opps=True)
        grand_by, _ = attribute(
            f"$filter={WON} and WonDate ge 2026-02-19T00:00:00Z and WonDate le {end_date}T23:59:59Z"
            # CompleteDate matters here too. Without it opp_revenue can never
            # take the completion branch, so every one-time job in the
            # season-to-date figure silently falls back to its quoted amount --
            # $979,821 instead of $1,017,021 across the 342 wins since Feb 19.
            # The week-level select already carries it; this one was missed.
            "&$select=WonDollars,ActualEarnedRevenue,OpportunityType,CompleteDate,"
            "BillingContactID,PropertyID")

        cpc = [0, 0.0, 0.0]
        org = [0, 0.0, 0.0]
        oth = [0, 0.0, 0.0]
        for s, (c, d, k) in week_by.items():
            tgt = (cpc if s in _PAID_SRC else
                   org if s in _ORGANIC_SRC else
                   oth if s != "(no source)" else None)
            if tgt is None:
                continue
            tgt[0] += c
            tgt[1] += d
            tgt[2] += k
        week_opps.sort(key=lambda x: -x["dollars"])
        return {
            # *_won is ONE-TIME CASH ONLY. Annual contract value is reported
            # beside it in *_acv and deliberately never folded in -- see the
            # comment in attribute().
            "cpc_won": cpc[1], "cpc_acv": cpc[2], "cpc_count": cpc[0],
            "organic_won": org[1], "organic_acv": org[2], "organic_count": org[0],
            "other_won": oth[1], "other_acv": oth[2], "other_count": oth[0],
            "total_won": cpc[1] + org[1] + oth[1],
            "total_acv": cpc[2] + org[2] + oth[2],
            "count": cpc[0] + org[0] + oth[0],
            "won_opps": week_opps,
            "week_by_source": week_by,
            "grand_by_source": grand_by,
        }
    except Exception as e:
        print(f"Aspire revenue v2 query skipped: {e}", file=sys.stderr)
        return None


aspire_revenue = get_aspire_revenue_v2(this_week_start, this_week_end)


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
# GOOGLE OPTIMIZATION RECOMMENDATIONS
# ============================================================

# Recommendation types we know how to evaluate
RECOMMENDATION_VERDICTS = {
    "KEYWORD": "review",         # Some keywords are good, some are junk
    "SEARCH_PARTNERS_OPT_IN": "skip",
    "DISPLAY_EXPANSION_OPT_IN": "skip",
    "RESPONSIVE_SEARCH_AD": "review",
    "RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH": "review",
    "LEAD_FORM_ASSET": "skip",
    "USE_BROAD_MATCH_KEYWORD": "skip",
    "FORECASTING_CAMPAIGN_BUDGET": "review",
    "RAISE_TARGET_CPA_BID_TOO_LOW": "review",
    "TARGET_CPA_OPT_IN": "review",
    "ENHANCED_CPC_OPT_IN": "skip",
    "MAXIMIZE_CONVERSIONS_OPT_IN": "review",
    "SITELINK_ASSET": "review",
    "CALLOUT_ASSET": "review",
    "CALL_ASSET": "review",
    "UPGRADE_SMART_SHOPPING_CAMPAIGN_TO_PERFORMANCE_MAX": "skip",
    "PERFORMANCE_MAX_OPT_IN": "skip",
}

RECOMMENDATION_REASONS = {
    "SEARCH_PARTNERS_OPT_IN": "Low-quality partner traffic dilutes conversion data",
    "DISPLAY_EXPANSION_OPT_IN": "Display ads are awareness, not lead gen -- muddies conversion data",
    "LEAD_FORM_ASSET": "Bypasses website and WhatConverts tracking pipeline",
    "USE_BROAD_MATCH_KEYWORD": "Loosens targeting and increases waste spend",
    "ENHANCED_CPC_OPT_IN": "Conflicts with Maximize Conversions bidding strategy",
    "PERFORMANCE_MAX_OPT_IN": "Removes control over targeting and placements",
    "UPGRADE_SMART_SHOPPING_CAMPAIGN_TO_PERFORMANCE_MAX": "Not applicable to service business",
}

def fetch_google_recommendations():
    """Fetch optimization recommendations from Google Ads API."""
    try:
        # Build campaign ID -> name lookup
        camp_id_to_name = {}
        for row in safe_query("SELECT campaign.id, campaign.name FROM campaign WHERE campaign.status = 'ENABLED'"):
            camp_id_to_name[str(row.campaign.id)] = row.campaign.name

        query = """
            SELECT
                recommendation.type,
                recommendation.campaign
            FROM recommendation
            WHERE recommendation.dismissed = FALSE
            ORDER BY recommendation.type
        """
        recs_by_type = {}
        for row in safe_query(query):
            rec_type = row.recommendation.type_.name
            campaign = row.recommendation.campaign or ""
            camp_id = campaign.split("/")[-1] if "/" in campaign else ""
            camp_name = camp_id_to_name.get(camp_id, "")
            if rec_type not in recs_by_type:
                recs_by_type[rec_type] = {"count": 0, "campaigns": set()}
            recs_by_type[rec_type]["count"] += 1
            if camp_name:
                recs_by_type[rec_type]["campaigns"].add(camp_name)

        results = []
        for rec_type, data in sorted(recs_by_type.items(), key=lambda x: -x[1]["count"]):
            verdict = RECOMMENDATION_VERDICTS.get(rec_type, "review")
            reason = RECOMMENDATION_REASONS.get(rec_type, "")
            results.append({
                "type": rec_type,
                "count": data["count"],
                "campaigns": sorted(data["campaigns"]),
                "verdict": verdict,
                "reason": reason,
            })
        return results
    except Exception as e:
        print(f"Google recommendations fetch skipped: {e}", file=sys.stderr)
        return []

google_recs = fetch_google_recommendations()
optimization_score_query = safe_query("""
    SELECT customer.optimization_score FROM customer LIMIT 1
""")
opt_score = None
for row in optimization_score_query:
    opt_score = row.customer.optimization_score


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
    # ROI compares one-time cash against one week of spend, which is the only
    # like-for-like comparison available. Contract wins are called out
    # separately rather than folded in: a $19,028 annual agreement is not a
    # week's return, and adding it here would make a single signature look like
    # a 900% week.
    cpc_acv = aspire_revenue["cpc_acv"] if aspire_revenue else 0
    if cpc_rev > 0:
        roi = ((cpc_rev - spend) / spend * 100) if spend > 0 else 0
        lines.append(f"Ad-sourced leads closed ${cpc_rev:,.0f} in one-time revenue ({cpc_count} opp{'s' if cpc_count != 1 else ''}). ROI: {roi:+.0f}%.")
    if cpc_acv > 0:
        lines.append(f"Ads also won ${cpc_acv:,.0f} in annual contract value, which recurs yearly and is not counted in that ROI.")
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
    h('<h2>Revenue by Lead Source</h2>')
    h('<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>')
    h(f'<td width="33%" style="padding:0 4px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">Ad Spend</div>')
    h(f'<div style="font-size:26px;font-weight:700;color:#e74c3c;">${acct_this["spend"]:.0f}</div>')
    h(f'</div></td>')
    cpc_color = "#27ae60" if aspire_revenue["cpc_won"] > 0 else "#888"
    h(f'<td width="33%" style="padding:0 4px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">One-Time from Ads</div>')
    h(f'<div style="font-size:26px;font-weight:700;color:{cpc_color};">${aspire_revenue["cpc_won"]:,.0f}</div>')
    h(f'<div style="font-size:11px;color:#666;margin-top:4px;">{aspire_revenue["cpc_count"]} opp{"s" if aspire_revenue["cpc_count"] != 1 else ""}</div>')
    h(f'</div></td>')
    org_color = "#3498db" if aspire_revenue["organic_won"] > 0 else "#888"
    h(f'<td width="33%" style="padding:0 4px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;">One-Time from Organic</div>')
    h(f'<div style="font-size:26px;font-weight:700;color:{org_color};">${aspire_revenue["organic_won"]:,.0f}</div>')
    h(f'<div style="font-size:11px;color:#666;margin-top:4px;">{aspire_revenue["organic_count"]} opp{"s" if aspire_revenue["organic_count"] != 1 else ""}</div>')
    h(f'</div></td>')
    h('</tr></table>')
    if aspire_revenue["other_won"] > 0:
        h(f'<div style="text-align:center;margin-top:6px;font-size:11px;color:#666;">+ ${aspire_revenue["other_won"]:,.0f} from other sources ({aspire_revenue["other_count"]} opps)</div>')
    # Annual contract value, kept visually separate from the cash figures
    # above. A maintenance agreement recurs every year; a sod install does not.
    if aspire_revenue.get("total_acv", 0) > 0:
        h('<div style="margin-top:10px;padding:8px 10px;background:#1b1b1b;border-left:3px solid #C9700B;border-radius:4px;">')
        h('<div style="font-size:11px;color:#888;text-transform:uppercase;">Plus annual contract value won this week</div>')
        h(f'<div style="font-size:12px;color:#ddd;margin-top:3px;">'
          f'Ads ${aspire_revenue["cpc_acv"]:,.0f} &bull; '
          f'Organic ${aspire_revenue["organic_acv"]:,.0f} &bull; '
          f'Other ${aspire_revenue["other_acv"]:,.0f}</div>')
        h('<div style="font-size:10px;color:#777;margin-top:3px;">Recurring yearly, not one-time cash. Not added to the figures above.</div>')
        h('</div>')
    # Grand Total by Lead Source, condensed to one line: the full per-source
    # table duplicated the weekly cadence of this report for numbers that only
    # move meaningfully over months. Rank + total is what "where do we stand"
    # actually needs.
    grand = aspire_revenue.get("grand_by_source") or {}
    if grand:
        _ranked_srcs = sorted(
            ((s, v[1]) for s, v in grand.items() if s != "(no source)"), key=lambda x: -x[1])
        _grand_opps = sum(v[0] for v in grand.values())
        _grand_onetime = sum(v[1] for v in grand.values())
        _ads_rank = next((i + 1 for i, (s, _) in enumerate(_ranked_srcs) if s == "Google Ads"), None)
        _ads_rank_str = (f"Google Ads ranks #{_ads_rank} of {len(_ranked_srcs)} sources by one-time cash"
                          if _ads_rank else "Google Ads has no won revenue yet")
        h(f'<div style="margin-top:14px;font-size:12px;color:#888;">'
          f'Since Feb 19: <span style="color:#fff;font-weight:600;">{_grand_opps} opps, '
          f'${_grand_onetime:,.0f}</span> one-time revenue across all lead sources &bull; {_ads_rank_str}.</div>')

    # Closed This Week, filtered to paid search -- an ads report only needs its
    # own wins, plus one trivial organic line for context.
    won_opps = aspire_revenue.get("won_opps") or []
    ads_opps = [o for o in won_opps if o.get("source_label") in _PAID_SRC]
    organic_opps = [o for o in won_opps if o.get("source_label") in _ORGANIC_SRC]
    if ads_opps:
        h('<div style="margin-top:12px;">')
        h('<div style="font-size:12px;color:#888;font-weight:600;margin-bottom:6px;">Closed This Week (Google/Bing Ads)</div>')
        for opp in ads_opps:
            s = opp.get("source_label", "?")
            lcolor = _SRC_COLOR.get(s, "#aaa")
            num = f" #{opp['number']}" if opp.get("number") else ""
            if opp.get("url"):
                name_html = f'<a href="{opp["url"]}" style="color:#fff;text-decoration:underline;">{opp["name"]}{num}</a>'
            else:
                name_html = f'{opp["name"]}{num}'
            h(f'<div style="margin-bottom:4px;padding:8px 12px;background:#1a1a1a;border-radius:4px;font-size:12px;">')
            h(f'<span style="color:{lcolor};font-weight:700;">[{s}]</span> {name_html} '
              f'<span style="color:#27ae60;font-weight:600;">${opp["dollars"]:,.0f}</span></div>')
        h('</div>')
    if organic_opps:
        _organic_total = sum(o["dollars"] for o in organic_opps)
        h(f'<div style="margin-top:6px;font-size:11px;color:#666;">For comparison: {len(organic_opps)} organic '
          f'win{"s" if len(organic_opps) != 1 else ""} this week, ${_organic_total:,.0f}.</div>')
    h(f'<div style="text-align:center;margin-top:6px;font-size:11px;color:#555;">Attribution from Aspire Lead Source '
      f'(customer-origin, first-touch). Won = status Won only.</div>')
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
    h('<h2>Negative Keyword Recommendations</h2>')
    h(f'<div style="background:#2a1a1a;border:1px solid #e74c3c;border-radius:8px;padding:14px 16px;margin-bottom:16px;text-align:center;">')
    h(f'<span style="font-size:24px;font-weight:700;color:#e74c3c;">${total_waste:.0f}</span>')
    h(f'<span style="font-size:13px;color:#ccc;"> spent on {len(waste_terms)} non-converting search terms</span>')
    h(f'</div>')

    h('<table><tr><th>Search Term</th><th class="right">Clicks</th><th class="right">Spend</th><th>Campaign</th></tr>')
    for w in waste_terms[:7]:
        h(f'<tr><td style="color:#e74c3c;">{w["term"]}</td><td class="right">{w["clicks"]}</td><td class="right">${w["spend"]:.2f}</td><td style="font-size:12px;color:#888;">{w["campaign"]}</td></tr>')
    h('</table>')
    h('<div style="font-size:11px;color:#555;margin-top:8px;">Search terms with 2+ clicks and 0 conversions in the last 28 days. Review and add as negatives.</div>')
    if already_blocked_terms:
        blocked_total = sum(w["spend"] for w in already_blocked_terms)
        h(f'<div style="margin-top:10px;padding:10px 14px;background:#1a2a1a;border-radius:6px;font-size:12px;color:#27ae60;">')
        h(f'Already handled: ${blocked_total:.0f} of past-28-day waste is now blocked by existing negatives ')
        h(f'({", ".join(w["term"] for w in already_blocked_terms[:6])}). This spend happened before the negatives went live.')
        h(f'</div>')
    h('</div>')

# --- Ad copy performance: best and worst only (by impressions, the existing sort) ---
def _best_worst(assets):
    return [("Best", assets[0])] if len(assets) == 1 else [("Best", assets[0]), ("Worst", assets[-1])]

if headlines or descriptions:
    h('<div class="section">')
    h('<h2>Ad Copy Performance</h2>')
    h('<div style="font-size:12px;color:#888;margin-bottom:12px;">RSA asset performance this week -- best and worst by impressions</div>')
    if headlines:
        h('<div style="font-size:13px;color:#c8963e;font-weight:600;margin-bottom:8px;">Headlines</div>')
        h('<table><tr><th></th><th>Headline</th><th class="right">Impr</th><th class="right">Clicks</th><th class="right">CTR</th><th class="right">Conv</th></tr>')
        for label, hl in _best_worst(headlines):
            ctr_color = "#27ae60" if hl["ctr"] >= 5 else "#f39c12" if hl["ctr"] >= 3 else "#ccc"
            h(f'<tr><td style="font-size:11px;color:#888;">{label}</td>'
              f'<td style="font-weight:500;color:#fff;max-width:250px;overflow:hidden;text-overflow:ellipsis;">{hl["text"]}</td>')
            h(f'<td class="right">{hl["impressions"]:,}</td><td class="right">{hl["clicks"]}</td>')
            h(f'<td class="right"><span style="color:{ctr_color};">{hl["ctr"]:.1f}%</span></td>')
            h(f'<td class="right"><span style="color:#27ae60;font-weight:600;">{hl["conversions"]:.0f}</span></td></tr>')
        h('</table>')
    if descriptions:
        h(f'<div style="font-size:13px;color:#c8963e;font-weight:600;margin:{"16px" if headlines else "0"} 0 8px;">Descriptions</div>')
        h('<table><tr><th></th><th>Description</th><th class="right">Impr</th><th class="right">Clicks</th><th class="right">CTR</th><th class="right">Conv</th></tr>')
        for label, desc in _best_worst(descriptions):
            ctr_color = "#27ae60" if desc["ctr"] >= 5 else "#f39c12" if desc["ctr"] >= 3 else "#ccc"
            h(f'<tr><td style="font-size:11px;color:#888;">{label}</td>'
              f'<td style="font-weight:500;color:#fff;max-width:300px;overflow:hidden;text-overflow:ellipsis;font-size:12px;">{desc["text"]}</td>')
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
        h(f'<div style="font-size:13px;color:#c8963e;font-weight:600;margin:{"16px" if device_data else "0"} 0 8px;">By Time of Day (Central)</div>')
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

    # Only QS 1-6 gets a row -- QS 7-10 keywords are healthy and don't need
    # a weekly line item; they still count in the summary/distribution above.
    qs_keywords_low = [kw for kw in qs_keywords if kw["qs"] <= 6]

    h('<table><tr><th>Keyword</th><th class="right">QS</th><th class="right">Trend</th><th class="right">Exp CTR</th><th class="right">Ad Rel</th><th class="right">Land Page</th></tr>')

    for kw in qs_keywords_low:
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

    h('</div>')

# ============================================================
# SECTION: GOOGLE OPTIMIZATION RECOMMENDATIONS
# ============================================================
if google_recs:
    h('<div class="section">')
    h('<h2>Google Optimization Recommendations</h2>')
    if opt_score is not None:
        score_color = "#27ae60" if opt_score >= 90 else "#f39c12" if opt_score >= 70 else "#e74c3c"
        h(f'<div style="font-size:12px;color:#888;margin-bottom:12px;">Optimization Score: <span style="color:{score_color};font-weight:600;">{opt_score:.0f}%</span></div>')

    skip_recs = [r for r in google_recs if r["verdict"] == "skip"]
    review_recs = [r for r in google_recs if r["verdict"] == "review"]

    if review_recs:
        h('<div style="font-size:12px;color:#c8963e;font-weight:600;margin-bottom:8px;">Worth Reviewing</div>')
        for rec in review_recs:
            type_label = rec["type"].replace("_", " ").title()
            camps = ", ".join(rec["campaigns"]) if rec["campaigns"] else "Account-level"
            h(f'<div style="margin-bottom:6px;padding:8px 12px;background:#222;border-radius:6px;border-left:3px solid #c8963e;">')
            h(f'<div style="font-size:13px;color:#fff;">{type_label} <span style="color:#888;">({rec["count"]})</span></div>')
            h(f'<div style="font-size:11px;color:#aaa;margin-top:2px;">{camps}</div>')
            h(f'</div>')

    if skip_recs:
        h(f'<div style="font-size:12px;color:#888;margin-top:12px;">'
          f'{sum(r["count"] for r in skip_recs)} recommendations skipped as not aligned with current strategy.</div>')

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
    md.append(f"| One-Time Revenue Won This Week | ${aspire_revenue['total_won']:,.0f} ({aspire_revenue['count']} opportunities) |")
    md.append(f"| Annual Contract Value Won This Week | ${aspire_revenue['total_acv']:,.0f} (recurring, not added to the above) |")
    md.append(f"| One-Time from Ads | ${aspire_revenue['cpc_won']:,.0f} ({aspire_revenue['cpc_count']} opps) |")
    md.append(f"| One-Time from Organic | ${aspire_revenue['organic_won']:,.0f} ({aspire_revenue['organic_count']} opps) |")
    md.append("")
    grand = aspire_revenue.get("grand_by_source") or {}
    if grand:
        ranked_srcs = sorted(
            ((s, v[1]) for s, v in grand.items() if s != "(no source)"), key=lambda x: -x[1])
        grand_opps = sum(v[0] for v in grand.values())
        grand_onetime = sum(v[1] for v in grand.values())
        ads_rank = next((i + 1 for i, (s, _) in enumerate(ranked_srcs) if s == "Google Ads"), None)
        ads_rank_str = (f"Google Ads ranks #{ads_rank} of {len(ranked_srcs)} sources by one-time cash"
                        if ads_rank else "Google Ads has no won revenue yet")
        md.append(f"**Since Feb 19**: {grand_opps} opps, ${grand_onetime:,.0f} one-time revenue "
                  f"across all lead sources -- {ads_rank_str}.")
        md.append("")
    won_opps = aspire_revenue.get("won_opps") or []
    ads_opps = [o for o in won_opps if o.get("source_label") in _PAID_SRC]
    organic_opps = [o for o in won_opps if o.get("source_label") in _ORGANIC_SRC]
    if ads_opps:
        md.append("**Closed this week (Google/Bing Ads)**:")
        for opp in ads_opps:
            num = f" #{opp['number']}" if opp.get("number") else ""
            link = f"[{opp['name']}{num}]({opp['url']})" if opp.get("url") else f"{opp['name']}{num}"
            md.append(f"- **[{opp['source_label']}]** {link} -- ${opp['dollars']:,.0f}")
        md.append("")
    if organic_opps:
        organic_total = sum(o["dollars"] for o in organic_opps)
        md.append(f"*For comparison: {len(organic_opps)} organic win{'s' if len(organic_opps) != 1 else ''} "
                  f"this week, ${organic_total:,.0f}.*")
        md.append("")
    lt90 = aspire_revenue.get("lead_type_90d")
    if lt90:
        f90, c90 = lt90["web_form"], lt90["phone_call"]
        md.append(f"*Last 90 days won: web forms ${f90['dollars']:,.0f} ({f90['won']} opps) | "
                  f"phone calls ${c90['dollars']:,.0f} ({c90['won']} opps)*")
        md.append("")
    md.append("*Correlation only -- not direct attribution between ads and won deals*")
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

# --- Irrigation Promo Watch (promo asset "$100 off Diagnosis w/ Repair" launched 2026-07-09) ---
PROMO_LAUNCH = "2026-07-09"
PROMO_CAMPAIGN = "BH_PC_Irrigationservice"
PROMO_BASELINE_CTR = 2.06   # trailing-30d CTR at launch (see project_irrigation_promo_baseline)
PROMO_BASELINE_CVR = 3.09
_irr = camp_data.get(PROMO_CAMPAIGN, {}).get("this", {})
if _irr:
    _irr_ctr = _irr.get("ctr", 0)
    _irr_cvr = (_irr["conversions"] / _irr["clicks"] * 100) if _irr.get("clicks") else 0
    # control = other enabled campaigns (no promo), aggregated
    _c_clicks = _c_impr = 0
    for _nm, _d in camp_data.items():
        if _nm == PROMO_CAMPAIGN:
            continue
        _t = _d.get("this", {})
        _c_clicks += _t.get("clicks", 0)
        _c_impr += _t.get("impressions", 0)
    _ctrl_ctr = (_c_clicks / _c_impr * 100) if _c_impr else 0
    _ctr_call = ("above" if _irr_ctr > PROMO_BASELINE_CTR + 0.15
                 else "below" if _irr_ctr < PROMO_BASELINE_CTR - 0.15 else "flat vs")
    md.append("## Irrigation Promo Watch")
    md.append(f"*'$100 off Diagnosis w/ Repair' promo launched {PROMO_LAUNCH} on {PROMO_CAMPAIGN} only; the other campaigns are the control. "
              f"Primary signal is CTR. Low volume, so read the trend over 2-4 weeks, not one week. No per-asset conversion attribution exists.*")
    md.append("")
    md.append("| Metric | Irrigation (this wk) | Baseline (pre-promo, 30d) | Control campaigns (this wk) |")
    md.append("|--------|----------------------|---------------------------|------------------------------|")
    md.append(f"| CTR | {_irr_ctr:.2f}% | {PROMO_BASELINE_CTR:.2f}% | {_ctrl_ctr:.2f}% |")
    md.append(f"| Conv rate | {_irr_cvr:.2f}% | {PROMO_BASELINE_CVR:.2f}% | -- |")
    md.append(f"| Clicks / Conv | {_irr.get('clicks',0)} / {_irr.get('conversions',0):.0f} | -- | -- |")
    md.append(f"\n*Read: Irrigation CTR is {_ctr_call} the pre-promo baseline.*")
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
    md.append(f"## Negative Keyword Recommendations: ${total_waste:.0f} on {len(waste_terms)} non-converting terms")
    md.append(f"| Search Term | Clicks | Spend | Campaign |")
    md.append(f"|-------------|--------|-------|----------|")
    for w in waste_terms[:7]:
        md.append(f"| {w['term']} | {w['clicks']} | ${w['spend']:.2f} | {w['campaign']} |")
    md.append("")

if already_blocked_terms:
    blocked_total = sum(w["spend"] for w in already_blocked_terms)
    md.append(f"*Already handled: ${blocked_total:.0f} of past-28-day waste is now blocked by existing negatives "
              f"({', '.join(w['term'] for w in already_blocked_terms[:6])}). This spend happened before the negatives went live.*")
    md.append("")

if headlines or descriptions:
    md.append("## Ad Copy Performance")
    md.append("*Best and worst by impressions*")
    md.append("")
    if headlines:
        md.append("### Headlines")
        md.append(f"| | Headline | Impr | Clicks | CTR | Conv |")
        md.append(f"|-|----------|------|--------|-----|------|")
        for label, hl in _best_worst(headlines):
            md.append(f"| {label} | {hl['text']} | {hl['impressions']:,} | {hl['clicks']} | {hl['ctr']:.1f}% | {hl['conversions']:.0f} |")
    if descriptions:
        md.append("### Descriptions")
        md.append(f"| | Description | Impr | Clicks | CTR | Conv |")
        md.append(f"|-|-------------|------|--------|-----|------|")
        for label, desc in _best_worst(descriptions):
            md.append(f"| {label} | {desc['text']} | {desc['impressions']:,} | {desc['clicks']} | {desc['ctr']:.1f}% | {desc['conversions']:.0f} |")
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
        md.append("### By Time of Day (Central)")
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
    below5_now_md = sum(1 for kw in qs_keywords if kw["qs"] < 5)
    avg_qs_md = sum(kw["qs"] for kw in qs_keywords) / len(qs_keywords)
    md.append(f"*{below5_now_md} of {len(qs_keywords)} keywords below QS 5. Average QS: {avg_qs_md:.1f}. "
              f"Only QS 1-6 keywords are listed below; QS 7-10 keywords are healthy.*")
    md.append("")
    # Only QS 1-6 gets a row here -- same cut as the HTML report.
    qs_keywords_low_md = [kw for kw in qs_keywords if kw["qs"] <= 6]
    md.append(f"| Keyword | QS | Exp CTR | Ad Rel | Landing Page |")
    md.append(f"|---------|----|---------| -------|-------------|")
    for kw in qs_keywords_low_md:
        prior = prior_qs.get(kw["keyword"])
        trend = ""
        if prior is not None:
            diff = kw["qs"] - prior
            if diff != 0:
                trend = f" ({'+' if diff > 0 else ''}{diff})"
        md.append(f"| {kw['keyword']} | {kw['qs']}{trend} | {qs_component_short(kw['ctr'])} | {qs_component_short(kw['relevance'])} | {qs_component_short(kw['landing'])} |")
    md.append("")

md.append(f"---\n*Targets: CPA <= ${TARGET_CPA:.0f} | Impr Share >= {TARGET_IMPR_SHARE:.0f}%*")

report_text = "\n".join(md)


# ============================================================
# ANALYST COMMENTARY (Claude Fable 5)
# ============================================================

FABLE_MODEL = "claude-fable-5"

FABLE_SYSTEM_PROMPT = """You are the weekly Google Ads analyst for Black Hill Landscaping, \
a commercial landscaping and irrigation company in DFW, Texas.

Business rules you must apply:
- Black Hill does NOT offer residential lawn mowing. Residential mowing search terms are waste \
and should be negatives. Commercial mowing IS a service. Irrigation, landscaping installs, \
sprinkler repair, and drainage are core services.
- Targets: CPA at or under $80, impression share at or above 50%.
- umairmg3417@gmail.com is the sanctioned web dev team with editor access. Their changes are \
authorized; your job is quality review, not suspicion. Past mistakes from this team include \
typo'd keywords (e.g. "irrigation istallation"), broken ValueTrack URL syntax, and \
inconsistent match types. Check their changes for errors like these.
- Changes from other emails (the owner, API automation) are routine.
- The change log shows RAW TYPED INPUT, not final stored state. Keywords logged with quote \
marks in the text and UNSPECIFIED match type are normalized by the Google Ads UI into clean \
phrase-match keywords. Do not report quote marks or unspecified match types from the change \
log as live account damage. Delete-then-recreate of the same keyword IS real damage \
(performance history reset) and should be flagged.
- Phone call conversions ("Calls from ads") are DELIBERATELY set as secondary per the owner's \
decision (June 2026): bidding optimizes on web form submits only. Never recommend promoting \
call conversions to primary. Low primary conversion counts partly reflect this choice; \
account for it when judging bidding performance.
- SPEND IS FROZEN (owner's decision, June 2026): never recommend raising budgets, bids, or \
target CPA while conversion volume is low. All recommendations must be efficiency levers: \
Quality Score, landing page relevance, negatives, ad copy, cutting wasted schedule or device \
spend. If the binding constraint can only be fixed with more spend, say so plainly and stop \
there; do not turn it into a recommendation.

Writing rules:
- Plain language for a busy non-technical owner. No jargon without a one-phrase explanation.
- Never use em dashes.
- Do not restate numbers already in the report unless you are interpreting them.
- If the data is insufficient to support a conclusion, say so. Never speculate as fact.
- No markdown tables. Use short bullets and numbered lists only.

Output EXACTLY these four markdown sections and nothing else:
### The Big Picture
(3-5 sentences: the real story of the week against the 4-week trend)
### Anomalies & Watch Items
(bullets; anything unusual in spend, CPC, QS, schedule, devices; say "Nothing unusual this week." if clean)
### Web Dev Team Change Review
(bullets reviewing umairmg3417 changes for errors; say "No web dev team changes this week." if none)
### Priority Actions
(numbered, max 5, most important first; end each with "Confidence: high/medium/low")

Priority Actions rules. These are strict:
- First identify the single binding constraint this week: the one thing most limiting leads \
(examples: impression share lost to ad rank, lost to budget, low QS on the highest-spend \
keywords, weak landing page experience, bad search term waste). Action 1 must attack that \
constraint directly.
- Every action must be executable this week and name the specific campaign, keyword, ad \
group, or setting it applies to, with a concrete change (a number, a bid, a specific \
negative keyword, a specific headline swap).
- If the blocker is ad rank, do not say "consider improving quality score". Say which QS \
component is low on which high-spend keywords and the specific fix (e.g. rewrite ad group X \
headlines to include keyword Y, raise bids Z% on campaign W, fix landing page mismatch on \
URL V), and what result to expect by next week's report.
- Ban vague verbs: consider, explore, monitor, review, evaluate, keep an eye on. If an \
action cannot be stated concretely, it does not belong in the list.
- State the expected payoff of each action in plain terms (more impressions, lower CPA, \
more calls), so the owner knows why it is worth doing."""


def _collect_change_events():
    rows = safe_query(f"""
        SELECT change_event.change_date_time,
               change_event.user_email,
               change_event.change_resource_type,
               change_event.resource_change_operation,
               change_event.changed_fields,
               change_event.new_resource,
               campaign.name
        FROM change_event
        WHERE change_event.change_date_time BETWEEN '{this_week_start} 00:00:00' AND '{this_week_end} 23:59:59'
        ORDER BY change_event.change_date_time DESC
        LIMIT 500
    """)
    events = []
    for r in rows:
        ce = r.change_event
        rtype = getattr(ce.change_resource_type, "name", str(ce.change_resource_type))
        op = getattr(ce.resource_change_operation, "name", str(ce.resource_change_operation))
        detail = ""
        try:
            kw = ce.new_resource.ad_group_criterion.keyword
            if kw.text:
                detail = f' | keyword="{kw.text}" match={getattr(kw.match_type, "name", "")}'
        except Exception:
            pass
        try:
            urls = list(ce.new_resource.ad.final_urls)
            if urls:
                detail += f" | final_urls={urls[:2]}"
        except Exception:
            pass
        fields = ",".join(list(ce.changed_fields.paths)[:8])
        events.append(
            f"{ce.change_date_time} | {ce.user_email} | {op} {rtype}"
            f" | campaign={r.campaign.name or 'n/a'} | fields={fields}{detail}"
        )
    return events


def _load_prior_reports(rdir, limit=4):
    try:
        today_name = f"{now.strftime('%Y-%m-%d')}.md"
        files = sorted(
            f for f in os.listdir(rdir)
            if f.endswith(".md") and f[:4].isdigit() and f != today_name
        )[-limit:]
        chunks = []
        for fn in files:
            with open(os.path.join(rdir, fn)) as fh:
                chunks.append(f"===== PRIOR REPORT {fn} =====\n{fh.read()}")
        return "\n\n".join(chunks)
    except Exception as e:
        print(f"Prior report load warning: {e}", file=sys.stderr)
        return ""


def _anthropic_messages(model, user_prompt, api_key):
    # Fable 5 has adaptive thinking always on; max_tokens must cover thinking + output
    body = json.dumps({
        "model": model,
        "max_tokens": 16000,
        "system": FABLE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)


fable_failure_reason = None
commentary_model = None  # actual model that produced the commentary (Fable or fallback)

# Human-readable bylines for models that can produce the commentary
MODEL_LABELS = {
    "claude-fable-5": "Claude Fable 5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
}

def _model_label(model):
    return MODEL_LABELS.get(model, model or "Claude Fable 5")

def _call_fable(user_prompt):
    global fable_failure_reason, commentary_model
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("No ANTHROPIC_API_KEY configured; skipping analyst commentary.")
        return None
    for model in (FABLE_MODEL, "claude-sonnet-4-6"):
        try:
            data = _anthropic_messages(model, user_prompt, api_key)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:500]
            except Exception:
                pass
            if "credit balance" in detail.lower():
                fable_failure_reason = "API credits exhausted. Top up at console.anthropic.com (Plans & Billing) to restore the analyst commentary."
            else:
                fable_failure_reason = f"API error (HTTP {e.code})."
            print(f"Analyst commentary: {model} HTTP {e.code}: {detail}", file=sys.stderr)
            continue
        except Exception as e:
            fable_failure_reason = "API unreachable."
            print(f"Analyst commentary: {model} failed: {e}", file=sys.stderr)
            continue
        if data.get("stop_reason") == "refusal":
            print(f"Analyst commentary: {model} refused; trying fallback.", file=sys.stderr)
            continue
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
        if text:
            commentary_model = model
            if model != FABLE_MODEL:
                print(f"Analyst commentary generated by fallback model {model}.")
            return text
    print("Analyst commentary unavailable (report sent without it).", file=sys.stderr)
    return None


def _commentary_to_html(text):
    import html as _html
    import re as _re
    byline = _model_label(commentary_model)
    parts = ['<div class="section">']
    parts.append(f'<h2>Analyst Commentary <span style="font-size:11px;color:#888;font-weight:400;">{byline}</span></h2>')
    open_list = None

    def close_list():
        nonlocal open_list
        if open_list:
            parts.append(f"</{open_list}>")
            open_list = None

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            close_list()
            continue
        esc = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _html.escape(s))
        if esc.startswith("### "):
            close_list()
            parts.append(f'<div style="font-size:13px;font-weight:700;color:#f0c040;margin:14px 0 6px;">{esc[4:]}</div>')
        elif esc.startswith(("- ", "* ")):
            if open_list != "ul":
                close_list()
                parts.append('<ul style="margin:4px 0 8px 18px;padding:0;">')
                open_list = "ul"
            parts.append(f'<li style="font-size:13px;color:#ccc;line-height:1.6;margin-bottom:4px;">{esc[2:]}</li>')
        elif _re.match(r"^\d+\. ", esc):
            if open_list != "ol":
                close_list()
                parts.append('<ol style="margin:4px 0 8px 18px;padding:0;">')
                open_list = "ol"
            parts.append(f'<li style="font-size:13px;color:#ccc;line-height:1.6;margin-bottom:4px;">{esc.split(". ", 1)[1]}</li>')
        else:
            close_list()
            parts.append(f'<div style="font-size:13px;color:#ccc;line-height:1.6;margin-bottom:6px;">{esc}</div>')
    close_list()
    parts.append("</div>")
    return "\n".join(parts)


_report_dir = os.path.join(REPO_ROOT, ".claude", "reports", "marketing", "google-ads", "weekly")
change_events = _collect_change_events()
prior_reports = _load_prior_reports(_report_dir)
events_text = "\n".join(change_events) if change_events else "(no change events in the account this week)"

fable_prompt = f"""Here is this week's deterministic Google Ads report, the prior weekly reports
for trend context, and the raw account change log for the week. Write your analyst commentary.

===== THIS WEEK'S REPORT =====
{report_text}

{prior_reports}

===== ACCOUNT CHANGE LOG (past 7 days, newest first) =====
{events_text}"""

commentary = _call_fable(fable_prompt)
if commentary:
    section_md = f"## Analyst Commentary ({_model_label(commentary_model)})\n{commentary}\n"
    marker = "## Did We Move the Needle?"
    if marker in report_text:
        report_text = report_text.replace(marker, f"{section_md}\n{marker}", 1)
    else:
        report_text += f"\n{section_md}"
    html_report = html_report.replace('<div class="section">', _commentary_to_html(commentary) + '\n<div class="section">', 1)
    print(f"Analyst commentary added ({len(commentary)} chars, {len(change_events)} change events reviewed).")
elif fable_failure_reason:
    notice = f"Analyst commentary unavailable: {fable_failure_reason}"
    report_text = report_text.replace(
        "## Did We Move the Needle?", f"*{notice}*\n\n## Did We Move the Needle?", 1)
    notice_html = (f'<div style="margin-bottom:14px;padding:10px 14px;background:#2a1a1a;'
                   f'border:1px solid #e74c3c;border-radius:6px;font-size:12px;color:#e74c3c;">{notice}</div>')
    html_report = html_report.replace('<div class="section">', notice_html + '\n<div class="section">', 1)


# ============================================================
# SAVE & SEND
# ============================================================

# --dry-run builds the whole report, including every Google Ads and Aspire
# query, then stops instead of emailing. Added 2026-08-12 so the revenue
# attribution rewrite could be proved before its first scheduled send, since
# dispatching the workflow otherwise mails the real report to all recipients.
# Writes go to /tmp, not the repo's report archive or the QS-history state
# file (guarded above), so a test run leaves no trace to commit or diff.
if DRY_RUN:
    dryrun_html_path = "/tmp/weekly_report_dryrun.html"
    dryrun_md_path = "/tmp/weekly_report_dryrun.md"
    with open(dryrun_html_path, "w") as f:
        f.write(html_report)
    with open(dryrun_md_path, "w") as f:
        f.write(report_text)
    print("=" * 70)
    print("DRY RUN - report built successfully, email NOT sent, no state persisted")
    print(f"HTML written to: {dryrun_html_path}")
    print(f"Markdown written to: {dryrun_md_path}")
    print("=" * 70)
    _run.done()
    sys.exit(0)

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
    _run.done()
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
        server.sendmail(gmail_email, TO_EMAILS, msg.as_string())
    print("Email sent successfully via Gmail!")
except Exception as e:
    print(f"Email send failed: {e}")
    print("Report was saved to file but email delivery failed.")

_run.done()
