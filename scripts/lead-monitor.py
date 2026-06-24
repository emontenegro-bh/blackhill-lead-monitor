#!/usr/bin/env python3
"""Web Form Lead Monitor for Black Hill Landscaping.

Polls the sales@meangreenlawncare.com shared mailbox for web form submissions,
classifies spam vs legitimate leads, sends branded auto-replies, creates Aspire
contacts (Phase 2), adds to Mailchimp nurture sequence, and notifies owners.

Runs every 5 minutes via launchd (local) or GitHub Actions (cloud).

Supports two modes:
  - Local: Reads config from ~/.config/lead-monitor/config.json, uses device code auth
  - Cloud: Reads config from environment variables, uses client credentials auth

Usage:
  python3 scripts/lead-monitor.py            # Normal run
  python3 scripts/lead-monitor.py --dry-run   # Preview without side effects
  python3 scripts/lead-monitor.py --test      # Test Graph API connection
  python3 scripts/lead-monitor.py --verify    # Health check all integrations
"""

import json, os, sys, re, hashlib, signal, smtplib, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- Timeout guard (120s) to prevent hung processes blocking launchd ---
TIMEOUT_SECONDS = 120

def _timeout_handler(signum, frame):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] TIMEOUT: Script exceeded {TIMEOUT_SECONDS}s, exiting.", flush=True)
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(TIMEOUT_SECONDS)

import msal

# --- Mode Detection ---
CLOUD_MODE = bool(os.environ.get("MS_CLIENT_SECRET"))

# --- Paths ---
CONFIG_DIR = os.path.expanduser("~/.config/lead-monitor")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
TOKEN_CACHE_FILE = os.path.join(CONFIG_DIR, "msal_token_cache.json")
STATE_FILE = os.path.join(CONFIG_DIR, "known-emails.json")
LEADS_DIR = os.path.join(CONFIG_DIR, "leads")
SPAM_DIR = os.path.join(CONFIG_DIR, "spam")
LOG_FILE = os.path.join(CONFIG_DIR, "lead-monitor.log")

DRY_RUN = "--dry-run" in sys.argv

if not CLOUD_MODE:
    os.makedirs(LEADS_DIR, exist_ok=True)
    os.makedirs(SPAM_DIR, exist_ok=True)

# --- Logging ---

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if not CLOUD_MODE:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


# --- Config ---

def load_config():
    if CLOUD_MODE:
        return load_config_from_env()
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_config_from_env():
    """Build config from environment variables (GitHub Actions / cloud)."""
    return {
        "microsoft": {
            "client_id": os.environ["MS_CLIENT_ID"],
            "tenant_id": os.environ["MS_TENANT_ID"],
            "client_secret": os.environ["MS_CLIENT_SECRET"],
            "shared_mailbox": os.environ.get("MS_SHARED_MAILBOX", "sales@meangreenlawncare.com"),
            "scopes": ["https://graph.microsoft.com/.default"],
        },
        "polling": {
            "max_messages_per_run": int(os.environ.get("MAX_MESSAGES", "20")),
        },
        "notifications": {
            "lead_recipients": os.environ.get("LEAD_RECIPIENTS", "evelin@blackhilltx.com,denisse@blackhilltx.com").split(","),
            "spam_recipients": os.environ.get("SPAM_RECIPIENTS", "evelin@blackhilltx.com").split(","),
            "from_email": os.environ.get("NOTIFY_FROM_EMAIL", "evelin@blackhilltx.com"),
            "from_name": "Black Hill Lead Monitor",
        },
        "spam": {
            "auto_filter": os.environ.get("SPAM_AUTO_FILTER", "false").lower() == "true",
            "blocked_domains": [],
            "allowed_senders": [],
        },
        "mailchimp": {
            "enabled": bool(os.environ.get("MAILCHIMP_API_KEY")),
            "api_key": os.environ.get("MAILCHIMP_API_KEY", ""),
            "server_prefix": os.environ.get("MAILCHIMP_SERVER", "us20"),
            "list_id": os.environ.get("MAILCHIMP_LIST_ID", ""),
            "tag": "web-lead",
        },
        "hubspot": {
            "enabled": bool(os.environ.get("HUBSPOT_ACCESS_TOKEN")),
            "access_token": os.environ.get("HUBSPOT_ACCESS_TOKEN", ""),
        },
        "aspire": {
            "enabled": bool(os.environ.get("ASPIRE_CLIENT_ID")),
            "api_client_id": os.environ.get("ASPIRE_CLIENT_ID", ""),
            "api_secret": os.environ.get("ASPIRE_SECRET", ""),
        },
        "auto_reply": {
            "enabled": os.environ.get("AUTO_REPLY_ENABLED", "true").lower() == "true",
            "from_name": "Black Hill Landscaping",
            "from_email": os.environ.get("AUTO_REPLY_FROM", "inquiry@blackhilltx.com"),
            "subject": "We received your inquiry - Black Hill Landscaping",
        },
    }


# --- State ---

CLOUD_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "processed-state.json")


def load_state():
    # Cloud mode: persist state in repo's data/ directory
    if CLOUD_MODE:
        if os.path.exists(CLOUD_STATE_FILE):
            try:
                with open(CLOUD_STATE_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_ids": [], "stats": {"total_leads": 0, "total_spam": 0}}


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    # Cap processed_ids at 5000 entries (FIFO)
    if len(state["processed_ids"]) > 5000:
        state["processed_ids"] = state["processed_ids"][-5000:]
    if CLOUD_MODE:
        os.makedirs(os.path.dirname(CLOUD_STATE_FILE), exist_ok=True)
        with open(CLOUD_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    else:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)


# --- Microsoft Graph API ---

def get_graph_token(config):
    ms = config["microsoft"]

    # Cloud mode: client credentials flow (no user interaction needed)
    if CLOUD_MODE:
        return _get_token_client_credentials(ms)

    # Local mode: device code flow with token cache
    return _get_token_device_code(ms)


def _get_token_client_credentials(ms):
    """Get token via client credentials (application permissions, headless)."""
    app = msal.ConfidentialClientApplication(
        ms["client_id"],
        authority=f"https://login.microsoftonline.com/{ms['tenant_id']}",
        client_credential=ms["client_secret"],
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if result and "access_token" in result:
        log("  Auth: client credentials token acquired")
        return result["access_token"]
    error = result.get("error_description", result.get("error", "unknown"))
    log(f"ERROR: Client credentials auth failed: {error}")
    return None


def _get_token_device_code(ms):
    """Get token via device code flow with cached refresh token (local/laptop)."""
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            cache.deserialize(f.read())
    else:
        log("ERROR: No MSAL token cache. Run: python3 ~/.config/lead-monitor/ms-auth-setup.py")
        return None

    app = msal.PublicClientApplication(
        ms["client_id"],
        authority=f"https://login.microsoftonline.com/{ms['tenant_id']}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if not accounts:
        log("ERROR: No accounts in token cache. Re-run ms-auth-setup.py")
        return None

    result = app.acquire_token_silent(ms["scopes"], account=accounts[0])
    if result and "access_token" in result:
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(cache.serialize())
        return result["access_token"]

    log("ERROR: Token refresh failed. Re-run ms-auth-setup.py")
    return None


def graph_request(token, method, url, body=None):
    """Make a Microsoft Graph API request."""
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": 'outlook.body-content-type="text"',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (202, 204):
                return {}
            response_body = resp.read()
            if not response_body:
                return {}
            return json.loads(response_body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:500]
        log(f"ERROR: Graph API {method} {url[:80]} -> {e.code}: {error_body}")
        return None


def fetch_unread_messages(token, config):
    """Fetch unread messages (legacy, used by --test)."""
    mailbox = config["microsoft"]["shared_mailbox"]
    max_msgs = config["polling"].get("max_messages_per_run", 20)
    url = (
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
        f"?$filter=isRead%20eq%20false"
        f"&$select=id,subject,from,receivedDateTime,body,bodyPreview,hasAttachments"
        f"&$orderby=receivedDateTime%20desc"
        f"&$top={max_msgs}"
    )
    result = graph_request(token, "GET", url)
    if result is None:
        return []
    return result.get("value", [])


def fetch_recent_messages(token, config, lookback_minutes=120):
    """Fetch messages received in the last N minutes, regardless of read status.

    This prevents missed leads when someone reads the email before the monitor
    checks (the isRead filter caused leads to be missed).
    """
    mailbox = config["microsoft"]["shared_mailbox"]
    max_msgs = config["polling"].get("max_messages_per_run", 20)
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Query only the Inbox folder to avoid picking up our own Sent Items
    # (auto-replies saved to Sent Items were being re-processed as leads)
    url = (
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/Inbox/messages"
        f"?$filter=receivedDateTime%20ge%20{since}"
        f"&$select=id,subject,from,receivedDateTime,body,bodyPreview,hasAttachments,isRead"
        f"&$orderby=receivedDateTime%20desc"
        f"&$top={max_msgs}"
    )
    result = graph_request(token, "GET", url)
    if result is None:
        return []
    return result.get("value", [])


def mark_as_read(token, config, message_id):
    if DRY_RUN:
        log(f"  DRY RUN: Would mark message as read: {message_id[:20]}...")
        return
    mailbox = config["microsoft"]["shared_mailbox"]
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}"
    graph_request(token, "PATCH", url, {"isRead": True})


def send_auto_reply(token, config, lead):
    if DRY_RUN:
        log(f"  DRY RUN: Would send auto-reply to {lead['email']}")
        return True
    if not config["auto_reply"].get("enabled", True):
        log("  Auto-reply disabled in config.")
        return False

    mailbox = config["microsoft"]["shared_mailbox"]
    from_name = config["auto_reply"].get("from_name", "Black Hill Landscaping")
    from_email = config["auto_reply"].get("from_email", mailbox)
    subject = config["auto_reply"].get("subject", "We received your inquiry - Black Hill Landscaping")

    first_name = lead.get("first_name", "").strip()
    greeting = first_name if first_name else "Hello"

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  body {{ font-family: 'Google Sans', Arial, sans-serif; color: #333; margin: 0; padding: 0; background: #f5f5f5; }}
  .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; }}
  .header {{ background: #000000; padding: 30px; text-align: center; }}
  .header h1 {{ color: #C9A24D; font-family: Georgia, serif; font-size: 22px; margin: 15px 0 5px; }}
  .header p {{ color: #B8B4AA; font-size: 12px; letter-spacing: 2px; margin: 0; }}
  .body {{ padding: 30px; line-height: 1.7; font-size: 15px; color: #333; }}
  .body p {{ margin: 0 0 15px; }}
  .divider {{ border: none; border-top: 1px solid #EFE9DF; margin: 25px 0; }}
  .footer {{ background: #000000; padding: 28px 30px; text-align: center; }}
  .footer a {{ color: #C9A24D; text-decoration: none; }}
  .gold {{ color: #C9A24D; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <img src="https://blackhilllandscaping.com/wp-content/uploads/2026/01/cropped-Black-Hill-Logo-Full-1.png" alt="Black Hill Landscaping" style="max-width: 200px; height: auto;">
  </div>
  <div class="body">
    <p>{greeting},</p>
    <p>Thank you for reaching out. We received your inquiry and a member of our team will be following up shortly.</p>
    <div style="background: #F9F6F0; border-left: 4px solid #C9A24D; padding: 16px 20px; margin: 20px 0; border-radius: 0 4px 4px 0;">
      <p style="margin: 0 0 4px; font-weight: bold; color: #C9A24D; font-size: 14px; letter-spacing: 0.5px;">OUR 5-10 RULE</p>
      <p style="margin: 0; font-size: 14px; color: #444;">Inquiries received before <strong>5 PM</strong> Monday through Friday get a response the same day. After 5 PM or on weekends, we will reach out by <strong>10 AM</strong> the next business day.</p>
    </div>
    <p>In the meantime, if you have photos of the property, specific areas of concern, or timeline details, feel free to reply to this email so we can come prepared.</p>
    <hr class="divider">
    <p style="font-size: 14px; color: #666;">
    <span class="gold">Black Hill Landscaping</span><br>
    <a href="tel:+18179950324" style="color: #C9A24D; text-decoration: none; font-weight: bold;">(817) 995-0324</a><br>
    <a href="https://blackhilllandscaping.com" style="color: #C9A24D;">BlackHillLandscaping.com</a>
    </p>
  </div>
  <div class="footer">
    <p style="margin: 0; font-family: Georgia, serif; font-size: 18px; color: #C9A24D; letter-spacing: 1.5px; font-style: italic;">Where Excellence Takes Root</p>
  </div>
</div>
</body>
</html>"""

    url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": lead["email"]}}],
            "from": {"emailAddress": {"address": from_email, "name": from_name}},
        },
        "saveToSentItems": True,
    }

    result = graph_request(token, "POST", url, payload)
    if result is not None:
        log(f"  Auto-reply sent to {lead['email']}")
        return True
    log(f"  ERROR: Failed to send auto-reply to {lead['email']}")
    return False


# --- Spam Classification ---
# Tuned from real data: Elementor form spam uses fake names, foreign cities,
# BBCode [url=] tags, rambler.ru emails, and no real phone numbers.

SPAM_KEYWORDS = [
    "seo", "backlink", "link building", "domain authority", "page rank",
    "page 1 of google", "search engine optimization", "keyword ranking",
    "guest post", "link exchange", "buy traffic", "web traffic",
]

MARKETING_KEYWORDS = [
    "unsubscribe", "click here to unsubscribe", "opt out", "email marketing",
    "bulk email", "mass email", "email blast", "mailing list",
]

SOLICITATION_KEYWORDS = [
    "virtual assistant", "appointment setting", "drip campaign",
    "fill your calendar", "we provide", "we offer", "prospecting",
    "i tried emailing you", "reaching out here instead",
    # AI traffic / SEO service solicitations (common form spam 2026)
    "visitors to your website", "visitors to your site",
    "traffic to your website", "traffic to your site",
    "ai-optimized", "ai optimized", "ai can fix",
    "missing out on leads", "more affordable than",
    "less than paid ads", "less than paid ad campaigns",
    "high-intent", "location-targeted traffic", "location-specific",
    "start scaling now", "start now to see", "see the impact",
    "keyword and location", "keyword-targeted",
    # YouTube / social media growth solicitations
    "youtube subscribers", "youtube channel", "new subscribers",
    "build a dedicated audience", "growth process",
    "handle the entire", "set one up for you",
    "we help website owners", "we help businesses",
    "guaranteed subscribers", "guarantee 400",
    "monthly subscribers", "real, human subscribers",
    # Generic B2B solicitation phrases
    "i'm reaching out because we", "reaching out because we",
    "we can guarantee", "we specialize in",
    "book a quick call", "book a call",
    "schedule a quick demo", "schedule a demo",
    "free audit", "free consultation",
    "our agency", "our team can help",
    "increase your revenue", "grow your business",
    "lead generation service", "leads for your business",
    # SEO pitch patterns (Massi B. / stepconceptagency 2026-04-08)
    "hurting your rankings", "rankings on google",
    "push you to page 1", "page 1 faster", "get to page 1",
    "low-competition keywords", "low competition keywords",
    "recorded a quick video", "recorded a video",
    "breaking down the issues", "mind if i share",
    "i was checking out", "noticed a few things",
    "missing some opportunities", "missing opportunities",
]

SERVICE_KEYWORDS = [
    "lawn", "tree", "irrigation", "landscape", "landscaping", "drainage",
    "hardscape", "sod", "fertiliz", "weed", "mulch", "patio", "fence",
    "sprinkler", "grading", "retaining wall", "outdoor lighting", "pavers",
    "turf", "mowing", "planting", "trimming", "pruning", "stump",
]

SPAM_EMAIL_DOMAINS = [
    "rambler.ru", "rambler.com", "yandex.ru", "mail.ru",
    "melssa.com", "mailnesia.com", "guerrillamail.com", "tempmail.com",
    "throwaway.email", "sharklasers.com", "grr.la", "guerrillamailblock.com",
    "pokemail.net", "spam4.me", "binkmail.com", "safetymail.info",
    # Junk mail services (Elementor bot submissions 2026-04)
    "polosmail.com",
]

# Known spam contact names (exact match, case-insensitive)
SPAM_NAMES = [
    "susan smith",
]

# Known bot name suffixes (Elementor form bots use Eastern European names + suffixes)
BOT_NAME_SUFFIXES = [
    "gek", "bot", "async", "sync", "dot", "stand", "leakquity",
    "ergyozo", "daync", "nergy", "coin", "cripto", "crypto",
]

# Out-of-state cities (clearly not in the greater Fort Worth service area)
OUT_OF_STATE_CITIES = [
    "las vegas", "los angeles", "new york", "chicago", "miami", "phoenix",
    "seattle", "denver", "portland", "san francisco", "san diego", "boston",
    "atlanta", "orlando", "tampa", "charlotte", "nashville", "detroit",
    "minneapolis", "st louis", "kansas city", "indianapolis", "columbus",
    "pittsburgh", "baltimore", "philadelphia", "houston", "san antonio",
    "austin", "el paso", "lubbock", "amarillo", "corpus christi", "laredo",
    "brownsville", "mcallen", "new orleans", "memphis", "oklahoma city", "tulsa",
]

# Valid US area code first digits (area codes start with 2-9, second digit 0-9)
# We check that the phone starts with a plausible US area code pattern
INVALID_US_PHONE_STARTS = [
    "0", "1",  # US area codes never start with 0 or 1
]

# Money/income scam keywords (common in bot form spam)
MONEY_SCAM_KEYWORDS = [
    "automate your income", "passive income", "monthly income",
    "make money", "earn money", "profit flips", "high-margin",
    "without selling", "without the need for", "investment of",
    "one-time investment", "generates all your content",
    "direct payments", "arbitrage", "does all the heavy lifting",
    "$3,000 monthly", "$2,000 monthly", "$5,000 monthly",
    "$1,000 monthly", "per month income", "daily income",
    # Crypto / trading scams
    "crypto trading", "bitcoin", "forex signal",
    "trading bot", "nft project",
    # AI tool solicitation scams
    "flipninja", "unfair data advantage", "profit flips across",
    "stop guessing", "next high-margin deal",
]

# Country names used as city — bots fill "Madagascar", "Brazil", etc.
COUNTRY_NAMES = [
    "madagascar", "brazil", "nigeria", "india", "pakistan", "bangladesh",
    "indonesia", "philippines", "vietnam", "thailand", "malaysia",
    "china", "japan", "south korea", "taiwan", "russia", "ukraine",
    "poland", "romania", "hungary", "turkey", "egypt", "morocco",
    "south africa", "kenya", "ghana", "cameroon", "senegal",
    "argentina", "colombia", "peru", "chile", "mexico", "canada",
    "united kingdom", "england", "france", "germany", "spain", "italy",
    "netherlands", "belgium", "sweden", "norway", "denmark", "finland",
    "australia", "new zealand", "ireland", "scotland", "wales",
]

# Foreign street address patterns (non-US address formats)
# Checked with "in" (not startswith) to handle house numbers before the prefix
FOREIGN_ADDRESS_PREFIXES = [
    "via ", "rue ", "strasse", "straße", "calle ", "rua ", "ulitsa ",
    "prospekt ", "platz ", "piazza ", "corso ", "viale ",
]

# Video/streaming link patterns in form messages (bots embed promo links)
VIDEO_LINK_PATTERNS = [
    "youtube.com/watch", "youtube.com/shorts/", "youtu.be/",
    "vimeo.com/", "rumble.com/", "bitchute.com/", "odysee.com/",
]

# Bounce-back / NDR sender patterns
BOUNCE_SENDERS = [
    "microsoftexchange", "mailer-daemon", "postmaster",
]

# Texas cities in the DFW service area (lowercase)
DFW_CITIES = [
    "fort worth", "arlington", "keller", "southlake", "grapevine",
    "colleyville", "bedford", "euless", "hurst", "north richland hills",
    "watauga", "haltom city", "richland hills", "benbrook", "white settlement",
    "lake worth", "river oaks", "westover hills", "saginaw", "haslet",
    "roanoke", "trophy club", "westlake", "azle", "weatherford",
    "burleson", "cleburne", "mansfield", "crowley", "joshua", "aledo",
    "granbury", "dallas", "irving", "plano", "frisco", "mckinney",
    "denton", "lewisville", "flower mound", "coppell", "carrollton",
    "the colony", "little elm", "prosper", "celina", "allen", "wylie",
    "garland", "mesquite", "grand prairie", "cedar hill", "desoto",
    "duncanville", "lancaster", "waxahachie", "midlothian", "mineral wells",
]

FORT_WORTH_ZIPS = [
    "76101", "76102", "76103", "76104", "76105", "76106", "76107", "76108",
    "76109", "76110", "76111", "76112", "76113", "76114", "76115", "76116",
    "76117", "76118", "76119", "76120", "76121", "76122", "76123", "76124",
    "76126", "76129", "76130", "76131", "76132", "76133", "76134", "76135",
    "76136", "76137", "76140", "76147", "76148", "76150", "76155", "76161",
    "76162", "76163", "76164", "76177", "76179", "76180", "76181", "76182",
    "76185", "76191", "76192", "76193", "76196", "76197", "76198", "76199",
    "76244", "76248", "76262",
]


def _is_gibberish(text):
    """Detect random/bot-generated text (names, messages, addresses).
    Returns True for strings like 'NATREGTEGH779280NERTHRTYHR' or
    'METYUTYJ779280MAWRERGTRH' that are clearly not human input."""
    if not text or len(text.strip()) < 4:
        return False
    t = text.strip()

    # ALL CAPS with digits mixed in and length > 10 (e.g. "NATREGTEGH779280NERTHRTYHR")
    if len(t) > 10 and t == t.upper() and any(c.isdigit() for c in t):
        return True

    # No spaces in a long string (>15 chars) — human names always have a space
    if " " not in t and len(t) > 15:
        return True

    # Very low vowel ratio in alpha chars (random consonant strings)
    alpha = [c for c in t.lower() if c.isalpha()]
    if len(alpha) > 6:
        vowel_ratio = sum(1 for c in alpha if c in "aeiou") / len(alpha)
        if vowel_ratio < 0.15:
            return True

    # Digits make up >30% of the string and string is >8 chars
    if len(t) > 8:
        digit_ratio = sum(1 for c in t if c.isdigit()) / len(t)
        if digit_ratio > 0.30:
            return True

    return False


def _is_web_form_email(message):
    """Detect if an email is an Elementor web form submission.

    These are also captured by the WhatConverts API monitor, which has
    LLM-based spam filtering. Skip CRM contact creation here to avoid
    duplicates and spam filter bypass (see: imane syano domain-sale spam
    2026-04-22).
    """
    body = message.get("body", {}).get("content", "")
    form = _parse_form_fields(body)
    # Elementor forms include these specific field labels
    form_indicators = ["name", "email", "contact no.", "what type of service do you need?", "city", "zip code"]
    matches = sum(1 for label in form_indicators if label in form)
    return matches >= 3


def _parse_form_fields(body_text):
    """Parse Elementor form body into a dict of field:value pairs."""
    clean = re.sub(r"<[^>]+>", "\n", body_text)
    clean = re.sub(r"&nbsp;|&amp;|&lt;|&gt;", " ", clean)
    # Normalize whitespace but preserve line breaks
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n\s*\n", "\n", clean)

    fields = {}
    # Known Elementor form labels
    labels = [
        "Name", "Email", "Address", "City", "Zip Code", "Contact No.",
        "What Type Of Service Do You Need?",
        "What is Your Preferred Mode Of Communication?",
        "Anything else you would like to share?",
        "Traffic Source", "Date", "Time", "Page URL",
        "User Agent", "Remote IP", "Powered by",
    ]
    # Build pattern: split on known labels
    label_pattern = "|".join(re.escape(l) for l in labels)
    parts = re.split(f"({label_pattern})\\s*:\\s*", clean, flags=re.IGNORECASE)

    # parts = [preamble, label1, value1, label2, value2, ...]
    for i in range(1, len(parts) - 1, 2):
        label = parts[i].strip()
        value = parts[i + 1].strip().strip(",;")
        if value and value != "---":
            fields[label.lower()] = value
    return fields


def classify_email(message, config):
    """Score an email for spam likelihood. Returns (score, reasons).

    Tuned for Elementor form spam: foreign cities, BBCode, no phone,
    Russian email domains, URL shorteners, solicitation pitches.
    """
    subject = (message.get("subject") or "").lower()
    body = (message.get("body", {}).get("content") or "").lower()
    sender = (message.get("from", {}).get("emailAddress", {}).get("address") or "").lower()
    full_text = f"{subject} {body}"

    score = 0
    reasons = []
    # Tripped by any high-confidence spam signal. When True, we skip the
    # "lead signal" credits entirely — a Brazilian bot with a foreign
    # address should not be rehabilitated by "has a dropdown value" or
    # "has 10 digits in a phone field". See Joanna Riggs / video pitch
    # case 2026-04-09 for the original miss.
    hard_spam = False

    # --- Parse form fields for smarter checks ---
    form = _parse_form_fields(message.get("body", {}).get("content", ""))
    form_city = form.get("city", "").lower().strip()
    form_contact = form.get("contact no.", "").strip()
    form_message = form.get("anything else you would like to share?", "").lower()
    form_email = form.get("email", "").lower().strip()
    form_address = form.get("address", "").lower().strip()
    form_traffic = form.get("traffic source", "").lower().strip()

    # Strip HTML tags from body for plain-text pattern matching (fallback when
    # form parsing misses fields due to non-standard HTML structure)
    body_plain = re.sub(r"<[^>]+>", " ", message.get("body", {}).get("content", ""))
    body_plain = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;", " ", body_plain).lower()
    body_plain = re.sub(r"\s+", " ", body_plain)

    # --- HIGH-CONFIDENCE SPAM SIGNALS ---

    # BBCode [url=...] tags — classic forum spam, never from real leads
    if "[url=" in full_text or "[/url]" in full_text:
        score += 5
        hard_spam = True
        reasons.append("BBCode [url=] tags (forum spam)")

    # URL shorteners in message body
    shorteners = ["tiny.cc", "cutt.us", "cutt.ly", "bit.ly", "t.co",
                   "tinyurl.com", "mub.me", "put2.me", "4ty.me", "tt.vg",
                   "citly.me", "rb.gy", "is.gd", "v.gd", "shorturl.at"]
    for s in shorteners:
        if s in full_text:
            score += 4
            hard_spam = True
            reasons.append(f"URL shortener: {s}")
            break

    # Telegram bot links
    if "t.me/" in full_text or "@netxmix" in full_text:
        score += 4
        hard_spam = True
        reasons.append("Telegram bot/channel link")

    # Video/streaming links in message (bots embed promo YouTube links)
    for pattern in VIDEO_LINK_PATTERNS:
        if pattern in full_text or pattern in form_message or pattern in body_plain:
            score += 5
            hard_spam = True
            reasons.append(f"Video link in message: {pattern}")
            break

    # Money/income scam keywords
    for kw in MONEY_SCAM_KEYWORDS:
        if kw in full_text or kw in form_message or kw in body_plain:
            score += 4
            hard_spam = True
            reasons.append(f"Money scam keyword: '{kw}'")
            break

    # Foreign street address format (Via Duomo, 63 Rue de la Paix, etc.)
    # Check form address field AND plain body text as fallback
    for prefix in FOREIGN_ADDRESS_PREFIXES:
        if prefix in form_address or prefix in body_plain:
            score += 3
            hard_spam = True
            reasons.append(f"Foreign address format: '{form_address or prefix}'")
            break

    # Spam email domains in form email field
    for domain in SPAM_EMAIL_DOMAINS:
        if domain in form_email:
            score += 4
            hard_spam = True
            reasons.append(f"Spam email domain: {domain}")
            break

    # Gibberish/random email local part (e.g. "0rq2i@", "x7k3m@")
    if form_email and "@" in form_email:
        local_part = form_email.split("@")[0]
        digit_ratio = sum(1 for c in local_part if c.isdigit()) / max(len(local_part), 1)
        has_no_vowels = not any(c in "aeiou" for c in local_part.lower())
        if len(local_part) <= 6 and digit_ratio >= 0.3 and has_no_vowels:
            score += 4
            reasons.append(f"Gibberish email local part: {local_part}")
        elif len(local_part) <= 3:
            score += 3
            reasons.append(f"Very short email local part: {local_part}")

    # Bot name detection (names ending with known bot suffixes like "LarisaGek")
    form_name = form.get("name", "").strip().lower()
    for suffix in BOT_NAME_SUFFIXES:
        if form_name.endswith(suffix):
            score += 4
            reasons.append(f"Bot name suffix: '{suffix}' in '{form_name}'")
            break

    # Known spam names (exact match)
    for spam_name in SPAM_NAMES:
        if form_name == spam_name:
            score += 10
            hard_spam = True
            reasons.append(f"Blocked name: {spam_name}")
            break

    # Gibberish name (e.g. "NATREGTEGH779280NERTHRTYHR")
    if _is_gibberish(form_name):
        score += 5
        hard_spam = True
        reasons.append(f"Gibberish name: '{form_name[:30]}'")

    # Gibberish message (e.g. "METYUTYJ779280MAWRERGTRH")
    if _is_gibberish(form_message) or _is_gibberish(body_plain[:200]):
        score += 4
        hard_spam = True
        reasons.append("Gibberish message content")

    # Country name used as city (bots fill "Madagascar", "Brazil", etc.)
    if form_city and form_city in COUNTRY_NAMES:
        score += 5
        hard_spam = True
        reasons.append(f"Country name as city: {form_city}")

    # Service field contains label text instead of actual selection (bot didn't use dropdown)
    form_service = form.get("what type of service do you need?", "").strip().lower()
    if form_service in ["type of service you need", "what type of service do you need?",
                         "what type of service do you need", "select", "choose", "--"]:
        score += 3
        reasons.append(f"Service field contains label text: '{form_service}'")

    # Phone field contains @ sign (spammers put email instead of phone)
    if "@" in form_contact:
        score += 3
        reasons.append("Phone field contains email address (not a real phone)")

    # Phone field has wrong digit count (not 10 or 11 digits = not US)
    phone_digits = re.sub(r"\D", "", form_contact)
    if phone_digits and len(phone_digits) not in (10, 11):
        score += 2
        reasons.append(f"Non-US phone number ({len(phone_digits)} digits: {form_contact})")

    # Phone starts with 0 or 1 (not valid US area code) or invalid area code
    if phone_digits and len(phone_digits) >= 10:
        area_code = phone_digits[-10:-7] if len(phone_digits) == 11 else phone_digits[:3]
        if area_code[0] in ("0", "1"):
            score += 3
            hard_spam = True
            reasons.append(f"Invalid US area code: {area_code} (starts with {area_code[0]})")
        elif area_code[1] == "9" and area_code[2] == "0":
            # x90 area codes don't exist in the US
            score += 2
            reasons.append(f"Suspicious area code: {area_code}")

    # 11-digit phone not starting with 1 (US numbers are 10 digits or 1+10)
    if phone_digits and len(phone_digits) == 11 and not phone_digits.startswith("1"):
        score += 3
        hard_spam = True
        reasons.append(f"Non-US phone format: {phone_digits} (11 digits, starts with {phone_digits[0]})")

    # No real US phone number anywhere in body
    us_phone = re.search(r"\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}", body)
    if not us_phone:
        score += 2
        reasons.append("No US phone number found")

    # Non-DFW city (if a city is provided and it's not in our service area)
    if form_city and form_city not in DFW_CITIES:
        # Check if it's at least a US-sounding city (heuristic: not obviously foreign)
        foreign_indicators = [
            "moscow", "basra", "dharan", "lilongwe", "addis ababa", "brcko",
            "molodesjnaja", "duverge", "gilcrest", "pritchett", "kiev",
            "mumbai", "lagos", "nairobi", "bogota", "manila", "karachi",
            # Brazilian cities (Elementor bots commonly use these)
            "campinas", "sao paulo", "são paulo", "rio de janeiro",
            "belo horizonte", "salvador", "brasilia", "fortaleza",
            "curitiba", "recife", "porto alegre",
            # UK towns (SEO solicitation bots)
            "culcheth", "london", "manchester", "birmingham", "leeds",
            "liverpool", "bristol", "edinburgh", "glasgow", "cardiff",
        ]
        if form_city in foreign_indicators:
            score += 4
            hard_spam = True
            reasons.append(f"Foreign city: {form_city}")
        elif form_city in OUT_OF_STATE_CITIES:
            score += 3
            reasons.append(f"Out-of-state city: {form_city}")
        elif not any(form_city == c for c in DFW_CITIES):
            score += 1
            reasons.append(f"Non-DFW city: {form_city}")

    # Non-US zip code (not 5 digits)
    form_zip = form.get("zip code", "").strip()
    if form_zip and not re.match(r"^\d{5}(-\d{4})?$", form_zip):
        score += 3
        hard_spam = True
        reasons.append(f"Non-US zip code: {form_zip}")

    # Cyrillic characters in body
    if re.search(r"[\u0400-\u04FF]", body):
        score += 3
        hard_spam = True
        reasons.append("Cyrillic characters in body")

    # Known spam tool names
    spam_tools = ["xrumer", "gsa ser", "scrapebox", "senuke"]
    for tool in spam_tools:
        if tool in full_text:
            score += 5
            reasons.append(f"Spam tool name: {tool}")
            break

    # Excessive URLs (more than 3)
    url_count = len(re.findall(r"https?://", body))
    if url_count > 5:
        score += 3
        reasons.append(f"Excessive URLs ({url_count})")
    elif url_count > 3:
        score += 2
        reasons.append(f"Multiple URLs ({url_count})")

    # Fake address patterns
    if form_address in ["123 main st", "123 main street", "test", "asdf", "na", "n/a"]:
        score += 3
        reasons.append(f"Fake address: {form_address}")

    # URL in the "message" field (real leads rarely include URLs)
    msg_text = form_message or body_plain
    if msg_text and re.search(r"https?://", msg_text):
        score += 3
        reasons.append("URL found in message field")

    # Message about services WE don't provide (solicitations posing as inquiries)
    # Real leads describe their property/project; spam describes what THEY sell
    selling_phrases = [
        "we help", "we can", "we specialize", "we guarantee",
        "our service", "our team", "our agency",
        "i can help", "i specialize",
        "for [company", "for your business", "for your company",
        "scale your", "automate your", "boost your",
        "secure an unfair", "data advantage", "stop guessing",
        # Cold-outreach templates (2026-04 video pitch case)
        "i just visited", "i came across your", "i was browsing",
        "ever considered", "have you considered", "have you ever",
        "our prices start", "prices start from",
        "samples of our", "samples of previous", "previous work",
        "our videos", "impactful video", "video to advertise",
        "video for your business", "advertise your business",
        "across social media", "on social media",
        "generate impressive results", "impressive results",
        "let me know if you're interested", "let me know if you are interested",
        "if you're interested in seeing",
    ]
    selling_text = form_message or body_plain
    selling_count = sum(1 for p in selling_phrases if p in selling_text)
    if selling_count >= 2:
        score += 4
        hard_spam = True
        reasons.append(f"Message contains selling language ({selling_count} phrases)")
    elif selling_count == 1:
        score += 2
        reasons.append(f"Message contains selling language")

    # Empty city AND empty zip (real leads almost always provide location)
    if not form_city and not form.get("zip code", "").strip():
        score += 1
        reasons.append("No city or zip provided")

    # No name AND no phone = not a real lead (bots often skip both)
    form_name_raw = form.get("name", "").strip()
    if not form_name_raw and not phone_digits:
        score += 3
        reasons.append("No name and no phone (likely bot)")

    # Non-ASCII heavy text (many foreign spam messages)
    non_ascii = sum(1 for c in body_plain if ord(c) > 127)
    if non_ascii > 20:
        score += 3
        reasons.append(f"High non-ASCII character count ({non_ascii})")

    # Disposable / throwaway email providers (expanded list)
    disposable_domains = [
        "mailinator.com", "10minutemail.com", "trashmail.com", "fakeinbox.com",
        "yopmail.com", "dispostable.com", "maildrop.cc", "temp-mail.org",
        "emailondeck.com", "getairmail.com", "mohmal.com", "mailnator.com",
        "getnada.com", "inboxbear.com", "mytemp.email", "spamgourmet.com",
        "mailcatch.com", "trashmail.net", "mintemail.com", "tempr.email",
        "burnermail.io", "discard.email", "mailsac.com", "tmail.com",
    ]
    for d in disposable_domains:
        if d in form_email or d in sender:
            score += 4
            hard_spam = True
            reasons.append(f"Disposable email: {d}")
            break

    # --- MEDIUM SPAM SIGNALS ---

    # SEO/marketing keywords
    for kw in SPAM_KEYWORDS:
        if kw in full_text or kw in body_plain:
            score += 3
            reasons.append(f"SEO keyword: '{kw}'")
            break

    for kw in MARKETING_KEYWORDS:
        if kw in full_text or kw in body_plain:
            score += 3
            reasons.append(f"Marketing keyword: '{kw}'")
            break

    # Business solicitation keywords (check full text, form message, AND plain body)
    for kw in SOLICITATION_KEYWORDS:
        if kw in full_text or kw in form_message or kw in body_plain:
            score += 3
            reasons.append(f"Solicitation: '{kw}'")
            break

    # Blocked domains from config
    blocked_domains = config.get("spam", {}).get("blocked_domains", [])
    for domain in blocked_domains:
        if domain in sender or domain in form_email:
            score += 3
            reasons.append(f"Blocked domain: {domain}")

    # Very short/empty body
    body_text = message.get("body", {}).get("content", "")
    if len(body_text.strip()) < 20:
        score += 2
        reasons.append("Very short or empty body")

    # Reply/forward (not a form submission)
    if subject.startswith("re:") or subject.startswith("fw:"):
        score += 1
        reasons.append("Reply/forward (not a form submission)")

    # No Page URL (real form submissions include the page URL)
    if form_traffic and "google ads" not in form_traffic and not form.get("page url", ""):
        score += 1
        reasons.append("No page URL (bot submission)")

    # --- LEAD SIGNALS (reduce score) ---
    # Gated: only apply these credits if no hard-spam signal fired.
    # Otherwise a Brazilian bot filling the dropdown + a bare phone number
    # can fully offset "foreign address + invalid area code" (Joanna Riggs
    # video pitch case, 2026-04-09).
    if not hard_spam:
        # Google Ads traffic source
        if "google ads" in form_traffic:
            score -= 3
            reasons.append("Traffic from Google Ads")

        # Direct website traffic with page URL
        if form.get("page url", "") and ("blackhilllandscaping.com" in form.get("page url", "") or "meangreenlawncare.com" in form.get("page url", "")):
            score -= 2
            reasons.append("Submitted from website contact page")

        # Fort Worth area references
        if "fort worth" in full_text or "tarrant" in full_text or "dfw" in full_text:
            score -= 2
            reasons.append("Fort Worth area reference")

        # DFW city match
        if form_city in DFW_CITIES:
            score -= 3
            reasons.append(f"DFW city: {form_city}")

        # Fort Worth zip code
        for zip_code in FORT_WORTH_ZIPS:
            if zip_code in body:
                score -= 2
                reasons.append(f"Fort Worth zip: {zip_code}")
                break

        # Service keywords
        for kw in SERVICE_KEYWORDS:
            if kw in full_text:
                score -= 1
                reasons.append(f"Service keyword: '{kw}'")
                break

        # Real US phone number present
        if us_phone:
            score -= 2
            reasons.append("Contains US phone number")
    else:
        reasons.append("Lead credits skipped (hard spam signal fired)")

    # Allowed senders from config — NOT gated by hard_spam because a
    # real explicit allow-list should always win.
    allowed = config.get("spam", {}).get("allowed_senders", [])
    for allowed_sender in allowed:
        if allowed_sender.lower() in sender or allowed_sender.lower() in form_email:
            score -= 10
            reasons.append(f"Allowed sender: {allowed_sender}")

    return score, reasons


# --- Lead Parsing ---
# Uses _parse_form_fields() from the spam classifier section to handle
# Elementor form format: "Label: Value" with known labels.

def detect_service(text):
    """Detect the most likely service interest from text content."""
    text_lower = text.lower()
    service_map = {
        "Lawn Care & Maintenance": ["lawn care", "mowing", "lawn maintenance", "lawn service"],
        "Landscape Design & Installation": ["landscape design", "landscaping", "landscape install"],
        "Sod Installation": ["sod", "new lawn", "sod installation"],
        "Irrigation & Sprinklers": ["irrigation", "sprinkler", "drip system", "watering"],
        "Tree & Plant Services": ["tree", "plant", "shrub", "trimming", "pruning", "stump"],
        "Drainage Solutions": ["drainage", "french drain", "water problem", "standing water", "erosion"],
        "Hardscape & Patios": ["hardscape", "patio", "pavers", "retaining wall", "walkway", "fire pit"],
        "Fertilization & Weed Control": ["fertiliz", "weed", "pre-emergent", "lawn treatment"],
        "Outdoor Lighting": ["lighting", "landscape light", "outdoor light"],
        "Grading & Resloping": ["grading", "reslope", "grade", "leveling"],
        "Mulch & Bed Maintenance": ["mulch", "flower bed", "bed maintenance"],
        "Fence Installation": ["fence", "fencing"],
    }
    for service_name, keywords in service_map.items():
        for kw in keywords:
            if kw in text_lower:
                return service_name
    return "General Inquiry"


def parse_lead(message):
    """Parse an Elementor web form email into a structured lead dict."""
    body = message.get("body", {}).get("content", "")
    subject = message.get("subject", "")
    from_info = message.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    from_name = from_info.get("name", "")
    received = message.get("receivedDateTime", "")

    # Use the Elementor form parser
    form = _parse_form_fields(body)

    # Extract name — split into first/last
    full_name = form.get("name", "").strip()
    first_name = ""
    last_name = ""
    if full_name:
        name_parts = full_name.split(None, 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""

    # Phone: use "Contact No." field, but only if it looks like a phone (not email)
    phone = form.get("contact no.", "").strip()
    if "@" in phone:
        phone = ""  # Spammers put email in phone field

    # Service from form dropdown
    service = form.get("what type of service do you need?", "")
    if not service:
        service = detect_service(f"{subject} {body}")

    # Message from free-text field
    message_text = form.get("anything else you would like to share?", "")
    if not message_text:
        # Fallback: strip HTML and use raw body
        clean_body = re.sub(r"<[^>]+>", " ", body)
        clean_body = re.sub(r"&nbsp;|&amp;|&lt;|&gt;", " ", clean_body)
        clean_body = re.sub(r"\s+", " ", clean_body).strip()
        message_text = clean_body[:500]

    lead = {
        "first_name": first_name,
        "last_name": last_name,
        "email": form.get("email", from_addr).strip(),
        "phone": phone,
        "service_interest": service,
        "address": form.get("address", "").strip(),
        "city": form.get("city", "").strip(),
        "zip": form.get("zip code", "").strip(),
        "message": message_text[:500],
        "source": "web_form",
        "traffic_source": form.get("traffic source", ""),
        "received_at": received,
        "original_subject": subject,
        "message_id": message.get("id", ""),
    }
    return lead


# --- Mailchimp ---

def add_to_mailchimp(config, lead):
    """Add lead to Mailchimp audience with web-lead tag."""
    mc_cfg = config.get("mailchimp", {})
    if not mc_cfg.get("enabled"):
        log("  Mailchimp disabled. Skipping.")
        return None
    if DRY_RUN:
        log(f"  DRY RUN: Would add {lead['email']} to Mailchimp")
        return "dry-run"

    api_key = mc_cfg.get("api_key", "")
    server = mc_cfg.get("server_prefix", "")
    list_id = mc_cfg.get("list_id", "")
    tag = mc_cfg.get("tag", "web-lead")

    if not api_key or not server or not list_id:
        log("  Mailchimp not fully configured (need api_key, server_prefix, list_id). Skipping.")
        return None

    # Upsert subscriber
    email_hash = hashlib.md5(lead["email"].lower().encode()).hexdigest()
    url = f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/members/{email_hash}"

    payload = {
        "email_address": lead["email"],
        "status_if_new": "subscribed",
        "merge_fields": {
            "FNAME": lead.get("first_name", ""),
            "LNAME": lead.get("last_name", ""),
            "PHONE": lead.get("phone", ""),
        },
        "tags": [tag],
    }

    # Add service tag if detected
    service = lead.get("service_interest", "")
    if service and service != "General Inquiry":
        service_tag = re.sub(r"[^a-zA-Z0-9\s]", "", service).lower().replace(" ", "-")
        payload["tags"].append(service_tag)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            status = result.get("status", "unknown")
            log(f"  Mailchimp: {lead['email']} -> {status} (tags: {payload['tags']})")
            return status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:300]
        log(f"  ERROR: Mailchimp API {e.code}: {error_body}")
        return None
    except Exception as e:
        log(f"  ERROR: Mailchimp request failed: {e}")
        return None


# --- HubSpot CRM ---

def create_hubspot_contact(config, lead):
    """Create a contact and deal in HubSpot CRM via API."""
    hubspot_cfg = config.get("hubspot", {})
    if not hubspot_cfg.get("enabled"):
        return None

    # Never create contacts for our own addresses or internal emails
    lead_email = (lead.get("email") or "").lower()
    for addr in OWN_ADDRESSES:
        if addr in lead_email:
            log(f"  Skipping HubSpot: own address ({lead_email})")
            return None
    if lead_email.endswith("@meangreenlawncare.com") or lead_email.endswith("@blackhilltx.com"):
        log(f"  Skipping HubSpot: internal email ({lead_email})")
        return None

    if DRY_RUN:
        desc = lead.get("email") or f"{lead.get('first_name', '')} {lead.get('last_name', '')}"
        log(f"  DRY RUN: Would create HubSpot contact for {desc}")
        return "dry-run"

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "hubspot-sync.py")
    if not os.path.exists(script_path):
        log(f"  ERROR: HubSpot sync script not found at {script_path}")
        return None

    lead_json = json.dumps(lead)
    try:
        import subprocess
        env = os.environ.copy()
        # Support cloud mode: pass token via env if config has it
        hs_token = hubspot_cfg.get("access_token") or os.environ.get("HUBSPOT_ACCESS_TOKEN")
        if hs_token:
            env["HUBSPOT_ACCESS_TOKEN"] = hs_token

        result = subprocess.run(
            ["python3", script_path, "--lead-json", lead_json],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.stdout.strip():
            response = json.loads(result.stdout.strip())
            action = response.get("action", "unknown")

            if action == "created":
                contact_url = response.get("contact_url", "")
                deal_id = response.get("deal_id", "")
                log(f"  HubSpot: Contact + deal created ({contact_url})")
                return contact_url or "created"
            elif action == "exists":
                log(f"  HubSpot: Contact already exists")
                return "exists"
            elif not response.get("success"):
                log(f"  HubSpot: {response.get('message', 'Unknown error')}")
                return None
            else:
                log(f"  HubSpot: {response.get('message', 'Done')}")
                return action
        else:
            stderr_tail = (result.stderr or "")[-300:]
            log(f"  ERROR: HubSpot sync returned no output. stderr: {stderr_tail}")
            return None

    except subprocess.TimeoutExpired:
        log("  ERROR: HubSpot sync timed out (30s)")
        return None
    except Exception as e:
        log(f"  ERROR: HubSpot sync failed: {e}")
        return None


# --- Aspire CRM ---

def create_aspire_contact(config, lead):
    """Create a contact in Aspire CRM via REST API."""
    aspire_cfg = config.get("aspire", {})
    if not aspire_cfg.get("enabled"):
        return None

    if DRY_RUN:
        desc = lead.get("email") or f"{lead.get('first_name','')} {lead.get('last_name','')}"
        log(f"  DRY RUN: Would create Aspire contact for {desc}")
        return "dry-run"

    # Call the API sync script as a subprocess
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "aspire-api-sync.py")
    if not os.path.exists(script_path):
        log(f"  ERROR: Aspire API script not found at {script_path}")
        return None

    # Pass credentials via env vars if available (cloud), otherwise script reads config file
    env = os.environ.copy()
    if aspire_cfg.get("api_client_id"):
        env["ASPIRE_CLIENT_ID"] = aspire_cfg["api_client_id"]
    if aspire_cfg.get("api_secret"):
        env["ASPIRE_SECRET"] = aspire_cfg["api_secret"]

    lead_json = json.dumps(lead)
    try:
        import subprocess
        result = subprocess.run(
            ["python3", script_path, "--lead-json", lead_json],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.stdout.strip():
            response = json.loads(result.stdout.strip())
            action = response.get("action", "unknown")
            success = response.get("success", False)

            if action == "created":
                contact_url = response.get("contact_url", "")
                log(f"  Aspire: Contact created ({contact_url})")
                return contact_url or "created"
            elif action == "exists":
                log(f"  Aspire: Contact already exists")
                return "exists"
            elif not success:
                log(f"  Aspire: {response.get('message', 'Unknown error')}")
                return None
            else:
                log(f"  Aspire: {response.get('message', 'Done')}")
                return action
        else:
            stderr_tail = (result.stderr or "")[-300:]
            log(f"  ERROR: Aspire API script returned no output. stderr: {stderr_tail}")
            return None

    except subprocess.TimeoutExpired:
        log("  ERROR: Aspire contact creation timed out (30s)")
        return None
    except Exception as e:
        log(f"  ERROR: Aspire contact creation failed: {e}")
        return None


# --- Notifications ---

# Users to @mention in the "Leads Alert" Teams channel. A mention the channel
# can't resolve makes Power Automate's "post card" action fail the whole post,
# so only known channel members are mentioned; anyone else degrades to plain
# text. Both lead owners (Evelin, Denisse) are set up on the channel. Add more
# via the TEAMS_MENTION_EMAILS env var (comma-separated).
TEAMS_MENTIONABLE = {
    e.strip().lower()
    for e in ("evelin@blackhilltx.com,denisse@blackhilltx.com," + os.environ.get("TEAMS_MENTION_EMAILS", "")).split(",")
    if e.strip()
}


def _mention_or_text(name, email):
    """Return (display_text, entities) for an optional Teams @mention.

    Only mentions users known to be in the channel; everyone else degrades to a
    plain name so the flow never fails trying to resolve an unknown mention.
    """
    name = name or "Team"
    email = (email or "").strip().lower()
    if email in TEAMS_MENTIONABLE:
        text = f"<at>{name}</at>"
        return text, [{"type": "mention", "text": text,
                       "mentioned": {"id": email, "name": name}}]
    return name, []


def _post_teams_card(webhook_url, card):
    """POST an adaptive card to the Power Automate webhook, with retry.

    Retries transient failures (timeouts, 5xx, 429); does not retry other 4xx
    (a bad payload won't fix itself on retry). On failure raises with the
    server's error body so the real Power Automate reason lands in the logs.
    """
    data = json.dumps(card).encode("utf-8")
    last_err = "unknown error"
    for attempt in range(3):
        try:
            req = urllib.request.Request(webhook_url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {body}".strip()
            if not (e.code == 429 or 500 <= e.code < 600):
                break  # non-retryable client error
        except Exception as e:
            last_err = str(e)
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err)


def send_teams_notification(lead, lead_type="lead", aspire_id=None, hubspot_id=None):
    """Send lead alert to Microsoft Teams via Power Automate webhook."""
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not webhook_url or DRY_RUN:
        if DRY_RUN:
            log("  DRY RUN: Would send Teams notification")
        return

    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Unknown"
    phone = lead.get("phone", "Not provided")
    email = lead.get("email", "Not provided")
    service = lead.get("service_interest", "General Inquiry")
    message = (lead.get("message", "") or "")[:300]
    address_line = " ".join(
        p for p in (str(lead.get(k, "") or "").strip() for k in ("address", "city", "state", "zip")) if p
    ) or "Not provided"

    aspire_text = f"Contact ID: {aspire_id}" if aspire_id else "Not added"
    hubspot_text = f"Deal: {hubspot_id}" if hubspot_id else "Not added"

    # Email leads are always assigned to Evelin; @mention only if a channel member
    mention_text, mention_entities = _mention_or_text("Evelin", "evelin@blackhilltx.com")

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "Container",
                        "style": "emphasis",
                        "items": [{
                            "type": "TextBlock",
                            "text": f"New Lead: {name}",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "Good"
                        }]
                    },
                    {
                        "type": "TextBlock",
                        "text": f"Assigned to: {mention_text}",
                        "weight": "Bolder",
                        "spacing": "Small"
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {"title": "Phone", "value": phone},
                            {"title": "Email", "value": email},
                            {"title": "Address", "value": address_line},
                            {"title": "Service", "value": service},
                            {"title": "Source", "value": "Email Inquiry"},
                        ]
                    },
                    {
                        "type": "TextBlock",
                        "text": f"**Message:** {message}" if message else "*(no message)*",
                        "wrap": True,
                        "spacing": "Medium"
                    },
                    {
                        "type": "TextBlock",
                        "text": f"Aspire: {aspire_text} | HubSpot: {hubspot_text}",
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "Small"
                    }
                ],
                "msteams": {"entities": mention_entities}
            }
        }]
    }

    try:
        status = _post_teams_card(webhook_url, card)
        log(f"  Teams notification sent ({status})")
    except Exception as e:
        log(f"  WARNING: Teams notification failed (non-fatal): {e}")


def send_notification(config, subject, body, recipients):
    """Send email notification via Gmail SMTP."""
    if DRY_RUN:
        log(f"  DRY RUN: Would notify {recipients}: {subject}")
        return

    gmail_user = os.environ.get("GMAIL_EMAIL", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not (gmail_user and gmail_pass):
        gmail_config = os.path.expanduser("~/.config/gmail-sender/config.json")
        if os.path.exists(gmail_config):
            with open(gmail_config) as f:
                creds = json.load(f)
            gmail_user = gmail_user or creds.get("email", "")
            gmail_pass = gmail_pass or creds.get("app_password", "")

    if not (gmail_user and gmail_pass):
        log("  No Gmail SMTP credentials. Notification skipped.")
        return

    from_name = config["notifications"].get("from_name", "Black Hill Lead Monitor")

    msg = MIMEMultipart()
    msg["From"] = f"{from_name} <{gmail_user}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, recipients, msg.as_string())
        log(f"  Notification sent to {recipients}")
    except Exception as e:
        log(f"  ERROR: Gmail SMTP notification failed: {e}")


def notify_new_lead(config, lead, aspire_id=None, hubspot_id=None, mailchimp_status=None):
    """Notify owners of a new legitimate lead."""
    recipients = config["notifications"].get("lead_recipients", [])
    if not recipients:
        return

    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Unknown"
    service = lead.get("service_interest", "General Inquiry")

    subject = f"New Web Lead: {name} - {service}"

    hubspot_line = f"HubSpot: {hubspot_id}" if hubspot_id else "HubSpot: Not configured"
    aspire_line = f"Aspire Contact ID: {aspire_id}" if aspire_id else "Aspire: Not configured"
    mc_line = f"Mailchimp: {mailchimp_status}" if mailchimp_status else "Mailchimp: Not configured"

    body = f"""New Web Form Lead
{'=' * 45}

Name:    {name}
Email:   {lead.get('email', '')}
Phone:   {lead.get('phone', 'Not provided')}
Service: {service}
Address: {lead.get('address', 'Not provided')}
City:    {lead.get('city', '')} {lead.get('zip', '')}

Message:
{lead.get('message', '(no message)')[:400]}

{'=' * 45}
{hubspot_line}
{aspire_line}
{mc_line}
Auto-reply: Sent
Source: {lead.get('original_subject', '')}
Received: {lead.get('received_at', '')}
"""
    send_notification(config, subject, body, recipients)


def notify_lead_reply(config, lead):
    """Notify owners when a lead replies to the auto-reply with project details."""
    recipients = config["notifications"].get("lead_recipients", [])
    if not recipients:
        return

    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Unknown"

    subject = f"Lead Reply: {name}"

    body = f"""Lead Reply to Auto-Reply
{'=' * 45}

Name:    {name}
Email:   {lead.get('email', '')}
Phone:   {lead.get('phone', 'Not provided')}

Message:
{lead.get('message', '(no message)')[:600]}

{'=' * 45}
This is a reply to our auto-reply, not a new lead.
No auto-reply was sent. No Mailchimp or Aspire action taken.
Received: {lead.get('received_at', '')}
"""
    send_notification(config, subject, body, recipients)


def notify_suspected_spam(config, message, score, reasons):
    """Notify Evelin only about suspected spam (training mode)."""
    recipients = config["notifications"].get("spam_recipients", [])
    if not recipients:
        return

    subject_line = message.get("subject", "(no subject)")
    sender = message.get("from", {}).get("emailAddress", {}).get("address", "unknown")
    body_preview = message.get("bodyPreview", "")[:300]

    subject = f"SUSPECTED SPAM (score {score}): {subject_line[:50]}"

    reasons_text = "\n".join(f"  - {r}" for r in reasons)

    body = f"""Suspected Spam Email
{'=' * 45}

From:    {sender}
Subject: {subject_line}
Score:   {score} (3+ = spam)

Score breakdown:
{reasons_text}

Preview:
{body_preview}

{'=' * 45}
This email was NOT auto-replied to.
If this is a real lead, it has NOT been processed.
To change spam rules, edit ~/.config/lead-monitor/config.json
"""
    send_notification(config, subject, body, recipients)


# --- Lead Storage ---

def save_lead_file(lead, classification):
    """Save lead/spam data as JSON file (local) or log to stdout (cloud)."""
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name_slug = re.sub(r"[^a-zA-Z0-9]", "-", f"{lead.get('first_name', '')}-{lead.get('last_name', '')}").strip("-").lower()
    if not name_slug:
        name_slug = "unknown"
    name_slug = name_slug[:40]

    if CLOUD_MODE:
        log(f"  [{classification.upper()}] {lead.get('first_name', '')} {lead.get('last_name', '')} | {lead.get('email', '')} | {lead.get('phone', '')} | {lead.get('service_interest', '')}")
        return None

    target_dir = LEADS_DIR if classification == "lead" else SPAM_DIR
    filename = f"{ts}_{name_slug}.json"
    filepath = os.path.join(target_dir, filename)

    lead_data = {**lead, "classification": classification, "saved_at": datetime.now().isoformat()}

    if not DRY_RUN:
        with open(filepath, "w") as f:
            json.dump(lead_data, f, indent=2)
    log(f"  Saved {classification} to {filename}")
    return filepath


# --- Anti-Loop Protection ---

NO_REPLY_PATTERNS = [
    r"noreply@", r"no-reply@", r"donotreply@", r"do-not-reply@",
    r"mailer-daemon@", r"postmaster@", r"bounce@",
]

# Own addresses that should never be processed as leads or replied to
OWN_ADDRESSES = [
    "inquiry@blackhilltx.com",
    "sales@meangreenlawncare.com",
    "info@meangreenlawncare.com",
    "info@blackhilltx.com",
]


def is_own_email(message, config):
    """Check if this message was sent by the monitor itself (auto-reply loop).

    Returns True if the message should be skipped entirely.
    Uses multiple detection layers because relay addresses may differ
    from the configured from_email in the Graph API envelope.
    """
    sender = (message.get("from", {}).get("emailAddress", {}).get("address") or "").lower()
    sender_name = (message.get("from", {}).get("emailAddress", {}).get("name") or "").lower()
    subject = (message.get("subject") or "").lower()
    body = (message.get("body", {}).get("content") or "").lower()
    mailbox = config["microsoft"]["shared_mailbox"].lower()
    auto_reply_subject = config.get("auto_reply", {}).get("subject", "").lower()
    auto_reply_from = config.get("auto_reply", {}).get("from_email", "").lower()

    # Skip emails FROM our own auto-reply address (exact or partial match)
    if auto_reply_from and auto_reply_from in sender:
        return True

    # Skip emails FROM any of our own addresses (partial match for relay wrappers)
    for addr in OWN_ADDRESSES:
        if addr in sender:
            return True

    # Skip emails FROM the shared mailbox itself
    if mailbox in sender:
        return True

    # Skip emails whose subject matches our auto-reply subject (exact or as reply)
    if auto_reply_subject and auto_reply_subject in subject:
        return True

    # Skip emails whose body contains our auto-reply signature text
    # This catches auto-replies even when sender/subject don't match (relay routing)
    auto_reply_fingerprints = [
        "our 5-10 rule",
        "inquiries received before 5 pm",
        "a member of our team will be following up shortly",
    ]
    for fingerprint in auto_reply_fingerprints:
        if fingerprint in body:
            return True

    return False


def should_auto_reply(lead, config):
    """Check if we should send an auto-reply (prevent loops)."""
    email = lead.get("email", "").lower()
    mailbox = config["microsoft"]["shared_mailbox"].lower()

    # Never reply to ourselves
    if email == mailbox:
        return False

    # Never reply to our own addresses
    for addr in OWN_ADDRESSES:
        if email == addr:
            return False

    # Never reply to no-reply addresses
    for pattern in NO_REPLY_PATTERNS:
        if re.search(pattern, email):
            return False

    # Never reply if no valid email
    if not email or "@" not in email:
        return False

    return True


# --- Main Processing ---

def process_messages(token, config, state):
    """Main processing loop.

    Fetches recent messages (last 2 hours) regardless of read status, then
    skips any already in processed_ids. This prevents missed leads when someone
    reads the email in Outlook before the 5-minute monitor cycle.
    """
    messages = fetch_recent_messages(token, config, lookback_minutes=120)
    if not messages:
        log("No recent messages found.")
        return

    log(f"Found {len(messages)} recent message(s)")
    auto_filter = config.get("spam", {}).get("auto_filter", False)

    for msg in messages:
        msg_id = msg.get("id", "")
        subject = msg.get("subject", "(no subject)")
        sender = msg.get("from", {}).get("emailAddress", {}).get("address", "unknown")

        # De-duplicate
        if msg_id in state["processed_ids"]:
            log(f"  Skipping (already processed): {subject[:50]}")
            continue

        log(f"\nProcessing: {subject[:60]} (from: {sender})")

        # Own-email detection (auto-reply loop prevention)
        if is_own_email(msg, config):
            log(f"  Skipping: Own email / auto-reply loop (from: {sender})")
            mark_as_read(token, config, msg_id)
            state["processed_ids"].append(msg_id)
            continue

        # Bounce-back / NDR detection (skip entirely, no auto-reply)
        is_bounce = False
        sender_lower = sender.lower()
        subject_lower = subject.lower()
        for bp in BOUNCE_SENDERS:
            if bp in sender_lower:
                is_bounce = True
                break
        if "undeliverable" in subject_lower or ("delivery" in subject_lower and "failed" in subject_lower):
            is_bounce = True
        if is_bounce:
            log(f"  Skipping: Bounce-back / NDR email")
            mark_as_read(token, config, msg_id)
            state["processed_ids"].append(msg_id)
            state["stats"]["total_spam"] = state["stats"].get("total_spam", 0) + 1
            continue

        # Classify
        score, reasons = classify_email(msg, config)
        is_spam = score >= 3

        if is_spam:
            log(f"  Classification: SPAM (score {score})")
            for r in reasons:
                log(f"    {r}")

            if auto_filter:
                # Auto-filter mode: silently archive
                lead = parse_lead(msg)
                save_lead_file(lead, "spam")
                state["stats"]["total_spam"] = state["stats"].get("total_spam", 0) + 1
            else:
                # Training mode: notify Evelin, don't process as lead
                lead = parse_lead(msg)
                save_lead_file(lead, "spam")
                notify_suspected_spam(config, msg, score, reasons)
                state["stats"]["total_spam"] = state["stats"].get("total_spam", 0) + 1

            mark_as_read(token, config, msg_id)
            state["processed_ids"].append(msg_id)
            continue

        # Check if this is a reply to our auto-reply
        auto_reply_subject = config["auto_reply"].get("subject", "We received your inquiry - Black Hill Landscaping")
        is_reply = subject.lower().startswith("re:") and auto_reply_subject.lower() in subject.lower()

        if is_reply:
            log(f"  Classification: LEAD REPLY (to auto-reply)")
            lead = parse_lead(msg)
            log(f"  Parsed: {lead['first_name']} {lead['last_name']} | {lead['email']}")

            save_lead_file(lead, "lead")
            notify_lead_reply(config, lead)

            mark_as_read(token, config, msg_id)
            state["processed_ids"].append(msg_id)
            continue

        # Legitimate lead (new)
        log(f"  Classification: LEAD (score {score})")
        if reasons:
            for r in reasons:
                log(f"    {r}")
        lead = parse_lead(msg)
        log(f"  Parsed: {lead['first_name']} {lead['last_name']} | {lead['email']} | {lead['phone']} | {lead['service_interest']}")

        # Web form submissions are handled by the WhatConverts API monitor
        # (which has LLM spam filtering). Skip CRM creation here to avoid
        # duplicates and spam filter bypass.
        if _is_web_form_email(msg):
            log("  SKIP CRM: Web form lead — handled by WhatConverts monitor")
            save_lead_file(lead, "lead")
            mark_as_read(token, config, msg_id)
            state["processed_ids"].append(msg_id)
            continue

        # Save lead file
        save_lead_file(lead, "lead")

        # Send auto-reply
        if should_auto_reply(lead, config):
            send_auto_reply(token, config, lead)
        else:
            log(f"  Skipping auto-reply (no-reply address or loop risk)")

        # Mailchimp
        mc_status = add_to_mailchimp(config, lead)

        # Aspire CRM (runs first so HubSpot note can include status)
        aspire_id = create_aspire_contact(config, lead)

        # HubSpot CRM
        lead["_aspire_status"] = aspire_id or "not_created"
        hubspot_id = create_hubspot_contact(config, lead)

        # Notify owners
        notify_new_lead(config, lead, aspire_id=aspire_id, hubspot_id=hubspot_id, mailchimp_status=mc_status)

        # Teams push notification for instant mobile alert
        send_teams_notification(lead, aspire_id=aspire_id, hubspot_id=hubspot_id)

        # Mark as read
        mark_as_read(token, config, msg_id)

        # Update state
        state["processed_ids"].append(msg_id)
        state["stats"]["total_leads"] = state["stats"].get("total_leads", 0) + 1
        state["stats"]["last_lead"] = datetime.now().isoformat()


# --- CLI Modes ---

def test_connection(config):
    """Test Microsoft Graph API connectivity."""
    log("Testing Microsoft Graph API connection...")
    token = get_graph_token(config)
    if not token:
        log("FAILED: Could not acquire Graph token")
        sys.exit(1)

    log("Token acquired successfully")
    messages = fetch_unread_messages(token, config)
    log(f"Mailbox accessible: {len(messages)} unread message(s)")

    for msg in messages[:3]:
        subject = msg.get("subject", "(no subject)")[:50]
        sender = msg.get("from", {}).get("emailAddress", {}).get("address", "?")
        log(f"  {sender[:30]} | {subject}")

    log("\nConnection test PASSED")


def verify_health(config):
    """Verify all integration health."""
    log("Running health check...\n")
    issues = []

    # Token cache
    if os.path.exists(TOKEN_CACHE_FILE):
        log("[OK] MSAL token cache exists")
    else:
        log("[FAIL] MSAL token cache missing")
        issues.append("Run: python3 ~/.config/lead-monitor/ms-auth-setup.py")

    # Graph token
    token = get_graph_token(config)
    if token:
        log("[OK] Graph token refreshed")
    else:
        log("[FAIL] Graph token refresh failed")
        issues.append("Re-authenticate with ms-auth-setup.py")

    # Mailbox access
    if token:
        messages = fetch_unread_messages(token, config)
        log(f"[OK] Shared mailbox accessible ({len(messages)} unread)")

    # Gmail SMTP
    gmail_user = os.environ.get("GMAIL_EMAIL", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (gmail_user and gmail_pass):
        gmail_config = os.path.expanduser("~/.config/gmail-sender/config.json")
        if os.path.exists(gmail_config):
            log("[OK] Gmail SMTP credentials file exists")
        else:
            log("[WARN] No Gmail SMTP credentials — notifications won't send")
    else:
        log("[OK] Gmail SMTP credentials set via environment")

    # Mailchimp
    mc = config.get("mailchimp", {})
    if mc.get("enabled"):
        if mc.get("api_key") and mc.get("server_prefix") and mc.get("list_id"):
            log("[OK] Mailchimp configured")
        else:
            log("[WARN] Mailchimp enabled but incomplete config")
            issues.append("Set mailchimp.api_key, server_prefix, and list_id in config.json")
    else:
        log("[INFO] Mailchimp disabled")

    # HubSpot
    hubspot = config.get("hubspot", {})
    if hubspot.get("enabled"):
        hs_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "hubspot-sync.py")
        if os.path.exists(hs_script):
            log("[OK] HubSpot sync script exists")
        else:
            log("[WARN] HubSpot enabled but sync script missing")
            issues.append(f"Create {hs_script}")
        hs_config = os.path.expanduser("~/.config/hubspot/config.json")
        has_token = bool(hubspot.get("access_token") or os.environ.get("HUBSPOT_ACCESS_TOKEN") or os.path.exists(hs_config))
        if has_token:
            log("[OK] HubSpot credentials available")
        else:
            log("[WARN] HubSpot enabled but no access token found")
            issues.append("Set HUBSPOT_ACCESS_TOKEN env var or create ~/.config/hubspot/config.json")
    else:
        log("[INFO] HubSpot disabled")

    # Aspire
    aspire = config.get("aspire", {})
    if aspire.get("enabled"):
        if aspire.get("api_client_id"):
            log("[OK] Aspire API credentials configured")
        else:
            log("[WARN] Aspire enabled but no API credentials")
            issues.append("Set ASPIRE_CLIENT_ID and ASPIRE_SECRET env vars")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "aspire-api-sync.py")
        if os.path.exists(script):
            log("[OK] Aspire API sync script exists")
        else:
            log("[WARN] Aspire API script missing")
            issues.append(f"Create {script}")
    else:
        log("[INFO] Aspire disabled")

    # Directories
    log(f"[OK] Leads dir: {LEADS_DIR}")
    log(f"[OK] Spam dir: {SPAM_DIR}")

    # State
    state = load_state()
    log(f"[OK] State: {state['stats'].get('total_leads', 0)} leads, {state['stats'].get('total_spam', 0)} spam processed")

    if issues:
        log(f"\n{len(issues)} issue(s) to resolve:")
        for i, issue in enumerate(issues, 1):
            log(f"  {i}. {issue}")
    else:
        log("\nAll checks passed.")


# --- Entry Point ---

def main():
    config = load_config()

    if "--test" in sys.argv:
        test_connection(config)
        return

    if "--verify" in sys.argv:
        verify_health(config)
        return

    if DRY_RUN:
        log("=== DRY RUN MODE ===")

    log("Lead monitor starting...")
    token = get_graph_token(config)
    if not token:
        sys.exit(1)

    state = load_state()

    try:
        process_messages(token, config, state)
    except Exception as e:
        log(f"ERROR: Unhandled exception: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        if not DRY_RUN:
            save_state(state)

    log("Lead monitor complete.")
    signal.alarm(0)  # Cancel timeout on clean exit


if __name__ == "__main__":
    main()
