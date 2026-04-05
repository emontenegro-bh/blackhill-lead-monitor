#!/usr/bin/env python3
"""Weekly Google Ads report - runs via launchd every Sunday at 9 AM.
Pulls performance data and emails a styled HTML summary to Evelin.
Includes week-over-week comparisons and recommendations.

Scheduler: ~/Library/LaunchAgents/com.blackhill.ads-weekly-report.plist
Manual run: python3 ~/projects/scripts/ads-weekly-report.py
"""

import json, warnings, smtplib, os, sys, signal
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

# --- Global timeout: kill the process if it runs longer than 5 minutes ---
SCRIPT_TIMEOUT = 300  # seconds

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
FROM_EMAIL = "evelin@blackhilltx.com"
SENDGRID_SMTP = "smtp.sendgrid.net"
SENDGRID_PORT = 587
API_KEY_FILE = os.path.expanduser("~/.config/sendgrid-api-key")
CLOUD_MODE = bool(os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"))
TARGET_CPA = 80.0
TARGET_CTR = 3.5
TARGET_MONTHLY_BUDGET = 3500.0
TARGET_MONTHLY_CONV = 44  # $3500 / $80 CPA
TARGET_WEEKLY_CONV = 10   # ~44/4.35
TARGET_CONV_RATE = 5.0    # conservative %
TARGET_MONTHLY_IMPR = 25000  # 44 conv / 5% cvr / 3.5% CTR
TARGET_WEEKLY_IMPR = 5700    # ~25000/4.35
TARGET_IMPR_SHARE = 50.0     # % of eligible impressions we want to capture

# Historical optimization scores file (for week-over-week comparison)
OPT_SCORE_HISTORY_FILE = os.path.expanduser(
    "~/projects/.claude/reports/marketing/google-ads/weekly/optimization-score-history.json"
)

# --- Load credentials ---
if CLOUD_MODE:
    ads_config = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        "customer_id": os.environ["GOOGLE_ADS_CUSTOMER_ID"],
    }
else:
    with open(os.path.expanduser("~/.config/google-ads/config.json")) as f:
        ads_config = json.load(f)

credentials = {
    "developer_token": ads_config["developer_token"],
    "client_id": ads_config["client_id"],
    "client_secret": ads_config["client_secret"],
    "refresh_token": ads_config["refresh_token"],
    "login_customer_id": ads_config["login_customer_id"],
    "use_proto_plus": True,
}

credentials["timeout"] = 60  # 60-second timeout per API call
client = GoogleAdsClient.load_from_dict(credentials)
ga_service = client.get_service("GoogleAdsService")
customer_id = ads_config["customer_id"]

# --- Date ranges ---
now = datetime.now()
this_week_end = now.strftime("%Y-%m-%d")
this_week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
prev_week_end = (now - timedelta(days=7)).strftime("%Y-%m-%d")
prev_week_start = (now - timedelta(days=13)).strftime("%Y-%m-%d")
today_fmt = now.strftime("%B %d, %Y")
week_ago_fmt = (now - timedelta(days=6)).strftime("%b %d")

# --- Helpers ---
def delta_pct(current, previous):
    """Return numeric percentage change."""
    if previous == 0:
        return None
    return ((current - previous) / previous) * 100

def delta_str(current, previous):
    """Return a formatted delta string like +12.3% or -5.2%."""
    pct = delta_pct(current, previous)
    if pct is None:
        return "NEW" if current > 0 else "--"
    arrow = "+" if pct >= 0 else ""
    return f"{arrow}{pct:.1f}%"


def load_opt_score_history():
    """Load historical optimization scores from JSON file."""
    if os.path.exists(OPT_SCORE_HISTORY_FILE):
        try:
            with open(OPT_SCORE_HISTORY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_opt_score_history(history, current_scores):
    """Save current optimization scores to history keyed by date.

    Keeps the last 12 weeks of data to avoid unbounded growth.
    current_scores: dict of {campaign_name: score_float}
    """
    today_key = datetime.now().strftime("%Y-%m-%d")
    history[today_key] = current_scores

    # Prune to last 12 entries (weeks)
    if len(history) > 12:
        sorted_dates = sorted(history.keys())
        for old_date in sorted_dates[:-12]:
            del history[old_date]

    os.makedirs(os.path.dirname(OPT_SCORE_HISTORY_FILE), exist_ok=True)
    with open(OPT_SCORE_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def get_prior_week_scores(history):
    """Return the most recent prior week's optimization scores."""
    today_key = datetime.now().strftime("%Y-%m-%d")
    sorted_dates = sorted(d for d in history.keys() if d != today_key)
    if sorted_dates:
        return history[sorted_dates[-1]]
    return {}


def opt_score_trend_html(current, previous):
    """Return HTML for optimization score with trend arrow.

    current/previous are floats 0-1 (or None).
    """
    if current is None:
        return '<span style="color:#666;">N/A</span>'
    pct = current * 100
    if previous is not None:
        diff = (current - previous) * 100  # percentage point change
        if diff > 0.5:
            arrow = '&#9650;'
            color = '#27ae60'
            trend = f' <span style="color:{color};font-size:11px;">{arrow}+{diff:.0f}pp</span>'
        elif diff < -0.5:
            arrow = '&#9660;'
            color = '#e74c3c'
            trend = f' <span style="color:{color};font-size:11px;">{arrow}{diff:.0f}pp</span>'
        else:
            trend = ' <span style="color:#888;font-size:11px;">&#9644;</span>'
    else:
        trend = ' <span style="color:#888;font-size:11px;">NEW</span>'

    # Color the score itself based on value
    if pct >= 80:
        score_color = '#27ae60'
    elif pct >= 50:
        score_color = '#f39c12'
    else:
        score_color = '#e74c3c'
    return f'<span style="color:{score_color};font-weight:600;">{pct:.0f}%</span>{trend}'


def opt_score_trend_text(current, previous):
    """Return plain text for optimization score with trend.

    current/previous are floats 0-1 (or None).
    """
    if current is None:
        return "N/A"
    pct = current * 100
    if previous is not None:
        diff = (current - previous) * 100
        if diff > 0.5:
            return f"{pct:.0f}% (+{diff:.0f}pp)"
        elif diff < -0.5:
            return f"{pct:.0f}% ({diff:.0f}pp)"
        else:
            return f"{pct:.0f}% (unchanged)"
    return f"{pct:.0f}% (new)"


# ============================================================
# DATA COLLECTION
# ============================================================
recs = []
def rec(s):
    recs.append(s)

# --- Account summary ---
acct_this = {}
acct_prev = {}

for label, date_start, date_end, store in [
    ("this", this_week_start, this_week_end, acct_this),
    ("prev", prev_week_start, prev_week_end, acct_prev),
]:
    query = f"""
        SELECT
            metrics.cost_micros, metrics.impressions, metrics.clicks,
            metrics.ctr, metrics.average_cpc, metrics.conversions,
            metrics.conversions_from_interactions_rate, metrics.cost_per_conversion
        FROM customer
        WHERE segments.date BETWEEN '{date_start}' AND '{date_end}'
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            m = row.metrics
            store["spend"] = m.cost_micros / 1_000_000
            store["impressions"] = m.impressions
            store["clicks"] = m.clicks
            store["ctr"] = m.ctr * 100
            store["cpc"] = m.average_cpc / 1_000_000
            store["conversions"] = m.conversions
            store["conv_rate"] = m.conversions_from_interactions_rate * 100
            store["cpa"] = m.cost_per_conversion / 1_000_000 if m.conversions > 0 else 0
    except (GoogleAdsException, Exception):
        pass

# Generate account-level recs
if acct_this:
    t = acct_this
    p = acct_prev if acct_prev else {}
    monthly_pace = t["spend"] * 4.35
    monthly_conv_pace = t["conversions"] * 4.35
    monthly_impr_pace = t["impressions"] * 4.35
    if t["cpa"] > TARGET_CPA:
        rec(f"CPA is ${t['cpa']:.2f} (target: ${TARGET_CPA:.0f}) — Review high-spend zero-conversion keywords. Consider pausing or adding as negatives.")
    if t["ctr"] < TARGET_CTR:
        rec(f"CTR is {t['ctr']:.2f}% (target: {TARGET_CTR}%) — Test new ad copy or tighten keyword match types.")
    if t["cpc"] > 10:
        rec(f"Avg CPC is ${t['cpc']:.2f} — Well above industry average ($2.94). Check Quality Scores.")
    if monthly_pace > TARGET_MONTHLY_BUDGET * 1.1:
        rec(f"Monthly pace ${monthly_pace:.0f} exceeds ${TARGET_MONTHLY_BUDGET:.0f} budget — Consider reducing daily budgets.")
    if t["conversions"] < TARGET_WEEKLY_CONV:
        rec(f"Conversions: {t['conversions']:.0f} this week (target: {TARGET_WEEKLY_CONV}) — On pace for {monthly_conv_pace:.0f}/mo vs {TARGET_MONTHLY_CONV} goal.")
    if t["impressions"] < TARGET_WEEKLY_IMPR:
        rec(f"Impressions: {t['impressions']:,} this week (target: {TARGET_WEEKLY_IMPR:,}) — On pace for {monthly_impr_pace:,.0f}/mo vs {TARGET_MONTHLY_IMPR:,} goal.")
    if p and t["conversions"] < p.get("conversions", 0):
        rec(f"Conversions dropped from {p['conversions']:.0f} to {t['conversions']:.0f} week-over-week — Investigate landing page or keyword shifts.")

# --- Campaign performance ---
camp_this = {}
camp_prev = {}

for label, date_start, date_end, store in [
    ("this", this_week_start, this_week_end, camp_this),
    ("prev", prev_week_start, prev_week_end, camp_prev),
]:
    query = f"""
        SELECT
            campaign.name, campaign.status,
            campaign.optimization_score,
            metrics.cost_micros, metrics.clicks, metrics.impressions,
            metrics.ctr, metrics.conversions, metrics.cost_per_conversion,
            metrics.search_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{date_start}' AND '{date_end}'
          AND campaign.status = 'ENABLED'
          AND metrics.impressions > 0
        ORDER BY metrics.cost_micros DESC
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            c = row.campaign
            m = row.metrics
            spend = m.cost_micros / 1_000_000
            cpa = m.cost_per_conversion / 1_000_000 if m.conversions > 0 else 0
            # optimization_score is a float 0-1 or None if not available
            opt_score = None
            try:
                if c.optimization_score is not None:
                    opt_score = c.optimization_score
            except AttributeError:
                pass
            store[c.name] = {
                "spend": spend, "clicks": m.clicks, "impressions": m.impressions,
                "ctr": m.ctr * 100, "conversions": m.conversions, "cpa": cpa,
                "impression_share": m.search_impression_share,
                "optimization_score": opt_score,
            }
    except (GoogleAdsException, Exception):
        pass

# Compute weighted average impression share (weighted by impressions)
avg_impr_share_this = 0
avg_impr_share_prev = 0
total_impr_this = sum(c.get("impressions", 0) for c in camp_this.values())
total_impr_prev = sum(c.get("impressions", 0) for c in camp_prev.values())
if total_impr_this > 0:
    avg_impr_share_this = sum(
        c.get("impression_share", 0) * c.get("impressions", 0)
        for c in camp_this.values()
    ) / total_impr_this * 100  # convert to percentage
if total_impr_prev > 0:
    avg_impr_share_prev = sum(
        c.get("impression_share", 0) * c.get("impressions", 0)
        for c in camp_prev.values()
    ) / total_impr_prev * 100

# --- Optimization score history ---
opt_score_history = load_opt_score_history()
prior_opt_scores = get_prior_week_scores(opt_score_history)

# Build current optimization scores dict for saving
current_opt_scores = {}
for name, t in camp_this.items():
    if t.get("optimization_score") is not None:
        current_opt_scores[name] = t["optimization_score"]

# Save current scores (will be the "prior" next week)
save_opt_score_history(opt_score_history, current_opt_scores)

# --- Campaign recommendations ---
for name, t in camp_this.items():
    if t["conversions"] == 0 and t["spend"] > 50:
        rec(f"{name}: Spent ${t['spend']:.2f} with 0 conversions. Review keywords and landing page.")
    elif t["conversions"] > 0 and t["cpa"] <= TARGET_CPA * 0.75 and t.get("impression_share", 0) < 0.5:
        rec(f"{name}: CPA ${t['cpa']:.2f} is well under target — increase budget for more impression share ({t.get('impression_share',0)*100:.0f}% currently).")

# Optimization score recommendations
for name, t in camp_this.items():
    opt = t.get("optimization_score")
    if opt is not None and opt < 0.7:
        prior = prior_opt_scores.get(name)
        trend = ""
        if prior is not None:
            diff = (opt - prior) * 100
            if diff < -5:
                trend = f" (dropped {abs(diff):.0f}pp from last week)"
            elif diff > 5:
                trend = f" (improved {diff:.0f}pp from last week)"
        rec(f"{name}: Optimization score {opt*100:.0f}%{trend} — Review Google's optimization recommendations tab for quick wins.")

if avg_impr_share_this < TARGET_IMPR_SHARE:
    rec(f"Impression share is {avg_impr_share_this:.1f}% (target: {TARGET_IMPR_SHARE:.0f}%) — Increase budgets, improve Quality Scores, or raise bids to win more auctions.")

# --- A/B test ads ---
WATCH_ADS = {
    "796752844531": "Brand Voice (NEW)",
    "721527726874": "Systemized (CONTROL)",
}

ad_this = {}
ad_prev = {}

for label, date_start, date_end, store in [
    ("this", this_week_start, this_week_end, ad_this),
    ("prev", prev_week_start, prev_week_end, ad_prev),
]:
    query = f"""
        SELECT
            ad_group_ad.ad.id,
            metrics.cost_micros, metrics.impressions, metrics.clicks,
            metrics.ctr, metrics.conversions,
            metrics.conversions_from_interactions_rate, metrics.cost_per_conversion
        FROM ad_group_ad
        WHERE segments.date BETWEEN '{date_start}' AND '{date_end}'
          AND campaign.id = 21906405585
          AND ad_group.id = 170482611077
          AND ad_group_ad.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            ad_id = str(row.ad_group_ad.ad.id)
            m = row.metrics
            spend = m.cost_micros / 1_000_000
            cpa = m.cost_per_conversion / 1_000_000 if m.conversions > 0 else 0
            store[ad_id] = {
                "spend": spend, "clicks": m.clicks, "ctr": m.ctr * 100,
                "conversions": m.conversions,
                "conv_rate": m.conversions_from_interactions_rate * 100,
                "cpa": cpa,
            }
    except (GoogleAdsException, Exception):
        pass

brand = ad_this.get("796752844531", {})
control = ad_this.get("721527726874", {})
if brand.get("clicks", 0) >= 30 and control.get("clicks", 0) >= 30:
    if brand.get("conv_rate", 0) > control.get("conv_rate", 0) * 1.2:
        rec("A/B Test: Brand Voice outperforming Control by 20%+. Consider pausing Control.")
    elif control.get("conv_rate", 0) > brand.get("conv_rate", 0) * 1.2:
        rec("A/B Test: Control outperforming Brand Voice by 20%+. Review Brand Voice copy.")
else:
    total_clicks = brand.get("clicks", 0) + control.get("clicks", 0)
    rec(f"A/B Test: {total_clicks} total clicks — need ~60 per ad for significance. Keep running.")

# --- Ad group performance ---
ag_this = {}
ag_prev = {}

for label, date_start, date_end, store in [
    ("this", this_week_start, this_week_end, ag_this),
    ("prev", prev_week_start, prev_week_end, ag_prev),
]:
    query = f"""
        SELECT
            ad_group.name, campaign.name,
            metrics.cost_micros, metrics.clicks, metrics.impressions,
            metrics.ctr, metrics.conversions, metrics.cost_per_conversion
        FROM ad_group
        WHERE segments.date BETWEEN '{date_start}' AND '{date_end}'
          AND campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND metrics.impressions > 0
        ORDER BY metrics.cost_micros DESC
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            ag = row.ad_group
            m = row.metrics
            spend = m.cost_micros / 1_000_000
            cpa = m.cost_per_conversion / 1_000_000 if m.conversions > 0 else 0
            store[ag.name] = {
                "spend": spend, "clicks": m.clicks, "ctr": m.ctr * 100,
                "conversions": m.conversions, "cpa": cpa,
            }
    except (GoogleAdsException, Exception):
        pass

for name, t in ag_this.items():
    if t["conversions"] == 0 and t["spend"] > 30:
        rec(f"{name}: ${t['spend']:.2f} spent, 0 conversions. Consider pausing or reviewing landing page.")

# --- Form abandonment ---
device_form = {}
query = f"""
    SELECT
        segments.device,
        segments.conversion_action_name,
        metrics.all_conversions
    FROM campaign
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND segments.conversion_action_name IN ('Lead Form Start', 'Lead Form Submit')
      AND metrics.all_conversions > 0
"""
try:
    response = ga_service.search(customer_id=customer_id, query=query)
    for row in response:
        device = row.segments.device.name
        action = row.segments.conversion_action_name
        convs = row.metrics.all_conversions
        if device not in device_form:
            device_form[device] = {"starts": 0, "submits": 0}
        if action == "Lead Form Start":
            device_form[device]["starts"] += convs
        elif action == "Lead Form Submit":
            device_form[device]["submits"] += convs
except (GoogleAdsException, Exception):
    pass

total_starts = sum(d["starts"] for d in device_form.values())
total_submits = sum(d["submits"] for d in device_form.values())
if total_starts > 0:
    total_abandon = (total_starts - total_submits) / total_starts * 100
    if total_abandon > 60:
        rec(f"Form abandonment is {total_abandon:.0f}% — still high. Simplify the form or add tap-to-call.")
    elif total_abandon < 40:
        rec(f"Form abandonment dropped to {total_abandon:.0f}% — optimizations are working.")

# --- Alerts: high-spend zero-conversion keywords ---
kw_alerts = []
query = f"""
    SELECT
        ad_group_criterion.keyword.text, ad_group.name,
        metrics.cost_micros, metrics.conversions
    FROM keyword_view
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND campaign.status = 'ENABLED'
      AND ad_group_criterion.status = 'ENABLED'
      AND metrics.cost_micros > 50000000
      AND metrics.conversions = 0
    ORDER BY metrics.cost_micros DESC
"""
try:
    response = ga_service.search(customer_id=customer_id, query=query)
    for row in response:
        spend = row.metrics.cost_micros / 1_000_000
        kw = row.ad_group_criterion.keyword.text
        kw_alerts.append({"keyword": kw, "spend": spend})
        rec(f'Keyword "{kw}" spent ${spend:.2f} with 0 conversions — consider adding as negative.')
except (GoogleAdsException, Exception):
    pass

# --- Negative keyword recommendations ---
neg_recs = []
query = f"""
    SELECT
        search_term_view.search_term, campaign.name,
        metrics.cost_micros, metrics.clicks, metrics.impressions,
        metrics.conversions
    FROM search_term_view
    WHERE segments.date BETWEEN '{this_week_start}' AND '{this_week_end}'
      AND campaign.status = 'ENABLED'
      AND metrics.clicks >= 2
      AND metrics.conversions = 0
    ORDER BY metrics.cost_micros DESC
"""
try:
    response = ga_service.search(customer_id=customer_id, query=query)
    for row in response:
        term = row.search_term_view.search_term
        spend = row.metrics.cost_micros / 1_000_000
        clicks = row.metrics.clicks
        camp = row.campaign.name
        neg_recs.append({"term": term, "spend": spend, "clicks": clicks, "campaign": camp})
except (GoogleAdsException, Exception):
    pass

if neg_recs:
    total_waste = sum(n["spend"] for n in neg_recs)
    if total_waste > 50:
        rec(f"${total_waste:.2f} spent on {len(neg_recs)} non-converting search terms — review and add negatives.")


# ============================================================
# HTML EMAIL BUILDER
# ============================================================

def change_html(current, previous, inverse=False):
    """Return colored HTML span for week-over-week change."""
    pct = delta_pct(current, previous)
    if pct is None:
        label = "NEW" if current > 0 else "--"
        return f'<span style="color:#888;">{label}</span>'
    arrow = "&#9650;" if pct >= 0 else "&#9660;"
    if inverse:
        color = "#e74c3c" if pct > 0 else "#27ae60" if pct < 0 else "#888"
    else:
        color = "#27ae60" if pct > 0 else "#e74c3c" if pct < 0 else "#888"
    return f'<span style="color:{color};font-weight:600;">{arrow} {abs(pct):.1f}%</span>'

def status_dot(ok):
    """Green or amber dot."""
    color = "#27ae60" if ok else "#f39c12"
    return f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};"></span>'

def bar_html(value, target, max_width=120):
    """Inline progress bar toward target."""
    pct = min(value / target * 100, 100) if target > 0 else 0
    color = "#27ae60" if pct >= 100 else "#f39c12" if pct >= 70 else "#e74c3c"
    filled = int(max_width * pct / 100)
    return (
        f'<div style="display:inline-block;width:{max_width}px;height:8px;background:#2a2a2a;border-radius:4px;overflow:hidden;vertical-align:middle;">'
        f'<div style="width:{filled}px;height:100%;background:{color};border-radius:4px;"></div>'
        f'</div>'
        f' <span style="font-size:11px;color:#aaa;">{pct:.0f}%</span>'
    )

# --- Styles ---
STYLES = """
body { margin:0; padding:0; background:#111; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color:#e0e0e0; }
.wrap { max-width:680px; margin:0 auto; background:#1a1a1a; }
.header { background:linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%); padding:28px 32px 20px; border-bottom:2px solid #c8963e; }
.header h1 { margin:0 0 4px; font-size:20px; color:#fff; font-weight:700; letter-spacing:0.5px; }
.header .period { font-size:13px; color:#c8963e; font-weight:500; }
.section { padding:24px 32px; border-bottom:1px solid #2a2a2a; }
.section h2 { margin:0 0 16px; font-size:15px; color:#c8963e; text-transform:uppercase; letter-spacing:1px; font-weight:600; }
.kpi-row { display:flex; gap:12px; flex-wrap:wrap; }
.kpi { flex:1; min-width:140px; background:#222; border-radius:8px; padding:16px; text-align:center; border:1px solid #333; }
.kpi .label { font-size:11px; color:#888; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
.kpi .value { font-size:22px; font-weight:700; color:#fff; }
.kpi .change { font-size:12px; margin-top:4px; }
.kpi .target { font-size:10px; color:#666; margin-top:4px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; padding:10px 12px; background:#222; color:#c8963e; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; border-bottom:2px solid #333; }
td { padding:10px 12px; border-bottom:1px solid #2a2a2a; color:#ccc; }
tr:hover td { background:#222; }
.right { text-align:right; }
.alert-item { background:#2a1a1a; border-left:3px solid #e74c3c; padding:10px 14px; margin-bottom:8px; border-radius:0 6px 6px 0; font-size:13px; }
.alert-item .kw { color:#e74c3c; font-weight:600; }
.rec-item { background:#1a2a1a; border-left:3px solid #c8963e; padding:10px 14px; margin-bottom:8px; border-radius:0 6px 6px 0; font-size:13px; color:#ccc; }
.rec-num { display:inline-block; width:20px; height:20px; background:#c8963e; color:#1a1a1a; border-radius:50%; text-align:center; line-height:20px; font-size:11px; font-weight:700; margin-right:8px; }
.ok-banner { background:#1a2a1a; border:1px solid #27ae60; border-radius:8px; padding:16px; text-align:center; color:#27ae60; font-weight:600; font-size:14px; }
.footer { padding:20px 32px; text-align:center; font-size:11px; color:#555; }
.footer .targets { color:#888; margin-bottom:4px; }
"""

html_parts = []
h = html_parts.append

h(f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>{STYLES}</style></head><body>')
h('<div class="wrap">')

# --- Header ---
h(f'<div class="header">')
h(f'<h1>Weekly Google Ads Report</h1>')
h(f'<div class="period">{week_ago_fmt} &mdash; {today_fmt}</div>')
h(f'</div>')

# --- Active Test Notices (auto-expires after 2026-03-16) ---
from datetime import date
if date.today() <= date(2026, 3, 16):
    h('<div style="background:#2a2a1a;border:1px solid #c8963e;border-radius:8px;padding:16px 20px;margin:16px 32px 0;">')
    h('<div style="font-size:13px;color:#c8963e;font-weight:700;margin-bottom:6px;">ACTIVE A/B TESTS</div>')
    h('<div style="font-size:13px;color:#ccc;margin-bottom:8px;"><strong>Irrigation:</strong> 6 new RSA ads deployed Mar 2. Testing diagnostic/process copy vs previous ads across Drainage, Repair, and Install ad groups.</div>')
    h('<div style="font-size:13px;color:#ccc;"><strong>LeadGen:</strong> 2 new process-copy RSA ads deployed Mar 2 in Landscape &amp; Sod and Rock Install ad groups. Testing against existing ads.</div>')
    h('<div style="font-size:12px;color:#c8963e;margin-top:8px;font-weight:600;">Do not pause or edit test ads until Mar 16.</div>')
    h('</div>')

# --- KPI Cards ---
if acct_this:
    t = acct_this
    p = acct_prev if acct_prev else {}
    monthly_pace = t["spend"] * 4.35
    monthly_conv_pace = t["conversions"] * 4.35

    h('<div class="section">')
    h('<h2>Account Overview</h2>')

    # Row 1: Spend, Conversions, CPA, CTR
    h('<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:12px;"><tr>')
    kpis = [
        ("Spend", f"${t['spend']:.2f}", change_html(t['spend'], p.get('spend', 0)), f"Pace: ${monthly_pace:.0f}/mo", None),
        ("Conversions", f"{t['conversions']:.0f}", change_html(t['conversions'], p.get('conversions', 0)), f"Target: {TARGET_WEEKLY_CONV}/wk", t['conversions'] >= TARGET_WEEKLY_CONV),
        ("CPA", f"${t['cpa']:.2f}" if t['cpa'] > 0 else "N/A", change_html(t['cpa'], p.get('cpa', 0), inverse=True) if t['cpa'] > 0 else "", f"Target: ${TARGET_CPA:.0f}", t['cpa'] <= TARGET_CPA if t['cpa'] > 0 else None),
        ("CTR", f"{t['ctr']:.2f}%", change_html(t['ctr'], p.get('ctr', 0)), f"Target: {TARGET_CTR}%", t['ctr'] >= TARGET_CTR),
    ]
    for label, value, change, target, ok in kpis:
        dot = ""
        if ok is not None:
            dot = f' {status_dot(ok)}'
        h(f'<td width="25%" style="padding:0 6px;"><div style="background:#222;border-radius:8px;padding:16px;text-align:center;border:1px solid #333;">')
        h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">{label}</div>')
        h(f'<div style="font-size:22px;font-weight:700;color:#fff;">{value}{dot}</div>')
        h(f'<div style="font-size:12px;margin-top:4px;">{change}</div>')
        h(f'<div style="font-size:10px;color:#666;margin-top:4px;">{target}</div>')
        h(f'</div></td>')
    h('</tr></table>')

    # Row 2: Secondary metrics
    h('<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:0;"><tr>')
    is_ok = avg_impr_share_this >= TARGET_IMPR_SHARE
    sec_kpis = [
        ("Clicks", f"{t['clicks']:,}", change_html(t['clicks'], p.get('clicks', 0))),
        ("Avg CPC", f"${t['cpc']:.2f}", change_html(t['cpc'], p.get('cpc', 0), inverse=True)),
        ("Conv Rate", f"{t['conv_rate']:.2f}%", change_html(t['conv_rate'], p.get('conv_rate', 0))),
        ("Impr Share", f"{avg_impr_share_this:.1f}%{' ' + status_dot(is_ok) if avg_impr_share_this > 0 else ''}", change_html(avg_impr_share_this, avg_impr_share_prev) if avg_impr_share_prev > 0 else ""),
    ]
    for label, value, change in sec_kpis:
        h(f'<td width="25%" style="padding:0 6px;"><div style="background:#1e1e1e;border-radius:6px;padding:10px;text-align:center;border:1px solid #2a2a2a;">')
        h(f'<div style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">{label}</div>')
        h(f'<div style="font-size:16px;font-weight:600;color:#ddd;">{value}</div>')
        h(f'<div style="font-size:11px;margin-top:2px;">{change}</div>')
        h(f'</div></td>')
    h('</tr></table>')

    # Monthly pace bars
    h('<div style="margin-top:16px;background:#222;border-radius:8px;padding:14px 16px;border:1px solid #333;">')
    h('<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;">Monthly Pace</div>')
    h(f'<div style="margin-bottom:6px;"><span style="display:inline-block;width:100px;font-size:12px;color:#aaa;">Budget</span> {bar_html(monthly_pace, TARGET_MONTHLY_BUDGET)} <span style="font-size:11px;color:#aaa;">${monthly_pace:,.0f} / ${TARGET_MONTHLY_BUDGET:,.0f}</span></div>')
    h(f'<div style="margin-bottom:6px;"><span style="display:inline-block;width:100px;font-size:12px;color:#aaa;">Conversions</span> {bar_html(monthly_conv_pace, TARGET_MONTHLY_CONV)} <span style="font-size:11px;color:#aaa;">{monthly_conv_pace:.0f} / {TARGET_MONTHLY_CONV}</span></div>')
    pace_impr = t["impressions"] * 4.35
    h(f'<div style="margin-bottom:6px;"><span style="display:inline-block;width:100px;font-size:12px;color:#aaa;">Impressions</span> {bar_html(pace_impr, TARGET_MONTHLY_IMPR)} <span style="font-size:11px;color:#aaa;">{pace_impr:,.0f} / {TARGET_MONTHLY_IMPR:,}</span></div>')
    h(f'<div><span style="display:inline-block;width:100px;font-size:12px;color:#aaa;">Impr Share</span> {bar_html(avg_impr_share_this, TARGET_IMPR_SHARE)} <span style="font-size:11px;color:#aaa;">{avg_impr_share_this:.1f}% / {TARGET_IMPR_SHARE:.0f}%</span></div>')
    h('</div>')

    h('</div>')

# --- Campaign Performance ---
if camp_this:
    h('<div class="section">')
    h('<h2>Campaign Performance</h2>')
    h('<table><tr><th>Campaign</th><th class="right">Spend</th><th class="right">vs Last Wk</th><th class="right">Conv</th><th class="right">CPA</th><th class="right">CTR</th><th class="right">Impr Share</th><th class="right">Opt Score</th><th style="text-align:center;">Status</th></tr>')
    for name, t in camp_this.items():
        p = camp_prev.get(name, {})
        cpa_str = f"${t['cpa']:.2f}" if t['conversions'] > 0 else '<span style="color:#666;">N/A</span>'
        spend_chg = change_html(t["spend"], p.get("spend", 0))
        camp_is = t.get("impression_share", 0) * 100
        is_color = "#27ae60" if camp_is >= TARGET_IMPR_SHARE else "#f39c12" if camp_is >= TARGET_IMPR_SHARE * 0.7 else "#e74c3c"
        is_str = f'<span style="color:{is_color};font-weight:600;">{camp_is:.0f}%</span>' if camp_is > 0 else '<span style="color:#666;">N/A</span>'
        # Optimization score with week-over-week trend
        opt_score_html = opt_score_trend_html(
            t.get("optimization_score"),
            prior_opt_scores.get(name)
        )
        if t["conversions"] > 0 and t["cpa"] <= TARGET_CPA:
            status = '<span style="color:#27ae60;">On Track</span>'
        elif t["conversions"] > 0:
            status = '<span style="color:#f39c12;">Watch</span>'
        else:
            status = '<span style="color:#e74c3c;">No Conv</span>'
        h(f'<tr><td style="font-weight:500;color:#fff;">{name}</td><td class="right">${t["spend"]:.2f}</td><td class="right">{spend_chg}</td><td class="right">{t["conversions"]:.0f}</td><td class="right">{cpa_str}</td><td class="right">{t["ctr"]:.2f}%</td><td class="right">{is_str}</td><td class="right">{opt_score_html}</td><td style="text-align:center;">{status}</td></tr>')
    h('</table>')
    h('</div>')

# --- Ad Strength ---
ad_strength_counts = {"EXCELLENT": 0, "GOOD": 0, "AVERAGE": 0, "POOR": 0}
try:
    query = """
        SELECT ad_group_ad.ad_strength
        FROM ad_group_ad
        WHERE campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND ad_group_ad.status = 'ENABLED'
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    for row in response:
        s = row.ad_group_ad.ad_strength.name
        if s in ad_strength_counts:
            ad_strength_counts[s] += 1
except (GoogleAdsException, Exception):
    pass

total_ads = sum(ad_strength_counts.values())
h('<div class="section">')
h('<h2>Ad Strength</h2>')
h('<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:0;"><tr>')
for label, count in ad_strength_counts.items():
    if label == "EXCELLENT":
        color = "#27ae60"
    elif label == "GOOD":
        color = "#c8963e"
    elif label == "AVERAGE":
        color = "#f39c12"
    else:
        color = "#e74c3c"
    h(f'<td width="25%" style="padding:0 6px;"><div style="background:#222;border-radius:8px;padding:14px;text-align:center;border:1px solid #333;">')
    h(f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">{label}</div>')
    h(f'<div style="font-size:28px;font-weight:700;color:{color};">{count}</div>')
    h(f'</div></td>')
h('</tr></table>')
if ad_strength_counts["POOR"] > 0:
    rec(f"Ad strength: {ad_strength_counts['POOR']} ads rated POOR. Rewrite headlines for more variety and add {{KeyWord:}} insertion.")
if ad_strength_counts["AVERAGE"] > 0:
    rec(f"Ad strength: {ad_strength_counts['AVERAGE']} ads rated AVERAGE. Add more headlines (15 per ad) and diversify themes.")
h('</div>')

# --- A/B Test ---
h('<div class="section">')
h('<h2>Ad Copy A/B Test</h2>')
h('<table><tr><th>Ad</th><th class="right">Spend</th><th class="right">Clicks</th><th class="right">CTR</th><th class="right">Conv</th><th class="right">Conv Rate</th><th class="right">CPA</th></tr>')
for ad_id, ad_label in WATCH_ADS.items():
    t = ad_this.get(ad_id, {"spend": 0, "clicks": 0, "ctr": 0, "conversions": 0, "conv_rate": 0, "cpa": 0})
    p = ad_prev.get(ad_id, {})
    cpa_str = f"${t['cpa']:.2f}" if t["conversions"] > 0 else '<span style="color:#666;">N/A</span>'
    h(f'<tr><td style="font-weight:500;color:#fff;">{ad_label}</td><td class="right">${t["spend"]:.2f}</td><td class="right">{t["clicks"]}</td><td class="right">{t["ctr"]:.2f}%</td><td class="right">{t["conversions"]:.0f}</td><td class="right">{t["conv_rate"]:.2f}%</td><td class="right">{cpa_str}</td></tr>')
h('</table>')
h('</div>')

# --- Ad Group Performance ---
if ag_this:
    h('<div class="section">')
    h('<h2>Ad Group Performance</h2>')
    h('<table><tr><th>Ad Group</th><th class="right">Spend</th><th class="right">vs Last Wk</th><th class="right">Conv</th><th class="right">CPA</th><th class="right">CTR</th></tr>')
    for name, t in ag_this.items():
        p = ag_prev.get(name, {})
        cpa_str = f"${t['cpa']:.2f}" if t['conversions'] > 0 else '<span style="color:#666;">N/A</span>'
        spend_chg = change_html(t["spend"], p.get("spend", 0))
        h(f'<tr><td style="font-weight:500;color:#fff;">{name}</td><td class="right">${t["spend"]:.2f}</td><td class="right">{spend_chg}</td><td class="right">{t["conversions"]:.0f}</td><td class="right">{cpa_str}</td><td class="right">{t["ctr"]:.2f}%</td></tr>')
    h('</table>')
    h('</div>')

# --- Form Abandonment ---
if device_form:
    h('<div class="section">')
    h('<h2>Form Abandonment</h2>')
    h('<table><tr><th>Device</th><th class="right">Starts</th><th class="right">Submits</th><th class="right">Lost</th><th class="right">Abandon %</th></tr>')
    for device in sorted(device_form.keys()):
        d = device_form[device]
        if d["starts"] > 0:
            abandon = (d["starts"] - d["submits"]) / d["starts"] * 100
            lost = d["starts"] - d["submits"]
            abn_color = "#e74c3c" if abandon > 60 else "#f39c12" if abandon > 40 else "#27ae60"
            h(f'<tr><td>{device}</td><td class="right">{d["starts"]:.0f}</td><td class="right">{d["submits"]:.0f}</td><td class="right">{lost:.0f}</td><td class="right"><span style="color:{abn_color};font-weight:600;">{abandon:.1f}%</span></td></tr>')
    if total_starts > 0:
        total_abandon_pct = (total_starts - total_submits) / total_starts * 100
        total_lost = total_starts - total_submits
        h(f'<tr style="border-top:2px solid #333;"><td style="font-weight:700;color:#fff;">TOTAL</td><td class="right" style="font-weight:700;color:#fff;">{total_starts:.0f}</td><td class="right" style="font-weight:700;color:#fff;">{total_submits:.0f}</td><td class="right" style="font-weight:700;color:#fff;">{total_lost:.0f}</td><td class="right" style="font-weight:700;color:#fff;">{total_abandon_pct:.1f}%</td></tr>')
    h('</table>')
    h('<div style="font-size:11px;color:#555;margin-top:8px;">Baseline (pre-optimization): Mobile 78%, Desktop 51%, Overall 69%</div>')
    h('</div>')
elif total_starts == 0:
    h('<div class="section">')
    h('<h2>Form Abandonment</h2>')
    h('<div style="color:#666;font-size:13px;">No form activity this week.</div>')
    h('</div>')

# --- Alerts ---
h('<div class="section">')
h('<h2>Alerts</h2>')
if kw_alerts:
    for a in kw_alerts:
        h(f'<div class="alert-item"><span class="kw">"{a["keyword"]}"</span> spent <strong>${a["spend"]:.2f}</strong> with 0 conversions</div>')
else:
    h('<div class="ok-banner">No alerts. All systems nominal.</div>')
h('</div>')

# --- Negative Keyword Recommendations ---
if neg_recs:
    h('<div class="section">')
    h('<h2>Negative Keyword Recommendations</h2>')
    h('<div style="font-size:12px;color:#888;margin-bottom:12px;">Search terms that triggered ads but didn\'t convert</div>')
    h('<table><tr><th>Search Term</th><th class="right">Clicks</th><th class="right">Spend</th><th>Campaign</th></tr>')
    for n in neg_recs[:15]:
        h(f'<tr><td style="color:#e74c3c;">{n["term"]}</td><td class="right">{n["clicks"]}</td><td class="right">${n["spend"]:.2f}</td><td style="font-size:12px;color:#888;">{n["campaign"]}</td></tr>')
    total_waste = sum(n["spend"] for n in neg_recs)
    h(f'</table>')
    h(f'<div style="margin-top:12px;background:#2a1a1a;border-radius:6px;padding:10px 14px;font-size:13px;color:#e74c3c;font-weight:600;">Total wasted: ${total_waste:.2f} across {len(neg_recs)} search terms</div>')
    h('</div>')

# --- Recommendations ---
h('<div class="section">')
h('<h2>Recommendations</h2>')
if recs:
    for i, r in enumerate(recs, 1):
        h(f'<div class="rec-item"><span class="rec-num">{i}</span>{r}</div>')
else:
    h('<div class="ok-banner">No action items this week. Performance is on track.</div>')
h('</div>')

# --- Footer ---
h('<div class="footer">')
h(f'<div class="targets">Targets: CPA &le; ${TARGET_CPA:.0f} &nbsp;|&nbsp; CTR &ge; {TARGET_CTR}% &nbsp;|&nbsp; Impr Share &ge; {TARGET_IMPR_SHARE:.0f}% &nbsp;|&nbsp; Conv &ge; {TARGET_MONTHLY_CONV}/mo &nbsp;|&nbsp; Budget &le; ${TARGET_MONTHLY_BUDGET:,.0f}/mo</div>')
h(f'<div>Black Hill Landscaping &bull; Weekly Google Ads Report</div>')
h('</div>')

h('</div></body></html>')

html_report = "\n".join(html_parts)


# ============================================================
# MARKDOWN REPORT (for file archive)
# ============================================================
md = []
md.append(f"# Black Hill Landscaping - Weekly Google Ads Report")
md.append(f"**Period**: {week_ago_fmt} - {today_fmt}\n")

if date.today() <= date(2026, 3, 16):
    md.append("> **ACTIVE A/B TESTS** — (1) Irrigation: 6 new RSA ads deployed Mar 2, testing diagnostic/process copy. (2) LeadGen: 2 new process-copy RSA ads in Landscape & Sod and Rock Install. Do not pause or edit test ads until Mar 16.\n")

if acct_this:
    t = acct_this
    p = acct_prev if acct_prev else {}
    monthly_pace = t["spend"] * 4.35
    md.append("## Account Summary")
    md.append(f"| Metric | This Week | Last Week | Change |")
    md.append(f"|--------|-----------|-----------|--------|")
    md.append(f"| Spend | ${t['spend']:.2f} | ${p.get('spend',0):.2f} | {delta_str(t['spend'], p.get('spend',0))} |")
    md.append(f"| Clicks | {t['clicks']:,} | {p.get('clicks',0):,} | {delta_str(t['clicks'], p.get('clicks',0))} |")
    md.append(f"| CTR | {t['ctr']:.2f}% | {p.get('ctr',0):.2f}% | {delta_str(t['ctr'], p.get('ctr',0))} |")
    md.append(f"| CPC | ${t['cpc']:.2f} | ${p.get('cpc',0):.2f} | {delta_str(t['cpc'], p.get('cpc',0))} |")
    md.append(f"| Conversions | {t['conversions']:.0f} | {p.get('conversions',0):.0f} | {delta_str(t['conversions'], p.get('conversions',0))} |")
    md.append(f"| Conv Rate | {t['conv_rate']:.2f}% | {p.get('conv_rate',0):.2f}% | {delta_str(t['conv_rate'], p.get('conv_rate',0))} |")
    cpa_str = f"${t['cpa']:.2f}" if t['cpa'] > 0 else "N/A"
    md.append(f"| CPA | {cpa_str} | ${p.get('cpa',0):.2f} | {delta_str(t['cpa'], p.get('cpa',0))} |")
    md.append("")

if camp_this:
    md.append("## Campaign Performance")
    md.append(f"| Campaign | Spend | Conv | CPA | CTR | Opt Score | Status |")
    md.append(f"|----------|-------|------|-----|-----|-----------|--------|")
    for name, t in camp_this.items():
        cpa_str = f"${t['cpa']:.2f}" if t['conversions'] > 0 else "N/A"
        opt_txt = opt_score_trend_text(
            t.get("optimization_score"),
            prior_opt_scores.get(name)
        )
        status = "OK" if (t["conversions"] > 0 and t["cpa"] <= TARGET_CPA) else ("Watch" if t["conversions"] > 0 else "No Conv")
        md.append(f"| {name} | ${t['spend']:.2f} | {t['conversions']:.0f} | {cpa_str} | {t['ctr']:.2f}% | {opt_txt} | {status} |")
    md.append("")

md.append("## Ad Strength")
md.append(f"| Excellent | Good | Average | Poor |")
md.append(f"|-----------|------|---------|------|")
md.append(f"| {ad_strength_counts['EXCELLENT']} | {ad_strength_counts['GOOD']} | {ad_strength_counts['AVERAGE']} | {ad_strength_counts['POOR']} |")
md.append("")

if recs:
    md.append("## Recommendations")
    for i, r in enumerate(recs, 1):
        md.append(f"{i}. {r}")
    md.append("")

md.append(f"---\n*Targets: CPA <= ${TARGET_CPA:.0f} | CTR >= {TARGET_CTR}% | Impr Share >= {TARGET_IMPR_SHARE:.0f}% | Conv >= {TARGET_MONTHLY_CONV}/mo | Budget <= ${TARGET_MONTHLY_BUDGET:,.0f}/mo*")

report_text = "\n".join(md)


# ============================================================
# SAVE & SEND
# ============================================================

# Save markdown to file
report_dir = os.path.expanduser("~/projects/.claude/reports/marketing/google-ads/weekly")
os.makedirs(report_dir, exist_ok=True)
report_file = os.path.join(report_dir, f"{datetime.now().strftime('%Y-%m-%d')}.md")
with open(report_file, "w") as f:
    f.write(report_text)
print(f"Report saved: {report_file}")

# Send email
api_key = os.environ.get("SENDGRID_API_KEY", "")
if not api_key:
    if not os.path.exists(API_KEY_FILE):
        print(f"\nNo SendGrid API key found at {API_KEY_FILE}")
        print("Report saved to file but email not sent.")
        sys.exit(0)
    with open(API_KEY_FILE) as f:
        api_key = f.read().strip()

msg = MIMEMultipart("alternative")
msg["Subject"] = f"Weekly Google Ads Report - {today_fmt}"
msg["From"] = FROM_EMAIL
msg["To"] = TO_EMAIL

# Plain text fallback
msg.attach(MIMEText(report_text, "plain"))
# HTML version (preferred by email clients)
msg.attach(MIMEText(html_report, "html"))

try:
    with smtplib.SMTP(SENDGRID_SMTP, SENDGRID_PORT) as server:
        server.starttls()
        server.login("apikey", api_key)
        server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())
    print("Email sent successfully!")
except Exception as e:
    print(f"Email failed: {e}")
    print("Report was still saved to file.")
