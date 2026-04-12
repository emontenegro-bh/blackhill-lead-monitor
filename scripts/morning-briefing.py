#!/usr/bin/env python3
"""Morning briefing for Black Hill Landscaping.

Pulls calendar events, Google Ads data, calculates drive times,
and delivers via SMS using email-to-SMS gateways (SendGrid).

Runs via GitHub Actions Sun-Fri at 6am CST, or locally with --test.

Supports two modes:
  - Local: Reads config from ~/.config/morning-briefing/config.json, uses MSAL cache
  - Cloud: Reads config from environment variables, uses client credentials

Usage:
  python3 scripts/morning-briefing.py            # Normal run
  python3 scripts/morning-briefing.py --test      # Build & print, don't send
"""

import json, os, sys, base64, smtplib, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

CENTRAL_TIME = timezone(timedelta(hours=-6))

# --- Mode Detection ---
CLOUD_MODE = bool(os.environ.get("SENDGRID_API_KEY"))
TEST_MODE = "--test" in sys.argv

# --- Email-to-SMS carrier gateways ---
CARRIER_GATEWAYS = {
    "tmobile": "tmomail.net",
    "att": "txt.att.net",
    "verizon": "vtext.com",
    "sprint": "messaging.sprintpcs.com",
}

# --- Config ---

def load_config():
    if CLOUD_MODE:
        return load_config_from_env()
    config_path = os.path.expanduser("~/.config/morning-briefing/config.json")
    with open(config_path) as f:
        return json.load(f)


def load_config_from_env():
    return {
        "office_address": os.environ.get("OFFICE_ADDRESS", "230 S Grants Ln, White Settlement, TX"),
        "phones": {
            "personal": os.environ.get("PHONE_PERSONAL", ""),
            "work": os.environ.get("PHONE_WORK", ""),
        },
        "microsoft": {
            "client_id": os.environ.get("MS_CLIENT_ID", ""),
            "tenant_id": os.environ.get("MS_TENANT_ID", ""),
            "client_secret": os.environ.get("MS_CLIENT_SECRET", ""),
            "user_email": os.environ.get("MS_USER_EMAIL", ""),
        },
        "google_ads": {
            "developer_token": os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", ""),
            "client_id": os.environ.get("GOOGLE_ADS_CLIENT_ID", ""),
            "client_secret": os.environ.get("GOOGLE_ADS_CLIENT_SECRET", ""),
            "refresh_token": os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", ""),
            "login_customer_id": os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""),
            "customer_id": os.environ.get("GOOGLE_ADS_CUSTOMER_ID", ""),
        },
        "sendgrid_api_key": os.environ.get("SENDGRID_API_KEY", ""),
        "sms_from_email": os.environ.get("SMS_FROM_EMAIL", "briefing@blackhilltx.com"),
        "sms_recipients": [
            {"phone": os.environ.get("PHONE_PERSONAL", ""), "carrier": "tmobile"},
            {"phone": os.environ.get("PHONE_WORK", ""), "carrier": "att"},
        ],
    }


CFG = load_config()


# --- Calendar via Microsoft Graph API ---

VIRTUAL_KEYWORDS = ["zoom.us", "teams.microsoft", "meet.google", "webex",
                     "microsoft teams", "teams meeting", "zoom meeting"]


def _get_graph_token_cloud():
    """Acquire token using client credentials flow (cloud/GitHub Actions)."""
    ms = CFG["microsoft"]
    url = f"https://login.microsoftonline.com/{ms['tenant_id']}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "client_id": ms["client_id"],
        "client_secret": ms["client_secret"],
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    return result["access_token"]


def _get_graph_token_local():
    """Acquire token using MSAL cache (local/laptop)."""
    try:
        import msal
        ms = CFG.get("microsoft", {})
        client_id = ms.get("client_id", "")
        tenant_id = ms.get("tenant_id", "")
        if not client_id:
            return None

        cache_path = os.path.expanduser("~/.config/morning-briefing/msal_token_cache.json")
        cache = msal.SerializableTokenCache()
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cache.deserialize(f.read())
        else:
            return None

        app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(["Calendars.Read"], account=accounts[0])
        if result and "access_token" in result:
            with open(cache_path, "w") as f:
                f.write(cache.serialize())
            return result["access_token"]
    except Exception as e:
        print(f"Local token error: {e}", file=sys.stderr)
    return None


def _get_graph_token():
    if CLOUD_MODE:
        return _get_graph_token_cloud()
    return _get_graph_token_local()


def _time_sort_key(evt):
    """Convert 8:30AM / 3:00PM to minutes since midnight for sorting."""
    t = evt["time"].upper()
    try:
        is_pm = "PM" in t
        t = t.replace("AM", "").replace("PM", "")
        h, m = t.split(":")
        h, m = int(h), int(m)
        if is_pm and h != 12:
            h += 12
        if not is_pm and h == 12:
            h = 0
        return h * 60 + m
    except Exception:
        return 9999


def _is_virtual(location):
    loc = location.lower()
    return loc.startswith("http") or any(kw in loc for kw in VIRTUAL_KEYWORDS)


def get_todays_events():
    """Fetch today's calendar events from Microsoft Outlook via Graph API."""
    try:
        token = _get_graph_token()
        if not token:
            return [{"time": "", "title": "(Calendar unavailable)", "location": ""}]

        now = datetime.now(CENTRAL_TIME)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        end = now.replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")

        # Cloud mode: access specific user's calendar; local mode: access /me
        if CLOUD_MODE:
            user_email = CFG["microsoft"].get("user_email", "")
            base = f"https://graph.microsoft.com/v1.0/users/{user_email}/calendarview"
        else:
            base = "https://graph.microsoft.com/v1.0/me/calendarview"

        url = (
            f"{base}"
            f"?startdatetime={start}&enddatetime={end}"
            f"&$select=subject,start,end,location,isOnlineMeeting,isAllDay,isCancelled,bodyPreview"
            f"&$orderby=start/dateTime"
            f"&$top=25"
        )

        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Prefer": 'outlook.timezone="Central Standard Time"',
        })

        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        events = []
        for evt in data.get("value", []):
            if evt.get("isCancelled"):
                continue
            start_dt = evt["start"].get("dateTime", "")
            is_all_day = evt.get("isAllDay", False)
            if is_all_day:
                time_str = "All day"
            elif start_dt:
                try:
                    clean_dt = start_dt.split(".")[0]
                    t = datetime.fromisoformat(clean_dt)
                    time_str = t.strftime("%-I:%M%p")
                except Exception:
                    time_str = start_dt[11:16]
            else:
                time_str = "All day"

            subject = evt.get("subject", "(no title)")
            loc = evt.get("location", {}).get("displayName", "")

            if evt.get("isOnlineMeeting"):
                loc = "Virtual"
            elif not loc:
                body = (evt.get("bodyPreview") or "").lower()
                if any(kw in body for kw in VIRTUAL_KEYWORDS):
                    loc = "Virtual"

            events.append({"time": time_str, "title": subject, "location": loc})

        events.sort(key=_time_sort_key)
        return events
    except Exception as e:
        return [{"time": "", "title": f"(Calendar error: {e})", "location": ""}]


# --- Drive time via OSRM ---

def get_drive_time(from_addr, to_addr):
    """Get drive time in minutes using OpenStreetMap + OSRM."""
    try:
        from_enc = urllib.parse.quote(from_addr)
        to_enc = urllib.parse.quote(to_addr)

        def geocode(enc):
            url = f"https://nominatim.openstreetmap.org/search?q={enc}&format=json&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "BlackHillBriefing/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())[0]

        from_geo = geocode(from_enc)
        to_geo = geocode(to_enc)

        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{from_geo['lon']},{from_geo['lat']};{to_geo['lon']},{to_geo['lat']}?overview=false")
        req = urllib.request.Request(url, headers={"User-Agent": "BlackHillBriefing/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            route = json.loads(resp.read())
        return round(route["routes"][0]["duration"] / 60)
    except Exception:
        return None


# --- Google Ads ---

def get_ads_data():
    """Pull yesterday and MTD Google Ads metrics."""
    try:
        if CLOUD_MODE:
            ads = CFG["google_ads"]
            credentials = {
                "developer_token": ads["developer_token"],
                "client_id": ads["client_id"],
                "client_secret": ads["client_secret"],
                "refresh_token": ads["refresh_token"],
                "login_customer_id": ads["login_customer_id"],
                "use_proto_plus": True,
            }
            customer_id = ads["customer_id"]
        else:
            ads_config_path = os.path.expanduser("~/.config/google-ads/config.json")
            with open(ads_config_path) as f:
                config = json.load(f)
            credentials = {
                "developer_token": config["developer_token"],
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "refresh_token": config["refresh_token"],
                "login_customer_id": config["login_customer_id"],
                "use_proto_plus": True,
            }
            customer_id = config["customer_id"]

        from google.ads.googleads.client import GoogleAdsClient
        client = GoogleAdsClient.load_from_dict(credentials)
        ga_service = client.get_service("GoogleAdsService")

        now = datetime.now()
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        mtd_start = now.strftime("%Y-%m-01")
        mtd_end = now.strftime("%Y-%m-%d")

        def query_metrics(date_clause):
            query = f"""
                SELECT
                    metrics.cost_micros, metrics.impressions,
                    metrics.clicks, metrics.conversions,
                    metrics.cost_per_conversion
                FROM customer
                WHERE segments.date {date_clause}
            """
            last_err = None
            for attempt in range(4):
                try:
                    response = ga_service.search(
                        customer_id=customer_id, query=query, timeout=30
                    )
                    for row in response:
                        m = row.metrics
                        spend = m.cost_micros / 1_000_000
                        cpl = m.cost_per_conversion / 1_000_000 if m.conversions > 0 else 0
                        return {"spend": spend, "impressions": m.impressions,
                                "clicks": m.clicks, "leads": m.conversions, "cpl": cpl}
                    return {"spend": 0, "impressions": 0, "clicks": 0, "leads": 0, "cpl": 0}
                except Exception as e:
                    last_err = e
                    err_str = str(e).lower()
                    if any(k in err_str for k in ["503", "timeout", "timed out", "unavailable"]):
                        wait = 2 ** (attempt + 1)
                        print(f"Google Ads API retry {attempt + 1}/3 after {wait}s: {e}", file=sys.stderr)
                        time.sleep(wait)
                        continue
                    raise
            raise last_err

        y = query_metrics(f"= '{yesterday}'")
        mtd = query_metrics(f"BETWEEN '{mtd_start}' AND '{mtd_end}'")

        y_cpl = f"${y['cpl']:.0f} CPL" if y["leads"] > 0 else "0 leads"
        mtd_cpl = f"${mtd['cpl']:.2f} CPL" if mtd["leads"] > 0 else "0 leads"

        lines = [
            f"Ads yesterday: ${y['spend']:.0f} | {y['clicks']} clicks | {y['leads']:.0f} leads | {y_cpl}",
            f"Ads MTD: ${mtd['spend']:.0f} | {mtd['impressions']:,} imp | {mtd['leads']:.0f} leads | {mtd_cpl}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Ads: Error - {e}"


# --- Check-in reminders ---

def get_checkin_reminder():
    """Check if today matches a scheduled marketing check-in date."""
    try:
        # Look for the state file relative to the script or in the repo
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "post-launch-checkins.json"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".claude", "states", "post-launch-checkins.json"),
        ]
        state = None
        for path in candidates:
            if os.path.exists(path):
                with open(path) as f:
                    state = json.load(f)
                break
        if not state:
            return None

        today = datetime.now(CENTRAL_TIME).strftime("%Y-%m-%d")
        for checkin in state.get("checkins", []):
            if checkin["date"] == today and checkin["status"] == "pending":
                return (
                    f">>> MARKETING CHECK-IN TODAY: {checkin['name']} <<<\n"
                    f"Focus: {checkin['focus']}"
                )
    except Exception:
        pass
    return None


# --- Send SMS via email-to-SMS gateway (SendGrid SMTP) ---

def send_sms(phone, carrier, message):
    """Send an SMS via carrier email-to-SMS gateway using SendGrid SMTP."""
    gateway = CARRIER_GATEWAYS.get(carrier)
    if not gateway:
        print(f"Unknown carrier '{carrier}' for {phone}", file=sys.stderr)
        return False

    api_key = CFG.get("sendgrid_api_key", "")
    from_email = CFG.get("sms_from_email", "briefing@blackhilltx.com")
    if not api_key:
        print("SendGrid API key not configured", file=sys.stderr)
        return False

    # Build email-to-SMS address
    clean = phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "").replace("+1", "").replace("+", "")
    to_email = f"{clean}@{gateway}"

    msg = MIMEText(message)
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = "Briefing"

    try:
        with smtplib.SMTP("smtp.sendgrid.net", 587, timeout=15) as server:
            server.starttls()
            server.login("apikey", api_key)
            server.sendmail(from_email, [to_email], msg.as_string())
        print(f"SMS sent to {phone} via {gateway}")
        return True
    except Exception as e:
        print(f"SMS error ({phone}): {e}", file=sys.stderr)
        return False


# --- System Health ---

def get_system_health():
    """Check GitHub Actions workflow health for the last 24 hours."""
    gh_pat = os.environ.get("GH_PAT", "")
    if not gh_pat:
        return "Systems: (no GH_PAT configured)"

    repos = [
        ("emontenegro-bh/blackhill-lead-monitor", "lead-monitor"),
        ("emontenegro-bh/blackhill-ops-dashboard", "ops-dashboard"),
    ]
    failures = []
    total_workflows = 0

    for repo, short_name in repos:
        try:
            url = f"https://api.github.com/repos/{repo}/actions/runs?status=failure&per_page=20"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {gh_pat}",
                "Accept": "application/vnd.github+json",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            # Filter to last 24h
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            for run in data.get("workflow_runs", []):
                if run.get("created_at", "") > cutoff:
                    wf_name = run.get("name", "unknown")
                    created = run.get("created_at", "")[:16].replace("T", " ")
                    failures.append(f"  - {wf_name} ({created})")

            # Count total workflows in repo
            wf_url = f"https://api.github.com/repos/{repo}/actions/workflows"
            wf_req = urllib.request.Request(wf_url, headers={
                "Authorization": f"Bearer {gh_pat}",
                "Accept": "application/vnd.github+json",
            })
            with urllib.request.urlopen(wf_req, timeout=15) as resp:
                wf_data = json.loads(resp.read())
            total_workflows += wf_data.get("total_count", 0)
        except Exception as e:
            failures.append(f"  - {short_name}: health check error ({e})")

    if failures:
        # Deduplicate (same workflow can fail multiple times)
        unique = list(dict.fromkeys(failures))
        return f"ALERT: {len(unique)} failure(s) in 24h\n" + "\n".join(unique[:5])
    else:
        return f"Systems: All OK ({total_workflows} workflows green)"


# --- Build the briefing ---

def build_briefing():
    now = datetime.now(CENTRAL_TIME)
    header = now.strftime("6am Briefing - %a %b %-d")
    office = CFG.get("office_address", "230 S Grants Ln, White Settlement, TX")

    # Calendar events
    events = get_todays_events()
    event_lines = []
    for evt in events:
        loc = evt["location"]
        line = f"{evt['time']} - {evt['title']}"
        if loc and _is_virtual(loc):
            line += " (Virtual)"
        elif loc:
            drive = get_drive_time(office, loc)
            if drive:
                line += f" @ {loc} ({drive} min drive)"
            else:
                line += f" @ {loc}"
        event_lines.append(line)

    # Ads data
    ads = get_ads_data()

    # Check-in reminders
    checkin = get_checkin_reminder()

    # System health
    health = get_system_health()

    # Sunday reminder
    sunday_reminder = ""
    if now.weekday() == 6:  # Sunday
        sunday_reminder = ">>> Run /skill-discovery today <<<"

    # Assemble
    parts = [header, ""]
    parts.append(health)
    parts.append("")
    if sunday_reminder:
        parts.append(sunday_reminder)
        parts.append("")
    if checkin:
        parts.append(checkin)
        parts.append("")
    if event_lines:
        parts.extend(event_lines)
    else:
        parts.append("No meetings today")
    parts.append("")
    parts.append(ads)

    return "\n".join(parts)


# --- Main ---

if __name__ == "__main__":
    briefing = build_briefing()
    print(briefing)

    if TEST_MODE:
        print("\n[TEST MODE - not sending]")
        sys.exit(0)

    recipients = CFG.get("sms_recipients", [])
    if not recipients:
        # Fall back to phones dict for local mode
        phones = CFG.get("phones", {})
        if phones.get("personal"):
            recipients.append({"phone": phones["personal"], "carrier": "tmobile"})
        if phones.get("work"):
            recipients.append({"phone": phones["work"], "carrier": "att"})

    if not recipients:
        print("No phone numbers configured", file=sys.stderr)
        sys.exit(1)

    success = False
    for r in recipients:
        if r.get("phone") and send_sms(r["phone"], r["carrier"], briefing):
            success = True

    if not success:
        print("SMS delivery failed — falling back to email", file=sys.stderr)
        success = send_email_fallback(briefing)

    if not success:
        print("FAILED: Neither SMS nor email delivered", file=sys.stderr)
        sys.exit(1)

    print("Morning briefing sent successfully")


def send_email_fallback(message):
    """Send briefing via Gmail SMTP when SMS fails."""
    gmail_email = os.environ.get("GMAIL_EMAIL", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_email or not gmail_password:
        print("Gmail credentials not configured for email fallback", file=sys.stderr)
        return False

    from email.mime.multipart import MIMEMultipart
    from email.utils import formataddr

    now = datetime.now(CENTRAL_TIME)
    subject = f"Morning Briefing — {now.strftime('%A %b %-d')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Assistant", gmail_email))
    msg["To"] = "evelin@blackhilltx.com"

    # Plain text version
    msg.attach(MIMEText(message, "plain"))
    # Simple HTML version (preserve line breaks)
    html = f"<pre style='font-family:system-ui;font-size:14px;line-height:1.5'>{message}</pre>"
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(gmail_email, gmail_password)
            server.sendmail(gmail_email, ["evelin@blackhilltx.com"], msg.as_string())
        print("Briefing sent via email fallback")
        return True
    except Exception as e:
        print(f"Email fallback error: {e}", file=sys.stderr)
        return False
