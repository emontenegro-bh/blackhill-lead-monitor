#!/usr/bin/env python3
"""Weekly Microsoft Advertising (Bing Ads) report for Black Hill Landscaping.

Lean counterpart to ads-weekly-report.py. Six sections, no quality-score
tracker, no device/timing breakdown, no AI commentary:

  1. Did We Move the Needle?      week vs prior week vs 4-week average
  2. Where's the Money Going?     campaign breakdown
  3. Converting Search Terms      what actually produced contacts
  4. Wasted Spend                 non-converting terms, negative candidates
  5. Top Keywords by Spend
  6. What to Do This Week         generated actions

Two things this report does that the Microsoft UI does not:

  * It counts phone clicks alongside form submissions. The account's
    "Conversions" column only includes the Lead Form Submission goal, so the
    UI's CPA is inflated. See CONTACT_GOALS below.
  * It forces ReportTimeZone to Central. The account's reporting time zone is
    set to GMT-02:00 Mid-Atlantic, which shifts every daily figure.

Dual-credential, matching the repo convention: env vars in CI, and
~/.config/bing-ads/config.json locally.

RUN THIS IN CI ONLY. Microsoft rotates the refresh token on every redemption,
so a local run invalidates the copy held in the BING_ADS_REFRESH_TOKEN secret
and the next scheduled run fails. Use `--dry-run` locally only when you are
prepared to re-set that secret afterwards.

    python3 scripts/bing-weekly-report.py [--dry-run]
"""
import json, os, sys, smtplib, tempfile, signal
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

from bingads.authorization import AuthorizationData, OAuthWebAuthCodeGrant
from bingads.service_client import ServiceClient
from bingads.v13.reporting import ReportingServiceManager, ReportingDownloadParameters


# --- Global timeout: kill the process if it runs longer than 12 minutes ---
def _timeout_handler(signum, frame):
    raise SystemExit("Timed out after 12 minutes.")


signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(720)

# --- Config ---
TO_EMAILS = ["evelin@blackhilltx.com"]
TO_EMAIL = ", ".join(TO_EMAILS)
TARGET_CPA = 80.0          # upper bound of the $50-80 CPL goal
WASTE_THRESHOLD = 15.0     # a non-converting term must burn this much to be listed

# Goals that represent a real contact attempt. "Form Start" is deliberately
# excluded: starting a form is intent, not a lead.
CONTACT_GOALS = {"Lead Form Submission", "phone click", "Email Click"}

# Never recommend negating our own brands. Mean Green is the company's former
# name, not a competitor.
BRAND_SAFE = ["black hill", "blackhill", "mean green", "meangreen"]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
REPORT_DIR = os.path.join(REPO_ROOT, ".claude", "reports", "marketing", "bing-ads", "weekly")

# --- Load credentials ---
if os.environ.get("BING_ADS_DEVELOPER_TOKEN"):
    cfg = {
        "developer_token": os.environ["BING_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["BING_ADS_CLIENT_ID"],
        "client_secret": os.environ["BING_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["BING_ADS_REFRESH_TOKEN"],
        "customer_id": os.environ["BING_ADS_CUSTOMER_ID"],
        "account_id": os.environ["BING_ADS_ACCOUNT_ID"],
        "redirect_uri": os.environ.get("BING_ADS_REDIRECT_URI", "http://localhost:8400/"),
    }
else:
    with open(os.path.expanduser("~/.config/bing-ads/config.json")) as f:
        cfg = json.load(f)

if not cfg.get("refresh_token"):
    sys.exit(
        "No refresh_token in the Bing Ads config. Run scripts/get-bing-refresh-token.py "
        "as a Microsoft identity that is a Super Admin on account "
        f"{cfg.get('account_id')}, then re-run this report."
    )

authentication = OAuthWebAuthCodeGrant(
    client_id=cfg["client_id"],
    client_secret=cfg["client_secret"],
    redirection_uri=cfg["redirect_uri"],
)
authentication.request_oauth_tokens_by_refresh_token(cfg["refresh_token"])

authorization_data = AuthorizationData(
    account_id=int(cfg["account_id"]),
    customer_id=int(cfg["customer_id"]),
    developer_token=cfg["developer_token"],
    authentication=authentication,
)

reporting_service = ServiceClient(
    service="ReportingService", version=13,
    authorization_data=authorization_data, environment="production",
)
reporting_manager = ReportingServiceManager(
    authorization_data=authorization_data,
    poll_interval_in_milliseconds=5000, environment="production",
)

# --- Date ranges ---
now = datetime.now()
this_start, this_end = now - timedelta(days=6), now
prev_start, prev_end = now - timedelta(days=13), now - timedelta(days=7)
four_start, four_end = now - timedelta(days=27), now
today_fmt = now.strftime("%B %d, %Y")
week_ago_fmt = this_start.strftime("%b %d")


# --- Reporting helpers ---
def _date(d):
    obj = reporting_service.factory.create("Date")
    obj.Day, obj.Month, obj.Year = d.day, d.month, d.year
    return obj


def _time(start, end):
    t = reporting_service.factory.create("ReportTime")
    t.CustomDateRangeStart = _date(start)
    t.CustomDateRangeEnd = _date(end)
    t.PredefinedTime = None
    # The account is set to GMT-02:00 Mid-Atlantic; force Central so the day
    # boundaries line up with Fort Worth.
    t.ReportTimeZone = "CentralTimeUSCanada"
    return t


# Each report type accepts a specific scope shape; AccountPerformance rejects
# anything with Campaigns/AdGroups on it ("Invalid client data").
SCOPE_FOR = {
    "AccountPerformance": "AccountReportScope",
    "CampaignPerformance": "AccountThroughCampaignReportScope",
}


def _scope(request, kind):
    scope_name = SCOPE_FOR.get(kind, "AccountThroughAdGroupReportScope")
    scope = reporting_service.factory.create(scope_name)
    scope.AccountIds = {"long": [authorization_data.account_id]}
    if scope_name != "AccountReportScope":
        scope.Campaigns = None
    if scope_name == "AccountThroughAdGroupReportScope":
        scope.AdGroups = None
    request.Scope = scope


def build_request(kind, columns, start, end, aggregation="Summary"):
    req = reporting_service.factory.create(f"{kind}ReportRequest")
    req.Format = "Csv"
    req.ReportName = f"BH {kind}"
    req.ReturnOnlyCompleteData = False
    req.Aggregation = aggregation
    req.Time = _time(start, end)
    _scope(req, kind)
    cols = reporting_service.factory.create(f"ArrayOf{kind}ReportColumn")
    getattr(cols, f"{kind}ReportColumn").append(columns)
    req.Columns = cols
    return req


def run_report(req):
    """Submit a report request and return a list of dicts. Never raises."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            params = ReportingDownloadParameters(
                report_request=req,
                result_file_directory=tmp,
                result_file_name="report.csv",
                overwrite_result_file=True,
                timeout_in_milliseconds=300000,
            )
            container = reporting_manager.download_report(params)
            if container is None:
                return []
            rows = []
            for rec in container.report_records:
                rows.append({c: rec.value(c) for c in container.report_columns})
            container.close()
            return rows
    except Exception as e:
        print(f"Report warning ({req.ReportName}): {e}", file=sys.stderr)
        return []


def num(v):
    """Parse a Microsoft report cell into a float. Handles '$1,221.89', '2.21%', '--'."""
    if v is None:
        return 0.0
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "--", "N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ============================================================
# DATA COLLECTION
# ============================================================
ACCOUNT_COLS = ["Impressions", "Clicks", "Spend", "Conversions"]
CAMPAIGN_COLS = ["CampaignName", "CampaignStatus", "Impressions", "Clicks", "Ctr",
                 "AverageCpc", "Spend", "Conversions"]
QUERY_COLS = ["SearchQuery", "CampaignName", "Impressions", "Clicks", "Spend", "Conversions"]
KEYWORD_COLS = ["Keyword", "CampaignName", "Impressions", "Clicks", "Spend",
                "AverageCpc", "Conversions"]
GOAL_COLS = ["Goal", "CampaignName", "AllConversions", "AllRevenue"]


def account_totals(start, end):
    """Account totals summed from the campaign report.

    AccountPerformanceReport rejects our request shape ("Invalid client data"),
    and deriving from campaigns guarantees section 1 reconciles with section 2.
    """
    rows = run_report(build_request("CampaignPerformance", CAMPAIGN_COLS, start, end))
    t = {"impressions": 0.0, "clicks": 0.0, "spend": 0.0, "conversions": 0.0}
    for r in rows:
        t["impressions"] += num(r.get("Impressions"))
        t["clicks"] += num(r.get("Clicks"))
        t["spend"] += num(r.get("Spend"))
        t["conversions"] += num(r.get("Conversions"))
    t["ctr"] = (t["clicks"] / t["impressions"] * 100) if t["impressions"] else 0.0
    t["cpc"] = (t["spend"] / t["clicks"]) if t["clicks"] else 0.0
    return t


def goal_totals(start, end):
    """Per-goal conversions. This is what the Conversions column leaves out."""
    rows = run_report(build_request("GoalsAndFunnels", GOAL_COLS, start, end))
    goals = {}
    for r in rows:
        name = (r.get("Goal") or "").strip()
        goals[name] = goals.get(name, 0.0) + num(r.get("AllConversions"))
    return goals


this_week = account_totals(this_start, this_end)
prev_week = account_totals(prev_start, prev_end)
four_week = account_totals(four_start, four_end)
for k in ("impressions", "clicks", "spend", "conversions"):
    four_week[k] = four_week[k] / 4.0

goals_this = goal_totals(this_start, this_end)
goals_prev = goal_totals(prev_start, prev_end)


def contacts(goals):
    return sum(v for k, v in goals.items() if k in CONTACT_GOALS)


contacts_this, contacts_prev = contacts(goals_this), contacts(goals_prev)
cost_per_contact = (this_week["spend"] / contacts_this) if contacts_this else 0.0

campaigns = [
    r for r in run_report(build_request("CampaignPerformance", CAMPAIGN_COLS, this_start, this_end))
    if num(r.get("Impressions")) > 0 or num(r.get("Spend")) > 0
]
campaigns.sort(key=lambda r: num(r.get("Spend")), reverse=True)

queries = run_report(build_request("SearchQueryPerformance", QUERY_COLS, four_start, four_end))
converting = sorted([q for q in queries if num(q.get("Conversions")) > 0],
                    key=lambda q: num(q.get("Conversions")), reverse=True)[:15]


def brand_safe(term):
    t = (term or "").lower()
    return not any(b in t for b in BRAND_SAFE)


waste = sorted(
    [q for q in queries
     if num(q.get("Conversions")) == 0
     and num(q.get("Spend")) >= WASTE_THRESHOLD
     and brand_safe(q.get("SearchQuery"))],
    key=lambda q: num(q.get("Spend")), reverse=True)[:15]
waste_total = sum(num(q.get("Spend")) for q in waste)

keywords = sorted(run_report(build_request("KeywordPerformance", KEYWORD_COLS, this_start, this_end)),
                  key=lambda r: num(r.get("Spend")), reverse=True)[:12]


# ============================================================
# SECTION 6: RECOMMENDATIONS
# ============================================================
def build_actions():
    out = []

    # Uncounted phone clicks -> bidding is aimed at the wrong goal.
    uncounted = {k: v for k, v in goals_this.items()
                 if k in CONTACT_GOALS and k != "Lead Form Submission" and v > 0}
    if uncounted:
        detail = ", ".join(f"{int(v)} {k}" for k, v in sorted(uncounted.items()))
        out.append(
            f"Conversion tracking is undercounting. {detail} recorded this week but excluded "
            f"from the Conversions column, so Max Conversions bidding cannot see them. "
            f"Reported CPA is inflated as a result.")

    # Budget-starved winners vs expensive losers.
    best = worst = None
    for c in campaigns:
        conv, spend = num(c.get("Conversions")), num(c.get("Spend"))
        if spend < 25:
            continue
        cpa = spend / conv if conv else None
        if cpa is not None and (best is None or cpa < best[1]):
            best = (c.get("CampaignName"), cpa, spend)
        if (cpa is None or cpa > TARGET_CPA) and (worst is None or spend > worst[2]):
            worst = (c.get("CampaignName"), cpa, spend)
    if best and worst and best[0] != worst[0]:
        worst_cpa = f"${worst[1]:,.2f} CPA" if worst[1] else "no conversions"
        out.append(
            f"Shift budget: {best[0]} is converting at ${best[1]:,.2f} CPA while "
            f"{worst[0]} spent ${worst[2]:,.2f} at {worst_cpa}.")

    if waste:
        out.append(
            f"${waste_total:,.2f} went to {len(waste)} non-converting search terms. "
            f"Review the list below and add negatives.")

    if this_week["cpc"] > 5.0:
        out.append(
            f"Average CPC is ${this_week['cpc']:,.2f}, well above the ~$2.94 home-services "
            f"benchmark. Check match types and bid caps on the highest-CPC keywords.")

    if contacts_this and cost_per_contact > TARGET_CPA:
        out.append(
            f"Cost per contact is ${cost_per_contact:,.2f}, above the ${TARGET_CPA:,.0f} target.")

    return out or ["No urgent actions this week."]


actions = build_actions()


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
.right { text-align:right; }
.footer { padding:20px 32px; text-align:center; font-size:11px; color:#555; }
"""

h_parts, m_parts = [], []
h, m = h_parts.append, m_parts.append


def delta(cur, prev, inverse=False):
    if not prev:
        return '<span style="color:#888;">--</span>'
    pct = (cur - prev) / prev * 100
    arrow = "&#9650;" if pct >= 0 else "&#9660;"
    good = (pct <= 0) if inverse else (pct >= 0)
    return f'<span style="color:{"#27ae60" if good else "#e74c3c"};">{arrow} {abs(pct):.0f}%</span>'


h(f'<!DOCTYPE html><html><head><meta charset="utf-8">'
  f'<meta name="viewport" content="width=device-width,initial-scale=1">'
  f'<style>{STYLES}</style></head><body><div class="wrap">')
h(f'<div class="header"><h1>Weekly Bing Ads Report</h1>')
h(f'<div class="period">{week_ago_fmt} &mdash; {today_fmt}</div></div>')
m(f"# Black Hill Landscaping - Weekly Bing Ads Report\n\n{week_ago_fmt} - {today_fmt}\n")

# --- 1. Did We Move the Needle? ---
h('<div class="section"><h2>Did We Move the Needle?</h2>')
h('<table><tr><th>Metric</th><th class="right">This Week</th>'
  '<th class="right">Last Week</th><th class="right">4-Wk Avg</th><th class="right">Change</th></tr>')
m("\n## Did We Move the Needle?\n")
m("| Metric | This Week | Last Week | 4-Wk Avg |")
m("|---|---|---|---|")

rows = [
    ("Spend", f"${this_week['spend']:,.2f}", f"${prev_week['spend']:,.2f}",
     f"${four_week['spend']:,.2f}", delta(this_week["spend"], prev_week["spend"], inverse=True)),
    ("Clicks", f"{this_week['clicks']:,.0f}", f"{prev_week['clicks']:,.0f}",
     f"{four_week['clicks']:,.0f}", delta(this_week["clicks"], prev_week["clicks"])),
    ("Impressions", f"{this_week['impressions']:,.0f}", f"{prev_week['impressions']:,.0f}",
     f"{four_week['impressions']:,.0f}", delta(this_week["impressions"], prev_week["impressions"])),
    ("CTR", f"{this_week['ctr']:.2f}%", f"{prev_week['ctr']:.2f}%",
     f"{four_week['ctr']:.2f}%", delta(this_week["ctr"], prev_week["ctr"])),
    ("Avg CPC", f"${this_week['cpc']:,.2f}", f"${prev_week['cpc']:,.2f}",
     f"${four_week['cpc']:,.2f}", delta(this_week["cpc"], prev_week["cpc"], inverse=True)),
    ("Form submissions", f"{this_week['conversions']:,.0f}", f"{prev_week['conversions']:,.0f}",
     f"{four_week['conversions']:,.1f}", delta(this_week["conversions"], prev_week["conversions"])),
    ("All contacts", f"{contacts_this:,.0f}", f"{contacts_prev:,.0f}", "--",
     delta(contacts_this, contacts_prev)),
    ("Cost per contact", f"${cost_per_contact:,.2f}" if contacts_this else "--", "--", "--", ""),
]
for label, a, b, c, d in rows:
    h(f'<tr><td>{label}</td><td class="right">{a}</td><td class="right">{b}</td>'
      f'<td class="right">{c}</td><td class="right">{d}</td></tr>')
    m(f"| {label} | {a} | {b} | {c} |")
h("</table>")

if goals_this:
    breakdown = ", ".join(f"{k}: {int(v)}" for k, v in sorted(goals_this.items()) if v)
    h(f'<div style="margin-top:12px;padding:10px 14px;background:#1a2332;border-left:3px solid #3498db;'
      f'border-radius:4px;font-size:12px;color:#9cf;">Goals this week &mdash; {breakdown}</div>')
    m(f"\nGoals this week: {breakdown}\n")
h("</div>")

# --- 2. Where's the Money Going? ---
h('<div class="section"><h2>Where\'s the Money Going?</h2>')
m("\n## Where's the Money Going?\n")
if campaigns:
    h('<table><tr><th>Campaign</th><th class="right">Spend</th><th class="right">Clicks</th>'
      '<th class="right">CTR</th><th class="right">CPC</th><th class="right">Conv</th>'
      '<th class="right">CPA</th></tr>')
    m("| Campaign | Spend | Clicks | CTR | CPC | Conv | CPA |")
    m("|---|---|---|---|---|---|---|")
    for c in campaigns:
        spend, conv = num(c.get("Spend")), num(c.get("Conversions"))
        cpa = f"${spend / conv:,.2f}" if conv else "--"
        cpa_color = "#27ae60" if conv and spend / conv <= TARGET_CPA else "#e74c3c"
        name = c.get("CampaignName") or "(unnamed)"
        status = (c.get("CampaignStatus") or "").strip()
        label = f"{name} <span style=\"color:#888;font-size:11px;\">{status}</span>" if status.lower() == "paused" else name
        h(f'<tr><td>{label}</td><td class="right">${spend:,.2f}</td>'
          f'<td class="right">{num(c.get("Clicks")):,.0f}</td>'
          f'<td class="right">{num(c.get("Ctr")):.2f}%</td>'
          f'<td class="right">${num(c.get("AverageCpc")):,.2f}</td>'
          f'<td class="right">{conv:,.0f}</td>'
          f'<td class="right"><span style="color:{cpa_color};font-weight:600;">{cpa}</span></td></tr>')
        m(f"| {name} {status} | ${spend:,.2f} | {num(c.get('Clicks')):,.0f} | "
          f"{num(c.get('Ctr')):.2f}% | ${num(c.get('AverageCpc')):,.2f} | {conv:,.0f} | {cpa} |")
    h("</table>")
else:
    h('<div style="color:#888;font-size:13px;">No campaign activity this week.</div>')
    m("No campaign activity this week.")
h("</div>")

# --- 3. Converting Search Terms ---
h('<div class="section"><h2>Converting Search Terms</h2>')
m("\n## Converting Search Terms\n")
if converting:
    h('<table><tr><th>Search Term</th><th class="right">Conv</th>'
      '<th class="right">Spend</th><th class="right">CPA</th></tr>')
    m("| Search Term | Conv | Spend | CPA |")
    m("|---|---|---|---|")
    for q in converting:
        conv, spend = num(q.get("Conversions")), num(q.get("Spend"))
        cpa = f"${spend / conv:,.2f}" if conv else "--"
        term = q.get("SearchQuery") or ""
        h(f'<tr><td>{term}</td><td class="right">{conv:,.0f}</td>'
          f'<td class="right">${spend:,.2f}</td><td class="right">{cpa}</td></tr>')
        m(f"| {term} | {conv:,.0f} | ${spend:,.2f} | {cpa} |")
    h("</table>")
    h('<div style="margin-top:10px;font-size:11px;color:#888;">28-day window.</div>')
else:
    h('<div style="color:#888;font-size:13px;">No converting search terms in the last 28 days.</div>')
    m("No converting search terms in the last 28 days.")
h("</div>")

# --- 4. Wasted Spend ---
h(f'<div class="section"><h2>Wasted Spend</h2>')
m("\n## Wasted Spend\n")
if waste:
    h(f'<div style="margin-bottom:12px;font-size:13px;color:#e74c3c;font-weight:600;">'
      f'${waste_total:,.2f} on {len(waste)} non-converting terms (28 days)</div>')
    h('<table><tr><th>Search Term</th><th class="right">Spend</th>'
      '<th class="right">Clicks</th><th class="right">Campaign</th></tr>')
    m(f"${waste_total:,.2f} on {len(waste)} non-converting terms (28 days)\n")
    m("| Search Term | Spend | Clicks | Campaign |")
    m("|---|---|---|---|")
    for q in waste:
        term = q.get("SearchQuery") or ""
        h(f'<tr style="background:#1a1212;"><td>{term}</td>'
          f'<td class="right">${num(q.get("Spend")):,.2f}</td>'
          f'<td class="right">{num(q.get("Clicks")):,.0f}</td>'
          f'<td class="right" style="font-size:11px;color:#888;">{q.get("CampaignName") or ""}</td></tr>')
        m(f"| {term} | ${num(q.get('Spend')):,.2f} | {num(q.get('Clicks')):,.0f} | {q.get('CampaignName') or ''} |")
    h("</table>")
    h('<div style="margin-top:10px;font-size:11px;color:#888;">'
      'Own-brand terms (Black Hill, Mean Green) are excluded from this list.</div>')
else:
    h('<div style="color:#27ae60;font-size:13px;">No significant wasted spend this period.</div>')
    m("No significant wasted spend this period.")
h("</div>")

# --- 5. Top Keywords by Spend ---
h('<div class="section"><h2>Top Keywords by Spend</h2>')
m("\n## Top Keywords by Spend\n")
if keywords:
    h('<table><tr><th>Keyword</th><th class="right">Spend</th><th class="right">Clicks</th>'
      '<th class="right">CPC</th><th class="right">Conv</th></tr>')
    m("| Keyword | Spend | Clicks | CPC | Conv |")
    m("|---|---|---|---|---|")
    for k in keywords:
        conv = num(k.get("Conversions"))
        kw = k.get("Keyword") or ""
        row_bg = "" if conv else ' style="background:#1a1212;"'
        h(f'<tr{row_bg}><td>{kw}</td><td class="right">${num(k.get("Spend")):,.2f}</td>'
          f'<td class="right">{num(k.get("Clicks")):,.0f}</td>'
          f'<td class="right">${num(k.get("AverageCpc")):,.2f}</td>'
          f'<td class="right">{conv:,.0f}</td></tr>')
        m(f"| {kw} | ${num(k.get('Spend')):,.2f} | {num(k.get('Clicks')):,.0f} | "
          f"${num(k.get('AverageCpc')):,.2f} | {conv:,.0f} |")
    h("</table>")
else:
    h('<div style="color:#888;font-size:13px;">No keyword data this week.</div>')
    m("No keyword data this week.")
h("</div>")

# --- 6. What to Do This Week ---
h('<div class="section"><h2>What to Do This Week</h2>')
m("\n## What to Do This Week\n")
for i, a in enumerate(actions, 1):
    h(f'<div style="margin-bottom:8px;padding:10px 14px;background:#1a1a1a;'
      f'border-left:3px solid #c8963e;border-radius:4px;font-size:13px;">{i}. {a}</div>')
    m(f"{i}. {a}")
h("</div>")

h(f'<div class="footer">Black Hill Landscaping &middot; Microsoft Advertising account '
  f'{cfg["account_id"]}<br>Reporting time zone forced to Central.</div>')
h("</div></body></html>")

html_report = "\n".join(h_parts)
report_text = "\n".join(m_parts)

# ============================================================
# SAVE & SEND
# ============================================================
os.makedirs(REPORT_DIR, exist_ok=True)
report_file = os.path.join(REPORT_DIR, f"{now.strftime('%Y-%m-%d')}.md")
with open(report_file, "w") as f:
    f.write(report_text)
print(f"Report saved: {report_file}")

if "--dry-run" in sys.argv:
    print("=" * 70)
    print("DRY RUN - report built successfully, email NOT sent")
    print("=" * 70)
    print(report_text)
    sys.exit(0)

gmail_email = os.environ.get("GMAIL_EMAIL", "")
gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
if not gmail_email or not gmail_password:
    print("No GMAIL_EMAIL / GMAIL_APP_PASSWORD configured. Report saved but email not sent.")
    sys.exit(0)

msg = MIMEMultipart("alternative")
msg["Subject"] = f"Weekly Bing Ads Report - {today_fmt}"
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
