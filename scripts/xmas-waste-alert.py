#!/usr/bin/env python3
"""Daily waste alert for the Christmas lighting campaigns, Google + Microsoft.

The holiday campaigns are brand new and running loose match types in their first
season, so a junk query can start burning budget days before the Sunday report
would catch it. This runs every evening, reads yesterday's search terms on both
platforms, and emails only when something looks like it should be a negative.

Silence is the normal outcome. The script sends nothing on a clean day, so an
email arriving means there is something to act on.

Dual-credential like every other script here: ~/.config/<service>/config.json
locally, env-var secrets in CI.
"""

import json
import os
import smtplib
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TO_EMAILS = ["evelin@blackhilltx.com"]
CAMPAIGN = "BH_HolidayLighting_Search"

# A term has to cost at least this much in one day before it is worth an email.
# One cheap stray click is noise; the point is to catch money draining.
MIN_SPEND_TO_FLAG = 5.00

# Total daily zero-conversion spend above this gets called out even when no
# single term trips a pattern, because it means the whole day underperformed.
DAY_WASTE_ALERT = 25.00

# Never suggest negating our own brands. Mean Green is the company's former
# name, not a competitor.
BRAND_SAFE = ("black hill", "blackhill", "mean green", "meangreen")

# Patterns that mark a query as something Black Hill cannot sell or does not
# want. Each entry is (label, tuple of substrings). Order matters only for which
# label gets reported first.
PATTERNS = (
    ("retail / DIY product", (
        "govee", "home depot", "lowes", "lowe's", "amazon", "walmart", "costco",
        "menards", "target", "wholesale", "bulk", "for sale", "buy ", "cheap",
        "kit", "clips", "bulbs", "replacement bulb",
    )),
    ("permanent / year-round lighting", (
        "permanent", "year round", "year-round", "trimlight", "jellyfish",
        "everlight", "gemstone",
    )),
    ("not our service", (
        "repair", "electrician", "electrical", "wiring", "generator",
        "inflatable", "blow up", "projector", "laser light", "tree sales",
        "christmas tree farm", "wreath delivery",
    )),
    ("employment", (
        "job", "jobs", "hiring", "salary", "careers", "employment", "apply",
        "pay rate", "wages",
    )),
    ("informational / DIY how-to", (
        "how to", "how do i", "diy", "what is", "ideas", "tutorial", "video",
        "calculator", "template", "guide",
    )),
    ("light show / event, not install", (
        "light show", "drive through", "drive-through", "festival", "parade",
        "trail of lights", "zoo lights", "botanic garden",
    )),
    ("out of area", (
        "dallas", "plano", "frisco", "mckinney", "allen", "denton", "waco",
        "austin", "houston", "san antonio", "oklahoma", "tulsa", "abilene",
        "lubbock", "el paso", "mansfield", "springtown", "nebraska",
        "california", "florida", "colorado", "arizona", "near dallas",
    )),
)


def classify(term):
    """Return a label if the query looks like waste, else None."""
    t = term.lower()
    if any(b in t for b in BRAND_SAFE):
        return None
    for label, needles in PATTERNS:
        for n in needles:
            if n in t:
                return label
    return None


# --------------------------------------------------------------------------
# Google
# --------------------------------------------------------------------------
def google_terms(day):
    from google.ads.googleads.client import GoogleAdsClient

    if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        cfg = {
            "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
            "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            "customer_id": os.environ["GOOGLE_ADS_CUSTOMER_ID"],
        }
    else:
        with open(os.path.expanduser("~/.config/google-ads/config.json")) as f:
            cfg = json.load(f)

    client = GoogleAdsClient.load_from_dict(
        {k: cfg[k] for k in ("developer_token", "client_id", "client_secret",
                             "refresh_token", "login_customer_id")}
        | {"use_proto_plus": True, "timeout": 90})
    svc = client.get_service("GoogleAdsService")

    out = []
    query = f"""
        SELECT search_term_view.search_term, segments.search_term_match_type,
               metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM search_term_view
        WHERE campaign.name = '{CAMPAIGN}'
          AND segments.date = '{day.isoformat()}'
          AND metrics.cost_micros > 0
    """
    for r in svc.search(customer_id=cfg["customer_id"], query=query):
        out.append({
            "platform": "Google",
            "term": r.search_term_view.search_term,
            "match": r.segments.search_term_match_type.name,
            "clicks": r.metrics.clicks,
            "spend": r.metrics.cost_micros / 1e6,
            "conv": r.metrics.conversions,
        })
    return out


# --------------------------------------------------------------------------
# Microsoft
# --------------------------------------------------------------------------
def bing_terms(day):
    from bingads.service_client import ServiceClient
    from bingads.v13.reporting import (ReportingDownloadParameters,
                                       ReportingServiceManager)
    import bing_auth

    ad = bing_auth.get_authorization_data()
    rs = ServiceClient(service="ReportingService", version=13,
                       authorization_data=ad, environment="production")
    rm = ReportingServiceManager(authorization_data=ad,
                                 poll_interval_in_milliseconds=5000,
                                 environment="production")

    def _date(d):
        o = rs.factory.create("Date")
        o.Day, o.Month, o.Year = d.day, d.month, d.year
        return o

    req = rs.factory.create("SearchQueryPerformanceReportRequest")
    req.Format = "Csv"
    req.ReportName = "xmas waste"
    req.ReturnOnlyCompleteData = False
    req.Aggregation = "Summary"

    t = rs.factory.create("ReportTime")
    t.CustomDateRangeStart = _date(day)
    t.CustomDateRangeEnd = _date(day)
    t.PredefinedTime = None
    # The account time zone is GMT-02:00 Mid-Atlantic; force Central or the day
    # boundaries land in the wrong place.
    t.ReportTimeZone = "CentralTimeUSCanada"
    req.Time = t

    scope = rs.factory.create("AccountThroughAdGroupReportScope")
    scope.AccountIds = {"long": [ad.account_id]}
    scope.Campaigns = None
    scope.AdGroups = None
    req.Scope = scope

    cols = rs.factory.create("ArrayOfSearchQueryPerformanceReportColumn")
    cols.SearchQueryPerformanceReportColumn.append([
        "CampaignName", "SearchQuery", "DeliveredMatchType",
        "Clicks", "Spend", "Conversions"])
    req.Columns = cols

    out = []
    with tempfile.TemporaryDirectory() as tmp:
        params = ReportingDownloadParameters(
            report_request=req, result_file_directory=tmp,
            result_file_name="r.csv", overwrite_result_file=True,
            timeout_in_milliseconds=300000)
        container = rm.download_report(params)
        if container is None:
            return out
        for rec in container.report_records:
            row = {k: rec.value(k) for k in container.report_columns}
            if row["CampaignName"] != CAMPAIGN:
                continue
            spend = float(row["Spend"] or 0)
            if spend <= 0:
                continue
            out.append({
                "platform": "Microsoft",
                "term": row["SearchQuery"],
                "match": row["DeliveredMatchType"],
                "clicks": int(row["Clicks"] or 0),
                "spend": spend,
                "conv": float(row["Conversions"] or 0),
            })
        container.close()
    return out


# --------------------------------------------------------------------------
def build_email(day, rows, flagged, waste_total, spend_total):
    by_label = defaultdict(list)
    for r in flagged:
        by_label[r["label"]].append(r)

    lines = [
        f"<p>Christmas lighting search terms for <b>{day:%A %d %B %Y}</b>.</p>",
        f"<p>Spend {money(spend_total)} across {len(rows)} terms. "
        f"Zero-conversion spend {money(waste_total)}.</p>",
    ]

    if flagged:
        lines.append("<h3>Suggested negatives</h3>")
        lines.append('<table cellpadding="6" cellspacing="0" border="1" '
                     'style="border-collapse:collapse;font-family:sans-serif;font-size:13px">')
        lines.append("<tr style='background:#f2f2f2'><th align='left'>Search term</th>"
                     "<th align='left'>Why</th><th>Platform</th><th>Match</th>"
                     "<th align='right'>Clicks</th><th align='right'>Spend</th></tr>")
        for label in sorted(by_label):
            for r in sorted(by_label[label], key=lambda x: -x["spend"]):
                lines.append(
                    f"<tr><td>{esc(r['term'])}</td><td>{esc(label)}</td>"
                    f"<td align='center'>{r['platform']}</td>"
                    f"<td align='center'>{esc(str(r['match']))}</td>"
                    f"<td align='right'>{r['clicks']}</td>"
                    f"<td align='right'>{money(r['spend'])}</td></tr>")
        lines.append("</table>")
        lines.append(f"<p>Flagged spend: <b>{money(sum(r['spend'] for r in flagged))}</b>.</p>")
    else:
        lines.append("<h3>No terms matched a waste pattern</h3>")
        lines.append("<p>This email was sent because total zero-conversion spend "
                     f"passed the {money(DAY_WASTE_ALERT)} daily threshold, not because "
                     "a specific term looked wrong. Worth a look at the search "
                     "terms tab.</p>")

    zero = [r for r in rows if r["conv"] == 0 and r["spend"] >= MIN_SPEND_TO_FLAG
            and not any(f["term"] == r["term"] and f["platform"] == r["platform"] for f in flagged)]
    if zero:
        lines.append("<h3>Other spend with no conversion</h3>")
        lines.append("<p>These look legitimate, listed so nothing is hidden.</p><ul>")
        for r in sorted(zero, key=lambda x: -x["spend"])[:15]:
            lines.append(f"<li>{esc(r['term'])} - {r['platform']}, "
                         f"{r['clicks']} clicks, {money(r['spend'])}</li>")
        lines.append("</ul>")

    lines.append("<hr><p style='color:#777;font-size:12px'>"
                 "Daily Christmas waste alert. Silent on clean days. "
                 "Thresholds: a term must cost "
                 f"{money(MIN_SPEND_TO_FLAG)} to be flagged, or the day must exceed "
                 f"{money(DAY_WASTE_ALERT)} in zero-conversion spend.</p>")
    return "\n".join(lines)


def money(v):
    return f"${v:,.2f}"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def send(subject, html):
    gmail_email = os.environ.get("GMAIL_EMAIL")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_email or not gmail_password:
        cfg_path = os.path.expanduser("~/.config/gmail-sender/config.json")
        with open(cfg_path) as f:
            c = json.load(f)
        gmail_email = c["email"]
        gmail_password = c["app_password"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Ads Monitor", gmail_email))
    msg["To"] = ", ".join(TO_EMAILS)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(gmail_email, gmail_password)
        server.sendmail(gmail_email, TO_EMAILS, msg.as_string())


def main():
    dry_run = "--dry-run" in sys.argv
    # Default to yesterday: today's numbers are still settling on both platforms.
    day = date.today() - timedelta(days=1)
    for arg in sys.argv[1:]:
        if arg.startswith("--date="):
            day = datetime.strptime(arg.split("=", 1)[1], "%Y-%m-%d").date()

    rows = []
    for name, fn in (("Google", google_terms), ("Microsoft", bing_terms)):
        try:
            rows.extend(fn(day))
        except Exception as exc:  # one platform failing must not kill the run
            print(f"WARNING: {name} pull failed: {exc}", file=sys.stderr)

    spend_total = sum(r["spend"] for r in rows)
    waste_total = sum(r["spend"] for r in rows if r["conv"] == 0)

    flagged = []
    for r in rows:
        if r["spend"] < MIN_SPEND_TO_FLAG:
            continue
        label = classify(r["term"])
        if label:
            flagged.append(dict(r, label=label))

    print(f"{day}: {len(rows)} terms, {money(spend_total)} spend, "
          f"{money(waste_total)} zero-conversion, {len(flagged)} flagged")
    for f in flagged:
        print(f"  FLAG [{f['label']}] {f['term']} ({f['platform']}, {money(f['spend'])})")

    if not flagged and waste_total < DAY_WASTE_ALERT:
        print("nothing to report, staying silent")
        return

    html = build_email(day, rows, flagged, waste_total, spend_total)
    subject = (f"Christmas ads: {len(flagged)} term(s) to negate, "
               f"{money(sum(f['spend'] for f in flagged))}" if flagged
               else f"Christmas ads: {money(waste_total)} with no conversions")

    if dry_run:
        print("\n--- DRY RUN, email not sent ---\n")
        print(html)
        return
    send(subject, html)
    print("alert sent")


if __name__ == "__main__":
    main()
