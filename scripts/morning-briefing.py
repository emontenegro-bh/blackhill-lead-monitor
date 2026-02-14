#!/usr/bin/env python3
"""Morning briefing for Black Hill Landscaping.

Pulls calendar events, Google Ads data, calculates drive times,
and sends an iMessage to both phones at 6am.

Runs via launchd Sun-Fri at 6:00am.

Usage: python3 scripts/morning-briefing.py [--test]
"""

import json, os, subprocess, warnings, sys
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import msal

# --- Config ---
ADS_CONFIG = os.path.expanduser("~/.config/google-ads/config.json")
BRIEFING_CONFIG = os.path.expanduser("~/.config/morning-briefing/config.json")

with open(BRIEFING_CONFIG) as f:
    briefing_cfg = json.load(f)

PHONES = [briefing_cfg["phones"]["personal"], briefing_cfg["phones"]["work"]]
OFFICE_ADDRESS = briefing_cfg["office_address"]

# --- Calendar via Microsoft Graph API ---
MS_TOKEN_CACHE = os.path.expanduser("~/.config/morning-briefing/msal_token_cache.json")
MS_SCOPES = ["Calendars.Read"]
CENTRAL_TIME = timezone(timedelta(hours=-6))


def _get_graph_token():
    """Acquire a Microsoft Graph token silently from the MSAL cache."""
    ms_cfg = briefing_cfg.get("microsoft", {})
    client_id = ms_cfg.get("client_id", "")
    tenant_id = ms_cfg.get("tenant_id", "")
    if not client_id or client_id == "YOUR_CLIENT_ID_HERE":
        return None

    cache = msal.SerializableTokenCache()
    if os.path.exists(MS_TOKEN_CACHE):
        with open(MS_TOKEN_CACHE) as f:
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

    result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
    if result and "access_token" in result:
        # Save refreshed cache
        with open(MS_TOKEN_CACHE, "w") as f:
            f.write(cache.serialize())
        return result["access_token"]
    return None


def get_todays_events():
    """Fetch today's calendar events from Microsoft Outlook via Graph API."""
    try:
        token = _get_graph_token()
        if not token:
            return [{"time": "", "title": "(Calendar: run ms-auth-setup.py)", "location": ""}]

        import urllib.request
        now = datetime.now(CENTRAL_TIME)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        end = now.replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")

        url = (
            f"https://graph.microsoft.com/v1.0/me/calendarview"
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
                    # Trim excess decimals for Python 3.9 fromisoformat compat
                    clean_dt = start_dt.split(".")[0]
                    t = datetime.fromisoformat(clean_dt)
                    time_str = t.strftime("%-I:%M%p")
                except Exception:
                    time_str = start_dt[11:16]
            else:
                time_str = "All day"

            subject = evt.get("subject", "(no title)")
            loc = evt.get("location", {}).get("displayName", "")

            # Use Graph's isOnlineMeeting for reliable virtual detection,
            # plus fall back to body text scanning
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


VIRTUAL_KEYWORDS = ["zoom.us", "teams.microsoft", "meet.google", "webex",
                     "microsoft teams", "teams meeting", "zoom meeting"]

def _is_virtual(location):
    """Check if a location is a virtual meeting (URL or known platform)."""
    loc = location.lower()
    return loc.startswith("http") or any(kw in loc for kw in VIRTUAL_KEYWORDS)


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


# --- Drive time via OSRM (free routing API) ---
def get_drive_time(from_addr, to_addr):
    """Get drive time in minutes using OpenStreetMap + OSRM."""
    try:
        import urllib.request, urllib.parse
        # Use Open Source Routing Machine (free, no API key)
        # First geocode the addresses
        from_enc = urllib.parse.quote(from_addr)
        to_enc = urllib.parse.quote(to_addr)

        # Geocode origin
        url = f"https://nominatim.openstreetmap.org/search?q={from_enc}&format=json&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "BlackHillBriefing/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            from_geo = json.loads(resp.read())[0]
        from_lon, from_lat = from_geo["lon"], from_geo["lat"]

        # Geocode destination
        url = f"https://nominatim.openstreetmap.org/search?q={to_enc}&format=json&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "BlackHillBriefing/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            to_geo = json.loads(resp.read())[0]
        to_lon, to_lat = to_geo["lon"], to_geo["lat"]

        # Get route from OSRM
        url = f"https://router.project-osrm.org/route/v1/driving/{from_lon},{from_lat};{to_lon},{to_lat}?overview=false"
        req = urllib.request.Request(url, headers={"User-Agent": "BlackHillBriefing/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            route = json.loads(resp.read())
        duration_sec = route["routes"][0]["duration"]
        return round(duration_sec / 60)
    except Exception:
        return None


# --- Google Ads ---
def get_ads_data():
    """Pull yesterday and MTD Google Ads metrics."""
    try:
        with open(ADS_CONFIG) as f:
            config = json.load(f)

        credentials = {
            "developer_token": config["developer_token"],
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": config["refresh_token"],
            "login_customer_id": config["login_customer_id"],
            "use_proto_plus": True,
        }

        client = GoogleAdsClient.load_from_dict(credentials)
        ga_service = client.get_service("GoogleAdsService")
        customer_id = config["customer_id"]

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


# --- Send iMessage ---
def send_imessage(phone, message):
    """Send an iMessage via AppleScript."""
    # Escape special characters for AppleScript
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{phone}" of targetService
        send "{escaped}" to targetBuddy
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


# --- Build the briefing ---
def build_briefing():
    now = datetime.now()
    header = now.strftime("6am Briefing — %a %b %-d")

    # Calendar events
    events = get_todays_events()
    event_lines = []
    for evt in events:
        loc = evt["location"]
        line = f"{evt['time']} - {evt['title']}"
        if loc and _is_virtual(loc):
            line += " (Virtual)"
        elif loc:
            drive = get_drive_time(OFFICE_ADDRESS, loc)
            if drive:
                line += f" @ {loc} ({drive} min drive)"
            else:
                line += f" @ {loc}"
        event_lines.append(line)

    # Ads data
    ads = get_ads_data()

    # Assemble
    parts = [header, ""]
    if event_lines:
        parts.extend(event_lines)
    else:
        parts.append("No meetings today")
    parts.append("")
    parts.append(ads)

    return "\n".join(parts)


# --- Main ---
if __name__ == "__main__":
    test_mode = "--test" in sys.argv
    briefing = build_briefing()

    print(briefing)

    if test_mode:
        print("\n[TEST MODE — not sending texts]")
    else:
        for phone in PHONES:
            ok = send_imessage(phone, briefing)
            status = "sent" if ok else "FAILED"
            print(f"iMessage to {phone}: {status}")

    # Save to file for reference
    data_file = os.path.expanduser("~/.config/morning-briefing/data.txt")
    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    with open(data_file, "w") as f:
        f.write(briefing)
