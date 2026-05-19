#!/usr/bin/env python3
"""Weekly Aspire Material Receipt Audit — Black Hill Landscaping.

Pulls Won Landscape opportunities (DivisionID=17) and flags any where the
estimated material cost has not been entered into actuals yet. Emails Evelin,
Denisse, and Grace (Ops) every Monday 8am Central with a punch list so Grace
can catch up on receipt entry.

A job is flagged when:
  - EstimatedMaterialCost >= $50, AND
  - ActualCostMaterial < 50% of estimate, AND
  - Job is Complete OR PercentComplete > 10%

If no jobs are flagged, no email is sent.

Cron runs at both 13:00 UTC and 14:00 UTC Monday; this script only sends when
local Chicago time hour == 8, giving 8am Central year-round through DST.

Usage:
  python3 aspire-material-audit.py              # Run audit
  python3 aspire-material-audit.py --test       # Test API + email auth
  python3 aspire-material-audit.py --force      # Skip 8am Central guard
  python3 aspire-material-audit.py --dry-run    # Run audit but don't send email
"""

import json, os, sys, signal, smtplib, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# --- Timeout guard ---
def _timeout_handler(signum, frame):
    log("TIMEOUT: Script exceeded time limit")
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(120)

# --- Config ---
CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
RECIPIENTS = [
    "evelin@blackhilltx.com",
    # TEST MODE: Denisse + Ops will be added after Evelin reviews the format
    # "denisse@blackhilltx.com",
    # "Ops@blackhilltx.com",
]
ASPIRE_PORTAL = "https://cloud.youraspire.com"
LOOKBACK_MONTHS = 6  # Audit only opps won in the last 6 months
MIN_MATERIAL_EST = 50.00   # Skip estimates below this
GAP_THRESHOLD = 0.50       # Flag if actual material < 50% of estimate
MIN_PERCENT_COMPLETE = 0.10  # Skip in-production jobs <10% done

DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv
TEST_MODE = "--test" in sys.argv


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --- Aspire API ---

def load_config():
    """Prefer ASPIRE_REPORTING_* env vars (GH Actions), fall back to config file."""
    client_id = os.environ.get("ASPIRE_REPORTING_CLIENT_ID")
    secret = os.environ.get("ASPIRE_REPORTING_SECRET")
    if client_id and secret:
        return {"api_base_url": "https://cloud-api.youraspire.com",
                "api_client_id": client_id, "api_secret": secret}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
            if cfg.get("reporting_client_id"):
                return {"api_base_url": cfg.get("api_base_url", "https://cloud-api.youraspire.com"),
                        "api_client_id": cfg["reporting_client_id"],
                        "api_secret": cfg["reporting_secret"]}
            return cfg
    return None


def authenticate(config):
    base = config["api_base_url"]
    data = json.dumps({"ClientId": config["api_client_id"],
                        "Secret": config["api_secret"]}).encode()
    req = urllib.request.Request(f"{base}/Authorization", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            return body.get("Token", "")
    except urllib.error.HTTPError as e:
        log(f"Auth failed: {e.code} {e.read().decode() if e.fp else ''}")
        return None


def odata_query(config, token, endpoint, params):
    base = config["api_base_url"]
    qs = urllib.parse.urlencode(params, safe="=&$,'()@ ")
    url = f"{base}/{endpoint}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            return body.get("value", body) if isinstance(body, dict) else body
    except urllib.error.HTTPError as e:
        log(f"Query failed: {endpoint} - {e.code} {e.read().decode() if e.fp else ''}")
        return []


# --- Vendor hint ---

VENDOR_HINTS = [
    (("sod", "tiftuf", "zoysia", "bermuda"), "Prime Sod / King Ranch"),
    (("dg ", "decomposed granite"), "CFM Stone / SiteOne"),
    (("mulch",), "Organic Recycler"),
    (("edging",), "Ewing (steel edging)"),
    (("drain", "drainage"), "Ewing / SiteOne (drain pipe)"),
    (("lighting", "lights"), "Lighting vendor"),
    (("topdress", "soil",), "Mayer (soil)"),
    (("flower", "annual", "color"), "Annual flower vendor"),
    (("plant", "azalea", "juniper", "tree"), "Plant vendor"),
    (("stone", "boulder", "flagstone"), "CFM / Stone vendor"),
]

def guess_vendor(opp_name):
    name = (opp_name or "").lower()
    hits = []
    for keywords, label in VENDOR_HINTS:
        for kw in keywords:
            if kw in name:
                hits.append(label)
                break
    if hits:
        return " + ".join(dict.fromkeys(hits))  # dedupe while preserving order
    return "Unknown - check job notes"


# --- Audit ---

def fetch_landscape_opps(config, token):
    """Pull Won Landscape opps from the last LOOKBACK_MONTHS months."""
    cutoff = (date.today() - timedelta(days=LOOKBACK_MONTHS * 31)).isoformat()
    rows = odata_query(config, token, "Opportunities", {
        "$filter": (f"DivisionID eq 17 and OpportunityStatusID eq 15 "
                    f"and WonDate ge {cutoff}T00:00:00Z"),
        "$select": ("OpportunityID,OpportunityNumber,OpportunityName,PropertyName,"
                    "WonDate,CompleteDate,JobStatusName,PercentComplete,"
                    "EstimatedMaterialCost,ActualCostMaterial,EstimatedDollars,"
                    "SalesRepContactName"),
        "$top": "200",
        "$orderby": "WonDate desc"
    })
    return rows


def find_gaps(opps):
    """Return list of flagged opps with gap info."""
    flagged = []
    for o in opps:
        est_mat = o.get("EstimatedMaterialCost") or 0
        act_mat = o.get("ActualCostMaterial") or 0
        pct = o.get("PercentComplete") or 0
        job_status = o.get("JobStatusName") or ""

        if est_mat < MIN_MATERIAL_EST:
            continue
        # Only audit jobs that have progressed
        if job_status != "Complete" and pct < MIN_PERCENT_COMPLETE:
            continue
        # Flag if material logged is below threshold
        if act_mat >= est_mat * GAP_THRESHOLD:
            continue
        gap = est_mat - act_mat
        flagged.append({
            "opp_id": o.get("OpportunityID"),
            "opp_num": o.get("OpportunityNumber"),
            "name": o.get("OpportunityName", "").strip(),
            "property": o.get("PropertyName", "").strip(),
            "status": "Complete" if job_status == "Complete" else "In Production",
            "complete_date": (o.get("CompleteDate") or "")[:10],
            "won_date": (o.get("WonDate") or "")[:10],
            "pct": pct,
            "est_material": round(est_mat, 2),
            "actual_material": round(act_mat, 2),
            "gap": round(gap, 2),
            "vendor_hint": guess_vendor(o.get("OpportunityName", "")),
            "sales_rep": (o.get("SalesRepContactName") or "").replace(" Montenegro", ""),
            "job_value": round(o.get("EstimatedDollars") or 0, 2),
        })
    # Sort: Complete jobs first (most urgent), then by gap descending
    flagged.sort(key=lambda r: (r["status"] != "Complete", -r["gap"]))
    return flagged


# --- Reporting ---

def build_email(flagged):
    today = date.today()
    week_label = today.strftime("%b %d, %Y")
    total_gap = sum(r["gap"] for r in flagged)
    n_complete = sum(1 for r in flagged if r["status"] == "Complete")
    n_inprog = sum(1 for r in flagged if r["status"] != "Complete")

    subject = f"Aspire Material Receipts - {len(flagged)} jobs need entry (${total_gap:,.0f} pending) - {week_label}"

    html = [
        "<html><body style='font-family:Arial,sans-serif;color:#222;'>",
        f"<h2 style='margin:0 0 8px 0;'>Material Receipt Audit - Week of {week_label}</h2>",
        f"<p><strong>{len(flagged)}</strong> Landscape jobs are missing material receipts in Aspire.<br>",
        f"&nbsp;&nbsp;- {n_complete} completed job(s)<br>",
        f"&nbsp;&nbsp;- {n_inprog} in-production job(s) (>10% complete)<br>",
        f"<strong>Total material gap: ${total_gap:,.2f}</strong></p>",
        "<p style='background:#FFF3CD;padding:10px;border-left:4px solid #FFC107;'>",
        "<strong>Grace</strong>: Please enter the missing receipts for each job below. ",
        "Match vendor invoices to the work ticket by date + property, then enter via ",
        f"<a href='{ASPIRE_PORTAL}/app/purchasing/receipts'>Purchasing &gt; Purchase Receipts</a> in Aspire.",
        "</p>",
        "<table style='border-collapse:collapse;width:100%;font-size:13px;'>",
        "<thead><tr style='background:#305496;color:white;'>",
        "<th style='padding:6px;text-align:left;'>Opp #</th>",
        "<th style='padding:6px;text-align:left;'>Status</th>",
        "<th style='padding:6px;text-align:left;'>Completed</th>",
        "<th style='padding:6px;text-align:left;'>Property</th>",
        "<th style='padding:6px;text-align:left;'>Job</th>",
        "<th style='padding:6px;text-align:left;'>Rep</th>",
        "<th style='padding:6px;text-align:right;'>Est Material</th>",
        "<th style='padding:6px;text-align:right;'>Logged</th>",
        "<th style='padding:6px;text-align:right;'>Gap</th>",
        "<th style='padding:6px;text-align:left;'>Likely Vendor</th>",
        "<th style='padding:6px;text-align:left;'>Link</th>",
        "</tr></thead><tbody>",
    ]
    plain = [
        f"Material Receipt Audit - Week of {week_label}",
        f"{len(flagged)} jobs missing material receipts. Total gap: ${total_gap:,.2f}",
        "",
        f"{'Opp':<6} {'Status':<14} {'Property':<30} {'Job':<35} {'Est':>9} {'Logged':>9} {'Gap':>9}  Vendor",
        "-" * 140,
    ]

    for r in flagged:
        bg = "#FFC7CE" if r["status"] == "Complete" and r["actual_material"] == 0 else (
             "#FFEB9C" if r["actual_material"] > 0 else "#E7E6E6")
        opp_url = f"{ASPIRE_PORTAL}/app/opportunities/{r['opp_id']}"
        html.append(
            f"<tr style='background:{bg};border-bottom:1px solid #ccc;'>"
            f"<td style='padding:6px;'>{r['opp_num'] or r['opp_id']}</td>"
            f"<td style='padding:6px;'>{r['status']}</td>"
            f"<td style='padding:6px;'>{r['complete_date'] or '-'}</td>"
            f"<td style='padding:6px;'>{r['property']}</td>"
            f"<td style='padding:6px;'>{r['name']}</td>"
            f"<td style='padding:6px;'>{r['sales_rep']}</td>"
            f"<td style='padding:6px;text-align:right;'>${r['est_material']:,.2f}</td>"
            f"<td style='padding:6px;text-align:right;'>${r['actual_material']:,.2f}</td>"
            f"<td style='padding:6px;text-align:right;'><strong>${r['gap']:,.2f}</strong></td>"
            f"<td style='padding:6px;'>{r['vendor_hint']}</td>"
            f"<td style='padding:6px;'><a href='{opp_url}'>Open</a></td>"
            f"</tr>"
        )
        plain.append(
            f"{(r['opp_num'] or r['opp_id'])!s:<6} {r['status']:<14} "
            f"{r['property'][:30]:<30} {r['name'][:35]:<35} "
            f"${r['est_material']:>7,.0f} ${r['actual_material']:>7,.0f} ${r['gap']:>7,.0f}  "
            f"{r['vendor_hint']}"
        )

    html.append("</tbody></table>")
    html.append(f"<p style='color:#666;font-size:12px;margin-top:20px;'>")
    html.append(f"Audit window: opps won in the last {LOOKBACK_MONTHS} months. ")
    html.append(f"A job is flagged when est material >= ${MIN_MATERIAL_EST:.0f} ")
    html.append(f"and actual logged < {GAP_THRESHOLD*100:.0f}% of estimate ")
    html.append(f"(and job is Complete or >{MIN_PERCENT_COMPLETE*100:.0f}% in production).")
    html.append("</p>")
    html.append("</body></html>")

    plain.append("")
    plain.append(f"Total gap: ${total_gap:,.2f}")
    plain.append(f"Enter receipts at: {ASPIRE_PORTAL}/app/purchasing/receipts")

    return subject, "\n".join(html), "\n".join(plain)


def send_email(subject, html_body, plain_body):
    sender = os.environ.get("GMAIL_EMAIL")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        log("No email credentials - printing report to stdout")
        print(f"\nSubject: {subject}\n")
        print(plain_body)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Aspire Audit", sender))
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, RECIPIENTS, msg.as_string())
        log(f"Email sent to {len(RECIPIENTS)} recipients: {subject}")
        return True
    except Exception as e:
        log(f"Email failed: {e}")
        print(f"\nSubject: {subject}\n")
        print(plain_body)
        return False


# --- Main ---

def main():
    log("Aspire Material Audit starting")

    # 8am Central guard (when running on cron with two UTC times)
    if not FORCE and not TEST_MODE:
        central_now = datetime.now(ZoneInfo("America/Chicago"))
        if central_now.weekday() == 0 and central_now.hour != 8:
            log(f"Skipping - current Central hour is {central_now.hour:02d}, only run at 08")
            sys.exit(0)

    config = load_config()
    if not config:
        log("ERROR: No Aspire config found")
        sys.exit(1)

    token = authenticate(config)
    if not token:
        log("ERROR: Aspire auth failed")
        sys.exit(1)
    log("Aspire authenticated")

    if TEST_MODE:
        log("Test mode - API connection OK")
        # Also verify email credentials
        sender = os.environ.get("GMAIL_EMAIL")
        if sender:
            log(f"Email sender configured: {sender}")
        else:
            log("WARNING: GMAIL_EMAIL not set")
        sys.exit(0)

    opps = fetch_landscape_opps(config, token)
    log(f"Fetched {len(opps)} Landscape opportunities")

    flagged = find_gaps(opps)
    log(f"Flagged {len(flagged)} jobs with material gaps")

    if not flagged:
        log("No material gaps - skipping email")
        return

    subject, html, plain = build_email(flagged)

    if DRY_RUN:
        log("Dry run - printing report")
        print(f"\nSubject: {subject}\n")
        print(plain)
        return

    send_email(subject, html, plain)


if __name__ == "__main__":
    main()
