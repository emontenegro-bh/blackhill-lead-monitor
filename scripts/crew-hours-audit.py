#!/usr/bin/env python3
"""Daily Crew Hours Audit — Reviews previous workday's Aspire WorkTickets.

Compares actual hours against budget and historical averages.
Flags anomalies: over-budget, time padding, unusual performance.
Sends email report to Evelin at noon CT every workday.

Usage:
  python3 crew-hours-audit.py              # Review previous workday
  python3 crew-hours-audit.py --test       # Test API connection
  python3 crew-hours-audit.py --date 2026-04-13  # Review specific date
"""

import json, os, sys, signal, smtplib, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

# --- Timeout guard ---
def _timeout_handler(signum, frame):
    log("TIMEOUT: Script exceeded time limit")
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(120)

# --- Config ---
CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
TOKEN_CACHE = os.path.expanduser("~/.config/aspire/api-token.json")
CLOUD_MODE = bool(os.environ.get("ASPIRE_CLIENT_ID"))
RECIPIENT = "evelin@blackhilltx.com"

# --- Schedule Data ---
# Expected crew sizes per property per day (from route-scheduler skill)
# Key: OpportunityID -> {day, crew_size, crew_leader, budget, property_name, target_notes}
SCHEDULE = {
    # Monday — Combined (5 people) — Leo at Bethel
    1779: {"day": "Monday", "crew_size": 5, "leader": "Combined", "budget": 37.0,
           "name": "Leo at Bethel", "target_wall_clock": 5.5, "target_manhours": 27.5},
    # Tuesday — Gustavo (3) — Watauga
    2837: {"day": "Tuesday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 20.0, "name": "Capp Smith Park"},
    2836: {"day": "Tuesday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 4.0, "name": "BISD Park"},
    2820: {"day": "Tuesday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 6.0, "name": "Whites Branch Creek Trail"},
    # Tuesday — Jorge (2) — S Fort Worth
    2491: {"day": "Tuesday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 16.0, "name": "University Christian Church"},
    3156: {"day": "Tuesday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 2.0, "name": "Nick Workman Residence"},
    3129: {"day": "Tuesday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 3.0, "name": "Richard Watters Residence"},
    # Wednesday — Gustavo (2) — Watauga
    2839: {"day": "Wednesday", "crew_size": 2, "leader": "Gustavo Aguilar", "budget": 12.0, "name": "Foster Village Park"},
    2838: {"day": "Wednesday", "crew_size": 2, "leader": "Gustavo Aguilar", "budget": 4.0, "name": "Central Fire Station"},
    2840: {"day": "Wednesday", "crew_size": 2, "leader": "Gustavo Aguilar", "budget": 2.0, "name": "Hillview Park"},
    # Wednesday — Jorge (3) — S FW / Crowley / Benbrook / Cresson
    3068: {"day": "Wednesday", "crew_size": 3, "leader": "Jorge Torres Conde", "budget": 5.72, "name": "Dakota Apartments"},
    3027: {"day": "Wednesday", "crew_size": 3, "leader": "Jorge Torres Conde", "budget": 7.34, "name": "Hampton Manor"},
    2446: {"day": "Wednesday", "crew_size": 3, "leader": "Jorge Torres Conde", "budget": 10.0, "name": "BASIS Benbrook"},
    2144: {"day": "Wednesday", "crew_size": 3, "leader": "Jorge Torres Conde", "budget": 8.0, "name": "Bear Creek HOA"},
    # Thursday — Gustavo (3) — Saginaw / Watauga / Irving
    2404: {"day": "Thursday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 5.32, "name": "Miller Milling"},
    2835: {"day": "Thursday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 2.0, "name": "Animal Service Center"},
    2842: {"day": "Thursday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 2.0, "name": "Public Works Facility"},
    2844: {"day": "Thursday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 4.0, "name": "Virgil Anthony Park"},
    2841: {"day": "Thursday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 4.0, "name": "Municipal Complex"},
    2845: {"day": "Thursday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 8.0, "name": "Watauga Community Center"},
    1704: {"day": "Thursday", "crew_size": 3, "leader": "Gustavo Aguilar", "budget": 2.0, "name": "Chick-Fil-A North Irving"},
    # Thursday — Jorge (2) — Mixed
    2055: {"day": "Thursday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 3.0, "name": "Craft Residence"},
    1978: {"day": "Thursday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 10.0, "name": "Parcel B"},
    # Carol Katz uses a different budget field, estimate 2.5
    2270: {"day": "Thursday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 2.5, "name": "Carol Katz Residence"},
    1830: {"day": "Thursday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 10.16, "name": "Five Oaks Crossing"},
    3217: {"day": "Thursday", "crew_size": 2, "leader": "Jorge Torres Conde", "budget": 2.0, "name": "Tom Brown Residence"},
    # Friday — Combined (5 people) — Crowley Creekside
    1602: {"day": "Friday", "crew_size": 5, "leader": "Combined", "budget": 39.0,
           "name": "Crowley Creekside HOA", "target_wall_clock": 6.0, "target_manhours": 30.0},
}

# Maintenance crew leaders to filter on
MAINT_LEADERS = {"Gustavo Aguilar", "Jorge Torres Conde", "Jon Hatcher",
                 "Daniel Garcia Albarran", "Saul Cruz Morales"}

# Thresholds
OVER_BUDGET_PCT = 0.15   # Flag if >15% over budget
OVER_HISTORICAL_PCT = 0.25  # Flag if >25% over historical avg


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --- Aspire API ---

def load_config():
    # Reporting API has broader read access (needed for WorkTickets)
    client_id = os.environ.get("ASPIRE_REPORTING_CLIENT_ID")
    secret = os.environ.get("ASPIRE_REPORTING_SECRET")
    if client_id and secret:
        return {"api_base_url": "https://cloud-api.youraspire.com",
                "api_client_id": client_id, "api_secret": secret}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
            # Prefer reporting credentials for read-only audit
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
        log(f"Query failed: {endpoint} — {e.code}")
        return []


# --- Date Logic ---

def get_previous_workday(ref_date=None):
    """Return the previous workday (skip weekends)."""
    if ref_date is None:
        ref_date = date.today()
    d = ref_date - timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def get_day_name(d):
    return d.strftime("%A")


# --- Core Audit ---

def fetch_completed_tickets(config, token, target_date):
    """Fetch WorkTickets completed on the target date."""
    date_str = target_date.isoformat()
    next_day = (target_date + timedelta(days=1)).isoformat()

    # Completed tickets
    completed = odata_query(config, token, "WorkTickets", {
        "$filter": f"CompleteDate ge {date_str}T00:00:00Z and CompleteDate lt {next_day}T00:00:00Z",
        "$select": "WorkTicketID,OpportunityID,HoursEst,HoursAct,OnSiteHours,CompleteDate,CrewLeaderName,WorkTicketStatusName",
        "$top": "200"
    })

    # Also get scheduled tickets with actual hours (not yet completed but worked)
    scheduled = odata_query(config, token, "WorkTickets", {
        "$filter": f"ScheduledStartDate ge {date_str}T00:00:00Z and ScheduledStartDate lt {next_day}T00:00:00Z and HoursAct gt 0",
        "$select": "WorkTicketID,OpportunityID,HoursEst,HoursAct,OnSiteHours,ScheduledStartDate,CrewLeaderName,WorkTicketStatusName",
        "$top": "200"
    })

    # Merge, dedup by WorkTicketID
    seen = set()
    all_tickets = []
    for t in completed + scheduled:
        wid = t.get("WorkTicketID")
        if wid not in seen:
            seen.add(wid)
            all_tickets.append(t)

    return all_tickets


def fetch_historical(config, token, opp_id, lookback_days=90):
    """Fetch last 90 days of completed tickets for a property to get averages."""
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    tickets = odata_query(config, token, "WorkTickets", {
        "$filter": f"OpportunityID eq {opp_id} and CompleteDate ge {cutoff}T00:00:00Z and HoursAct gt 0",
        "$select": "WorkTicketID,HoursAct,OnSiteHours",
        "$top": "50",
        "$orderby": "CompleteDate desc"
    })
    if not tickets:
        return None
    acts = [t["HoursAct"] for t in tickets if t.get("HoursAct")]
    onsites = [t["OnSiteHours"] for t in tickets if t.get("OnSiteHours")]
    return {
        "count": len(tickets),
        "avg_total": sum(acts) / len(acts) if acts else 0,
        "avg_onsite": sum(onsites) / len(onsites) if onsites else 0,
    }


def analyze_tickets(tickets, target_date, config, token):
    """Analyze tickets and flag anomalies."""
    day_name = get_day_name(target_date)
    findings = []
    summary_rows = []

    # Filter to maintenance-relevant tickets
    maint_tickets = []
    for t in tickets:
        leader = t.get("CrewLeaderName", "")
        opp_id = t.get("OpportunityID")
        # Include if crew leader is maintenance OR if OpportunityID is in our schedule
        if any(name in leader for name in MAINT_LEADERS) or opp_id in SCHEDULE:
            maint_tickets.append(t)

    if not maint_tickets:
        findings.append(("WARNING", "No maintenance tickets found for this day", ""))
        return findings, summary_rows

    for t in maint_tickets:
        opp_id = t.get("OpportunityID")
        hours_act = t.get("HoursAct") or 0
        hours_est = t.get("HoursEst") or 0
        onsite = t.get("OnSiteHours") or 0
        leader = t.get("CrewLeaderName", "Unknown")
        status = t.get("WorkTicketStatusName", "")

        # Skip tickets with no actual hours
        if hours_act <= 0:
            continue

        # Look up schedule info
        sched = SCHEDULE.get(opp_id, {})
        prop_name = sched.get("name", f"Opp #{opp_id}")
        expected_day = sched.get("day", "Unknown")
        expected_crew = sched.get("crew_size", "?")
        budget = sched.get("budget", hours_est)

        # Calculate per-person hours if we know crew size
        per_person = hours_act / expected_crew if isinstance(expected_crew, int) else None
        mobilization = hours_act - onsite if onsite > 0 else None

        # Build summary row
        row = {
            "property": prop_name,
            "leader": leader,
            "budget": budget,
            "actual": hours_act,
            "onsite": onsite,
            "mobilization": mobilization,
            "crew_size": expected_crew,
            "per_person": per_person,
            "flags": []
        }

        # --- Flag checks ---

        # 1. Over budget
        if budget > 0 and hours_act > budget * (1 + OVER_BUDGET_PCT):
            pct = ((hours_act - budget) / budget) * 100
            row["flags"].append(f"OVER BUDGET: {pct:.0f}% over ({hours_act:.1f} vs {budget:.1f} budget)")
            findings.append(("OVER BUDGET", prop_name,
                             f"{hours_act:.1f} actual vs {budget:.1f} budget ({pct:.0f}% over). "
                             f"Crew: {expected_crew}. Per person: {per_person:.1f} hrs." if per_person else ""))

        # 2. Check against historical average
        hist = fetch_historical(config, token, opp_id)
        if hist and hist["avg_total"] > 0:
            if hours_act > hist["avg_total"] * (1 + OVER_HISTORICAL_PCT):
                pct = ((hours_act - hist["avg_total"]) / hist["avg_total"]) * 100
                row["flags"].append(f"ABOVE HISTORICAL: {pct:.0f}% over avg ({hours_act:.1f} vs {hist['avg_total']:.1f} avg)")
                findings.append(("ABOVE HISTORICAL", prop_name,
                                 f"{hours_act:.1f} actual vs {hist['avg_total']:.1f} avg over {hist['count']} visits ({pct:.0f}% over)"))
            row["hist_avg"] = hist["avg_total"]
            row["hist_onsite"] = hist["avg_onsite"]
            row["hist_count"] = hist["count"]

        # 3. Wrong day check
        if expected_day != "Unknown" and expected_day != day_name:
            row["flags"].append(f"WRONG DAY: Expected {expected_day}, worked {day_name}")
            findings.append(("WRONG DAY", prop_name,
                             f"Scheduled for {expected_day} but completed on {day_name}"))

        # 4. Target checks (for combined crew properties)
        target_mh = sched.get("target_manhours")
        if target_mh and onsite > target_mh * 1.15:
            row["flags"].append(f"OVER TARGET: {onsite:.1f} on-site vs {target_mh:.1f} target man-hrs")
            findings.append(("OVER TARGET", prop_name,
                             f"On-site {onsite:.1f} man-hrs vs {target_mh:.1f} target. "
                             f"Wall clock: {onsite/expected_crew:.1f} hrs vs {sched.get('target_wall_clock', '?')} target."))

        # 5. High mobilization
        if mobilization and budget > 0 and mobilization > budget * 0.35:
            mob_pct = (mobilization / hours_act) * 100
            row["flags"].append(f"HIGH MOBILIZATION: {mobilization:.1f} hrs ({mob_pct:.0f}% of total)")

        summary_rows.append(row)

    return findings, summary_rows


# --- Report Generation ---

def build_email(target_date, findings, summary_rows):
    day_name = get_day_name(target_date)
    date_str = target_date.strftime("%B %d, %Y")

    has_flags = any(r["flags"] for r in summary_rows)
    subject_prefix = "⚠️ FLAGS" if has_flags else "✅ OK"
    subject = f"[Crew Audit] {subject_prefix} — {day_name} {target_date.strftime('%m/%d')}"

    # --- HTML ---
    html_parts = [f"""
<h2 style="margin:0">Crew Hours Audit — {day_name}, {date_str}</h2>
<p style="color:#666;margin:4px 0 16px">Reviewed at {datetime.now().strftime('%I:%M %p CT')}</p>
"""]

    # Findings summary
    if findings:
        html_parts.append('<h3 style="color:#c0392b;margin:0 0 8px">⚠️ Flags</h3><ul style="margin:0 0 16px">')
        for severity, prop, detail in findings:
            color = "#c0392b" if severity in ("OVER BUDGET", "OVER TARGET") else "#e67e22" if severity == "ABOVE HISTORICAL" else "#2980b9"
            html_parts.append(f'<li><strong style="color:{color}">[{severity}]</strong> {prop} — {detail}</li>')
        html_parts.append('</ul>')
    else:
        html_parts.append('<p style="color:#27ae60;font-weight:bold">✅ All crews within expected hours.</p>')

    # Detail table
    html_parts.append("""
<table style="border-collapse:collapse;width:100%;font-size:13px;margin-top:12px">
<tr style="background:#2c3e50;color:white">
  <th style="padding:6px 8px;text-align:left">Property</th>
  <th style="padding:6px 8px;text-align:left">Crew Leader</th>
  <th style="padding:6px 8px;text-align:right">Crew</th>
  <th style="padding:6px 8px;text-align:right">Budget</th>
  <th style="padding:6px 8px;text-align:right">Actual</th>
  <th style="padding:6px 8px;text-align:right">On-Site</th>
  <th style="padding:6px 8px;text-align:right">Mobil.</th>
  <th style="padding:6px 8px;text-align:right">Per Person</th>
  <th style="padding:6px 8px;text-align:right">Hist Avg</th>
  <th style="padding:6px 8px;text-align:left">Flags</th>
</tr>
""")

    for i, r in enumerate(summary_rows):
        bg = "#fdf2f2" if r["flags"] else ("#f9f9f9" if i % 2 else "#ffffff")
        flag_text = "<br>".join(f'<span style="color:#c0392b;font-size:11px">{f}</span>' for f in r["flags"]) if r["flags"] else "—"
        hist = f"{r.get('hist_avg', 0):.1f}" if r.get("hist_avg") else "—"
        mob = f"{r['mobilization']:.1f}" if r.get("mobilization") is not None else "—"
        pp = f"{r['per_person']:.1f}" if r.get("per_person") is not None else "—"

        html_parts.append(f"""<tr style="background:{bg}">
  <td style="padding:4px 8px">{r['property']}</td>
  <td style="padding:4px 8px">{r['leader']}</td>
  <td style="padding:4px 8px;text-align:right">{r['crew_size']}</td>
  <td style="padding:4px 8px;text-align:right">{r['budget']:.1f}</td>
  <td style="padding:4px 8px;text-align:right"><strong>{r['actual']:.1f}</strong></td>
  <td style="padding:4px 8px;text-align:right">{r['onsite']:.1f}</td>
  <td style="padding:4px 8px;text-align:right">{mob}</td>
  <td style="padding:4px 8px;text-align:right">{pp}</td>
  <td style="padding:4px 8px;text-align:right">{hist}</td>
  <td style="padding:4px 8px">{flag_text}</td>
</tr>""")

    html_parts.append("</table>")

    # Daily total
    total_actual = sum(r["actual"] for r in summary_rows)
    total_budget = sum(r["budget"] for r in summary_rows)
    html_parts.append(f"""
<p style="margin-top:12px;font-size:13px;color:#555">
  <strong>Day total:</strong> {total_actual:.1f} actual man-hrs vs {total_budget:.1f} budget
  ({((total_actual - total_budget) / total_budget * 100) if total_budget else 0:+.0f}%)
</p>
""")

    # --- Plain text ---
    plain = f"Crew Hours Audit — {day_name}, {date_str}\n{'='*50}\n\n"
    if findings:
        plain += "FLAGS:\n"
        for sev, prop, detail in findings:
            plain += f"  [{sev}] {prop} — {detail}\n"
        plain += "\n"
    else:
        plain += "All crews within expected hours.\n\n"

    for r in summary_rows:
        plain += f"{r['property']}: {r['actual']:.1f} actual vs {r['budget']:.1f} budget"
        if r.get("per_person"):
            plain += f" ({r['per_person']:.1f}/person)"
        if r["flags"]:
            plain += " ⚠️"
        plain += "\n"

    return subject, "".join(html_parts), plain


def send_email(subject, html, plain):
    sender = os.environ.get("GMAIL_EMAIL")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        log("No email credentials — printing report to stdout")
        print(f"\nSubject: {subject}\n")
        print(plain)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Crew Audit", sender))
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, RECIPIENT, msg.as_string())
        log(f"Email sent: {subject}")
        return True
    except Exception as e:
        log(f"Email failed: {e}")
        print(f"\nSubject: {subject}\n")
        print(plain)
        return False


# --- Main ---

def main():
    log("Crew Hours Audit starting")

    config = load_config()
    if not config:
        log("ERROR: No Aspire config found")
        sys.exit(1)

    token = authenticate(config)
    if not token:
        log("ERROR: Aspire auth failed")
        sys.exit(1)
    log("Aspire authenticated")

    if "--test" in sys.argv:
        log("Test mode — API connection OK")
        sys.exit(0)

    # Determine target date
    target_date = None
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        target_date = date.fromisoformat(sys.argv[idx + 1])
    else:
        target_date = get_previous_workday()

    day_name = get_day_name(target_date)
    log(f"Auditing: {day_name}, {target_date.isoformat()}")

    # Fetch tickets
    tickets = fetch_completed_tickets(config, token, target_date)
    log(f"Found {len(tickets)} tickets for {target_date.isoformat()}")

    # Analyze
    findings, summary_rows = analyze_tickets(tickets, target_date, config, token)
    log(f"Analysis complete: {len(findings)} flags, {len(summary_rows)} properties reviewed")

    # Build and send report
    subject, html, plain = build_email(target_date, findings, summary_rows)
    send_email(subject, html, plain)

    # Print summary for GitHub Actions log
    if findings:
        log(f"RESULT: {len(findings)} flags found")
        for sev, prop, detail in findings:
            log(f"  [{sev}] {prop}: {detail}")
    else:
        log("RESULT: All crews within expected hours")


if __name__ == "__main__":
    main()
