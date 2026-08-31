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
import json, os, sys, smtplib, tempfile, signal, urllib.parse, urllib.request
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
# AdDistribution splits Search from the Microsoft Audience Network. Blending them
# makes impressions look like growth and craters CTR: on 2026-08-30 audience served
# 2,254 impressions for 7 clicks and 0 conversions, dragging a healthy 3.33% search
# CTR down to a reported 1.41%.
DIST_COLS = ["AdDistribution", "Impressions", "Clicks", "Spend", "Conversions"]
QUERY_COLS = ["SearchQuery", "CampaignName", "Impressions", "Clicks", "Spend", "Conversions"]
KEYWORD_COLS = ["Keyword", "CampaignName", "Impressions", "Clicks", "Spend",
                "AverageCpc", "Conversions"]
GOAL_COLS = ["Goal", "CampaignName", "AllConversions", "AllRevenue"]


def account_totals(start, end):
    """Account totals summed from the campaign report.

    AccountPerformanceReport rejects our request shape ("Invalid client data"),
    and deriving from campaigns guarantees section 1 reconciles with section 2.
    """
    rows = run_report(build_request("CampaignPerformance", DIST_COLS, start, end))
    t = {"impressions": 0.0, "clicks": 0.0, "spend": 0.0, "conversions": 0.0,
         "aud_impressions": 0.0, "aud_clicks": 0.0, "aud_spend": 0.0, "aud_conversions": 0.0}
    for r in rows:
        audience = "audience" in str(r.get("AdDistribution") or "").lower()
        pre = "aud_" if audience else ""
        t[pre + "impressions"] += num(r.get("Impressions"))
        t[pre + "clicks"] += num(r.get("Clicks"))
        t[pre + "spend"] += num(r.get("Spend"))
        t[pre + "conversions"] += num(r.get("Conversions"))
    # CTR and CPC describe SEARCH only; audience is reported separately below the table.
    t["ctr"] = (t["clicks"] / t["impressions"] * 100) if t["impressions"] else 0.0
    t["cpc"] = (t["spend"] / t["clicks"]) if t["clicks"] else 0.0
    t["total_spend"] = t["spend"] + t["aud_spend"]
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
for k in ("impressions", "clicks", "spend", "conversions", "total_spend",
          "aud_impressions", "aud_clicks", "aud_spend", "aud_conversions"):
    four_week[k] = four_week[k] / 4.0

goals_this = goal_totals(this_start, this_end)
goals_prev = goal_totals(prev_start, prev_end)


def contacts(goals):
    return sum(v for k, v in goals.items() if k in CONTACT_GOALS)


contacts_this, contacts_prev = contacts(goals_this), contacts(goals_prev)
cost_per_contact = (this_week["total_spend"] / contacts_this) if contacts_this else 0.0

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
# ASPIRE WON REVENUE ATTRIBUTED TO BING ADS
# ============================================================
# Ports the attribution the Google report already uses: Lead Source lives on the
# CONTACT (custom field definition 34), and a customer belongs to one origin, so
# every won opportunity for that customer rolls up to it. First touch wins when a
# property has several tagged contacts. Aspire already carries a "Bing Ads" value,
# so nothing new had to be created.
#
# Read this as a FLOOR, not a full picture: most won opportunities carry no lead
# source at all, and the roll-up credits Bing with a customer's later work too.
ASPIRE_SOURCE = "Bing Ads"
LAUNCH_DATE = "2026-02-19"


def get_aspire_bing_revenue(start_date, end_date):
    """Won dollars attributed to Bing Ads. Returns None if Aspire is unreachable."""
    try:
        client_id = os.environ.get("ASPIRE_REPORTING_CLIENT_ID") or os.environ.get("ASPIRE_CLIENT_ID")
        secret = os.environ.get("ASPIRE_REPORTING_SECRET") or os.environ.get("ASPIRE_SECRET")
        if not client_id or not secret:
            cfg_path = os.path.expanduser("~/.config/aspire/config.json")
            if not os.path.exists(cfg_path):
                return None
            with open(cfg_path) as f:
                acfg = json.load(f)
            client_id = acfg.get("reporting_client_id", acfg.get("client_id"))
            secret = acfg.get("reporting_secret", acfg.get("secret"))
        base = os.environ.get("ASPIRE_API_URL", "https://cloud-api.youraspire.com")
        auth = json.dumps({"ClientId": client_id, "Secret": secret}).encode()
        areq = urllib.request.Request(f"{base}/Authorization", data=auth,
                                      headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(areq, timeout=20) as resp:
            token = json.loads(resp.read().decode()).get("Token", "")
        if not token:
            return None

        def paged(entity, params, ps=500):
            out, skip = [], 0
            while True:
                url = f"{base}/{entity}?" + urllib.parse.quote(
                    params + f"&$top={ps}&$skip={skip}", safe="=&$,()/%:@")
                req = urllib.request.Request(
                    url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read().decode())
                d = d if isinstance(d, list) else [d]
                out += d
                if len(d) < ps:
                    break
                skip += ps
            return out

        cf = paged("ContactCustomFields", "$filter=ContactCustomFieldDefinitionID eq 34")
        src = {int(x["ContactID"]): (x.get("ColumnValue") or "").strip()
               for x in cf if (x.get("ColumnValue") or "").strip()}
        if not src:
            return None
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
            return src[min(cands, key=lambda c: created.get(c, "9999"))]

        WON = "OpportunityStatusName eq 'Won'"
        sel = ("$select=WonDollars,OpportunityName,OpportunityNumber,OpportunityID,"
               "BillingContactID,PropertyID,WonDate")
        opps = paged("Opportunities", f"$filter={WON} and WonDate ge {LAUNCH_DATE}T00:00:00Z&{sel}")

        week, life = [], [0, 0.0]
        by_customer = {}
        for o in opps:
            if origin(o.get("BillingContactID"), o.get("PropertyID")) != ASPIRE_SOURCE:
                continue
            dollars = float(o.get("WonDollars", 0) or 0)
            won_on = str(o.get("WonDate") or "")[:10]
            life[0] += 1
            life[1] += dollars
            pid = o.get("PropertyID")
            if pid is not None:
                by_customer[pid] = by_customer.get(pid, 0.0) + dollars
            if start_date <= won_on <= end_date:
                oid = o.get("OpportunityID")
                week.append({"name": o.get("OpportunityName") or "Unnamed opportunity",
                             "dollars": dollars, "won_on": won_on,
                             "url": f"https://cloud.youraspire.com/app/opportunities/{oid}" if oid else None})
        week.sort(key=lambda x: -x["dollars"])
        top_dollars = max(by_customer.values()) if by_customer else 0.0
        return {"week_opps": week,
                "week_count": len(week),
                "week_dollars": sum(w["dollars"] for w in week),
                "life_count": life[0],
                "life_dollars": life[1],
                "customers": len(by_customer),
                "top_customer_dollars": top_dollars,
                "ex_top_dollars": life[1] - top_dollars}
    except Exception as e:
        print(f"Aspire revenue warning: {e}", file=sys.stderr)
        return None


aspire_rev = get_aspire_bing_revenue(this_start.strftime("%Y-%m-%d"), this_end.strftime("%Y-%m-%d"))
# Spend since launch, so lifetime return is a real number rather than a ratio of
# one week's revenue to one week's spend (won dates lag the clicks that caused them).
life_spend = account_totals(datetime.strptime(LAUNCH_DATE, "%Y-%m-%d"), now)["total_spend"] if aspire_rev else 0.0

# ============================================================
# SECTION 6: RECOMMENDATIONS
# ============================================================
def build_actions():
    out = []

    # Contact goals recorded but NOT reflected in the Conversions column.
    # Inferred, not assumed: if the goals that fired already sum to at least the
    # account Conversions figure, everything is being counted. The old version
    # hard-coded "anything but Lead Form Submission is excluded", which kept
    # printing a false warning after phone click was switched on 2026-08-23.
    fired = sum(v for k, v in goals_this.items() if k in CONTACT_GOALS and v > 0)
    counted = this_week["conversions"]
    if fired > counted + 0.5:
        gap = fired - counted
        out.append(
            f"Conversion tracking may be undercounting: contact goals recorded "
            f"{fired:.0f} actions but only {counted:.0f} reached the Conversions column "
            f"({gap:.0f} unaccounted). Check each goal's Include in \"Conversions\" setting.")

    # Audience network noise: cheap, but it distorts impressions and CTR.
    if this_week["aud_impressions"] > this_week["impressions"] and this_week["aud_conversions"] == 0:
        out.append(
            f"Microsoft Audience Network served {this_week['aud_impressions']:,.0f} impressions "
            f"({this_week['aud_clicks']:,.0f} clicks, ${this_week['aud_spend']:,.2f}, no conversions) "
            f"versus {this_week['impressions']:,.0f} on search. It costs little but inflates "
            f"impressions and depresses blended CTR.")

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
    ("Spend", f"${this_week['total_spend']:,.2f}", f"${prev_week['total_spend']:,.2f}",
     f"${four_week['total_spend']:,.2f}", delta(this_week["total_spend"], prev_week["total_spend"], inverse=True)),
    ("Clicks (search)", f"{this_week['clicks']:,.0f}", f"{prev_week['clicks']:,.0f}",
     f"{four_week['clicks']:,.0f}", delta(this_week["clicks"], prev_week["clicks"])),
    ("Impressions (search)", f"{this_week['impressions']:,.0f}", f"{prev_week['impressions']:,.0f}",
     f"{four_week['impressions']:,.0f}", delta(this_week["impressions"], prev_week["impressions"])),
    ("CTR (search)", f"{this_week['ctr']:.2f}%", f"{prev_week['ctr']:.2f}%",
     f"{four_week['ctr']:.2f}%", delta(this_week["ctr"], prev_week["ctr"])),
    ("Avg CPC (search)", f"${this_week['cpc']:,.2f}", f"${prev_week['cpc']:,.2f}",
     f"${four_week['cpc']:,.2f}", delta(this_week["cpc"], prev_week["cpc"], inverse=True)),
    ("Conversions (counted)", f"{this_week['conversions']:,.0f}", f"{prev_week['conversions']:,.0f}",
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

aud_note = (f"Audience network (excluded from the search figures above): "
            f"{this_week['aud_impressions']:,.0f} impressions, {this_week['aud_clicks']:,.0f} clicks, "
            f"${this_week['aud_spend']:,.2f}, {this_week['aud_conversions']:,.0f} conversions")
h(f'<div style="margin-top:12px;padding:10px 14px;background:#1a1a1a;border-left:3px solid #666;'
  f'border-radius:4px;font-size:12px;color:#999;">{aud_note}</div>')
m(f"\n{aud_note}\n")

if goals_this:
    breakdown = ", ".join(f"{k}: {int(v)}" for k, v in sorted(goals_this.items()) if v)
    h(f'<div style="margin-top:12px;padding:10px 14px;background:#1a2332;border-left:3px solid #3498db;'
      f'border-radius:4px;font-size:12px;color:#9cf;">Goals this week &mdash; {breakdown}</div>')
    m(f"\nGoals this week: {breakdown}\n")
h("</div>")


# --- Revenue Context (Aspire won revenue attributed to Bing Ads) ---
h('<div class="section"><h2>Revenue Context</h2>')
m("\n## Revenue Context\n")
if aspire_rev:
    wk_roas = (aspire_rev["week_dollars"] / this_week["total_spend"]) if this_week["total_spend"] else 0
    life_roas = (aspire_rev["life_dollars"] / life_spend) if life_spend else 0
    h('<table><tr><th>Won revenue attributed to Bing Ads</th>'
      '<th class="right">This Week</th><th class="right">Since Feb 19</th></tr>')
    rev_rows = [
        ("Won opportunities", f"{aspire_rev['week_count']:,}", f"{aspire_rev['life_count']:,}"),
        ("Won revenue", f"${aspire_rev['week_dollars']:,.2f}", f"${aspire_rev['life_dollars']:,.2f}"),
        ("Ad spend", f"${this_week['total_spend']:,.2f}", f"${life_spend:,.2f}"),
        ("Return on spend", f"{wk_roas:,.1f}x" if this_week["total_spend"] else "--",
         f"{life_roas:,.1f}x" if life_spend else "--"),
    ]
    m("| Won revenue attributed to Bing Ads | This Week | Since Feb 19 |")
    m("|---|---|---|")
    for label, a, b in rev_rows:
        colour = ""
        if label == "Return on spend" and life_roas >= 1:
            colour = ' style="color:#27ae60;font-weight:600;"'
        h(f'<tr><td>{label}</td><td class="right"{colour}>{a}</td><td class="right"{colour}>{b}</td></tr>')
        m(f"| {label} | {a} | {b} |")
    h("</table>")

    # A single customer can carry the whole ratio, so say so next to the ratio.
    if aspire_rev["life_dollars"] > 0 and aspire_rev.get("customers"):
        share = aspire_rev["top_customer_dollars"] / aspire_rev["life_dollars"] * 100
        ex_roas = (aspire_rev["ex_top_dollars"] / life_spend) if life_spend else 0
        conc = (f"Concentration: {aspire_rev['life_count']} jobs across "
                f"{aspire_rev['customers']} customers. The largest is "
                f"${aspire_rev['top_customer_dollars']:,.2f} of ${aspire_rev['life_dollars']:,.2f} "
                f"({share:.0f}%). Excluding them, ${aspire_rev['ex_top_dollars']:,.2f} on "
                f"${life_spend:,.2f} spend is {ex_roas:,.1f}x.")
        warn = share >= 50
        h(f'<div style="margin-top:10px;padding:10px 14px;background:{"#1a1212" if warn else "#1a1a1a"};'
          f'border-left:3px solid {"#e74c3c" if warn else "#666"};border-radius:4px;font-size:12px;'
          f'color:{"#e08" if warn else "#999"};">{conc}</div>')
        m(f"\n**{conc}**\n")

    if aspire_rev["week_opps"]:
        h('<div style="margin-top:14px;font-size:12px;color:#c8963e;text-transform:uppercase;'
          'letter-spacing:0.5px;">Won this week</div>')
        m("\nWon this week:\n")
        for o in aspire_rev["week_opps"]:
            link = f'<a href="{o["url"]}" style="color:#9cf;text-decoration:none;">{o["name"]}</a>' if o["url"] else o["name"]
            h(f'<div style="margin-top:6px;padding:8px 12px;background:#1a1a1a;border-radius:4px;font-size:12px;">'
              f'{o["won_on"]} &middot; <strong>${o["dollars"]:,.2f}</strong> &middot; {link}</div>')
            m(f"- {o['won_on']}  ${o['dollars']:,.2f}  {o['name']}")
    else:
        h('<div style="margin-top:10px;font-size:12px;color:#888;">No Bing-attributed opportunities '
          'were won this week. Won dates lag the clicks that caused them.</div>')
        m("\nNo Bing-attributed opportunities were won this week.")

    caveat = ("Attribution is first touch by customer origin, from the Lead Source field in Aspire. "
              "A customer tagged Bing Ads has all their won work counted here, including later jobs. "
              "Most won opportunities carry no lead source at all, so treat this as a floor.")
    h(f'<div style="margin-top:12px;padding:10px 14px;background:#1a1a1a;border-left:3px solid #666;'
      f'border-radius:4px;font-size:11px;color:#888;">{caveat}</div>')
    m(f"\n*{caveat}*\n")
else:
    h('<div style="color:#888;font-size:13px;">Aspire revenue unavailable this run.</div>')
    m("Aspire revenue unavailable this run.")
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
