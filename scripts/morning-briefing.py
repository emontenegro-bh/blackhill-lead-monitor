#!/usr/bin/env python3
"""Morning briefing for Black Hill Landscaping.

Pulls calendar events, Google Ads data, calculates drive times,
and delivers via SMS (Twilio).

Runs via GitHub Actions Sun-Fri at 6am CST, or locally with --test.

Supports two modes:
  - Local: Reads config from ~/.config/morning-briefing/config.json, uses MSAL cache
  - Cloud: Reads config from environment variables, uses client credentials

Usage:
  python3 scripts/morning-briefing.py            # Normal run
  python3 scripts/morning-briefing.py --test      # Build & print, don't send
"""

import json, os, sys, base64, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

CENTRAL_TIME = timezone(timedelta(hours=-6))

# --- Mode Detection ---
CLOUD_MODE = bool(os.environ.get("TWILIO_ACCOUNT_SID"))
TEST_MODE = "--test" in sys.argv

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
        "twilio": {
            "account_sid": os.environ.get("TWILIO_ACCOUNT_SID", ""),
            "auth_token": os.environ.get("TWILIO_AUTH_TOKEN", ""),
            "from_number": os.environ.get("TWILIO_FROM_NUMBER", ""),
        },
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
            response = ga_service.search(customer_id=customer_id, query=query)
            for row in response:
                m = row.metrics
                spend = m.cost_micros / 1_000_000
                cpl = m.cost_per_conversion / 1_000_000 if m.conversions > 0 else 0
                return {"spend": spend, "impressions": m.impressions,
                        "clicks": m.clicks, "leads": m.conversions, "cpl": cpl}
            return {"spend": 0, "impressions": 0, "clicks": 0, "leads": 0, "cpl": 0}

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


# --- Send SMS via Twilio ---

def send_sms(phone, message):
    """Send an SMS via Twilio REST API (no SDK needed)."""
    if CLOUD_MODE:
        twilio = CFG["twilio"]
    else:
        twilio = CFG.get("twilio", {})

    account_sid = twilio.get("account_sid", "")
    auth_token = twilio.get("auth_token", "")
    from_number = twilio.get("from_number", "")

    if not all([account_sid, auth_token, from_number]):
        print(f"Twilio not configured, skipping SMS to {phone}", file=sys.stderr)
        return False

    # Normalize phone to E.164 format
    clean = phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
    if not clean.startswith("+"):
        clean = "+1" + clean

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({
        "To": clean,
        "From": from_number,
        "Body": message,
    }).encode()

    req = urllib.request.Request(url, data=data)
    cred = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            sid = result.get("sid", "unknown")
            print(f"SMS sent to {phone} (SID: {sid})")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Twilio error ({phone}): {e.code} - {body}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"SMS error ({phone}): {e}", file=sys.stderr)
        return False


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

    # Assemble
    parts = [header, ""]
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

    phones = CFG.get("phones", {})
    phone_list = [p for p in [phones.get("personal"), phones.get("work")] if p]

    if not phone_list:
        print("No phone numbers configured", file=sys.stderr)
        sys.exit(1)

    success = False
    for phone in phone_list:
        if send_sms(phone, briefing):
            success = True

    if not success:
        print("FAILED: No SMS sent successfully", file=sys.stderr)
        sys.exit(1)

    print("Morning briefing sent successfully")
