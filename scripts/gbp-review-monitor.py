#!/usr/bin/env python3
"""GBP Review Monitor - polls for new reviews, drafts responses, emails Evelin.

Runs every 15 minutes via launchd. Detects new reviews, selects a response
template based on star rating, stores draft in pending-responses/, and sends
an email notification.

Usage:
  python3 scripts/gbp-review-monitor.py           # Normal run
  python3 scripts/gbp-review-monitor.py --dry-run  # Preview without email
"""

import json, os, sys, random, smtplib, hashlib, uuid, time
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Add scripts dir to path for gbp_auth import
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

_CLOUD_MODE = bool(os.environ.get("GBP_CLIENT_ID"))
if _CLOUD_MODE:
    _DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gbp")
    CONFIG_DIR = _DATA_DIR
else:
    CONFIG_DIR = os.path.expanduser("~/.config/gbp")
KNOWN_FILE = os.path.join(CONFIG_DIR, "known-reviews.json")
PENDING_DIR = os.path.join(CONFIG_DIR, "pending-responses")
TEMPLATE_FILE = os.path.join(os.path.dirname(__file__), "gbp-review-templates.json")
EMAIL_ADDRESS = "evelin@blackhilltx.com"
REPLY_TO_ADDRESS = "monte24negro+gbp@gmail.com"
SENDGRID_SMTP = "smtp.sendgrid.net"
SENDGRID_PORT = 587
SENDGRID_KEY_FILE = os.path.expanduser("~/.config/sendgrid-api-key")
LOG_FILE = os.path.join(CONFIG_DIR, "monitor.log")
GMAIL_SENDER_CONFIG = os.path.expanduser("~/.config/gmail-sender/config.json")
GMAIL_SMTP = "smtp.gmail.com"
GMAIL_PORT = 587

DRY_RUN = "--dry-run" in sys.argv

os.makedirs(PENDING_DIR, exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_known():
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE) as f:
            return json.load(f)
    return {"review_ids": [], "last_check": None}


def save_known(data):
    data["last_check"] = datetime.now().isoformat()
    with open(KNOWN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_templates():
    with open(TEMPLATE_FILE) as f:
        return json.load(f)


def detect_service(review_text):
    """Detect which service the review mentions for template personalization."""
    text = review_text.lower()
    keywords = {
        "lawn": ["lawn", "mow", "grass", "yard", "turf", "weed"],
        "tree": ["tree", "trim", "prune", "stump", "branch", "removal"],
        "irrigation": ["irrigation", "sprinkler", "water", "drain", "drainage"],
        "hardscape": ["patio", "stone", "retaining wall", "walkway", "hardscape", "pavers"],
        "design": ["design", "plan", "blueprint", "consultation"],
        "install": ["install", "plant", "sod", "mulch", "flower bed"],
        "commercial": ["commercial", "office", "business", "parking lot"],
    }
    for service, words in keywords.items():
        if any(w in text for w in words):
            return service
    return "default"


def draft_response(review, templates):
    """Select and personalize a response template."""
    stars = review.get("starRating", "FIVE")
    star_map = {"FIVE": "5_star", "FOUR": "4_star", "THREE": "3_star", "TWO": "2_star", "ONE": "1_star"}
    key = star_map.get(stars, "5_star")

    options = templates.get(key, templates["5_star"])
    template = random.choice(options)

    reviewer_name = review.get("reviewer", {}).get("displayName", "there")
    first_name = reviewer_name.split()[0] if reviewer_name and reviewer_name != "A Google User" else "there"

    comment = review.get("comment", "")
    service = detect_service(comment)
    service_ref = templates["service_references"].get(service, templates["service_references"]["default"])

    response = template["template"].replace("{name}", first_name).replace("{service_reference}", service_ref)

    return {
        "template_id": template["id"],
        "response_text": response,
        "service_detected": service,
    }


def _build_mime(subject, body, from_addr, to_addr, reply_to=None):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(body, "plain"))
    return msg


def _send_via_sendgrid(msg, api_key):
    """Try SendGrid SMTP with 3 retries and exponential backoff."""
    for attempt in range(3):
        try:
            with smtplib.SMTP(SENDGRID_SMTP, SENDGRID_PORT, timeout=10) as server:
                server.starttls()
                server.login("apikey", api_key)
                server.sendmail(msg["From"], msg["To"], msg.as_string())
            log("Email sent via SendGrid.")
            return True
        except Exception as e:
            wait = 2 ** (attempt + 1)
            log(f"SendGrid attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(wait)
    return False


def _send_via_ms_graph(subject, body, to_addr, reply_to=None):
    """Primary: send via Microsoft Graph API (M365 mailbox - reliable delivery to blackhilltx.com)."""
    client_id = os.environ.get("MS_CLIENT_ID", "")
    client_secret = os.environ.get("MS_CLIENT_SECRET", "")
    tenant_id = os.environ.get("MS_TENANT_ID", "")
    mailbox = os.environ.get("MS_USER_EMAIL", "")

    if not (client_id and client_secret and tenant_id and mailbox):
        log("MS Graph credentials not configured.")
        return False

    try:
        # Get token via client credentials flow
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        token_data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }).encode()
        req = urllib.request.Request(token_url, data=token_data)
        resp = urllib.request.urlopen(req, timeout=10)
        access_token = json.loads(resp.read())["access_token"]

        # Send via Graph
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_addr}}],
            },
            "saveToSentItems": True,
        }
        if reply_to:
            payload["message"]["replyTo"] = [{"emailAddress": {"address": reply_to}}]

        url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        log(f"Email sent via MS Graph (from {mailbox}).")
        return True
    except urllib.error.HTTPError as e:
        log(f"MS Graph send failed: {e.code} {e.read().decode()[:200]}")
        return False
    except Exception as e:
        log(f"MS Graph send failed: {e}")
        return False


def _send_via_gmail_smtp(subject, body, to_addr, reply_to=None):
    """Fallback: send via Gmail SMTP using app password."""
    gmail_user = os.environ.get("GMAIL_EMAIL", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not (gmail_user and gmail_pass) and os.path.exists(GMAIL_SENDER_CONFIG):
        with open(GMAIL_SENDER_CONFIG) as f:
            creds = json.load(f)
        gmail_user = creds.get("email", "")
        gmail_pass = creds.get("app_password", "")

    if not (gmail_user and gmail_pass):
        log("No Gmail SMTP credentials available for fallback.")
        return False

    msg = _build_mime(subject, body, gmail_user, to_addr, reply_to)

    try:
        with smtplib.SMTP(GMAIL_SMTP, GMAIL_PORT, timeout=10) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_addr, msg.as_string())
        log(f"Email sent via Gmail SMTP fallback ({gmail_user}).")
        return True
    except Exception as e:
        log(f"Gmail SMTP fallback failed: {e}")
        return False


def send_email(subject, body, reply_to=None):
    """Send notification email. Primary: MS Graph (M365). Fallbacks: SendGrid, Gmail SMTP."""
    if DRY_RUN:
        log(f"DRY RUN - Would email: {subject}")
        return

    rt = reply_to or REPLY_TO_ADDRESS

    # Primary: Microsoft Graph (M365 -> M365 has best deliverability)
    if _send_via_ms_graph(subject, body, EMAIL_ADDRESS, rt):
        return

    # Fallback 1: SendGrid SMTP
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    if not api_key and os.path.exists(SENDGRID_KEY_FILE):
        with open(SENDGRID_KEY_FILE) as f:
            api_key = f.read().strip()
    if api_key:
        sg_msg = _build_mime(subject, body, EMAIL_ADDRESS, EMAIL_ADDRESS, rt)
        if _send_via_sendgrid(sg_msg, api_key):
            return

    # Fallback 2: Gmail SMTP (often spam-filtered by M365, last resort)
    if _send_via_gmail_smtp(subject, body, EMAIL_ADDRESS, rt):
        return

    log("ERROR: All email delivery methods failed.")


def main():
    log("GBP Review Monitor starting...")

    # Import auth (deferred so script loads fast for --help)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("gbp_auth", os.path.join(os.path.dirname(__file__), "gbp-auth.py"))
    gbp_auth = module_from_spec(spec)
    spec.loader.exec_module(gbp_auth)

    known = load_known()
    templates = load_templates()

    # Fetch reviews via v4 REST API
    try:
        reviews_response = gbp_auth.v4_get("reviews", params={"pageSize": 50, "orderBy": "updateTime desc"})
    except Exception as e:
        log(f"ERROR fetching reviews: {e}")
        sys.exit(1)

    reviews = reviews_response.get("reviews", [])
    log(f"Fetched {len(reviews)} reviews.")

    new_reviews = []
    for review in reviews:
        review_id = review.get("name", review.get("reviewId", ""))
        if not review_id:
            # Generate stable ID from content
            content = f"{review.get('reviewer', {}).get('displayName', '')}{review.get('createTime', '')}"
            review_id = hashlib.sha256(content.encode()).hexdigest()[:16]

        if review_id not in known["review_ids"]:
            new_reviews.append((review_id, review))
            known["review_ids"].append(review_id)

    if not new_reviews:
        log("No new reviews.")
        save_known(known)
        return

    log(f"Found {len(new_reviews)} new review(s).")

    for review_id, review in new_reviews:
        stars = review.get("starRating", "FIVE")
        reviewer = review.get("reviewer", {}).get("displayName", "Anonymous")
        comment = review.get("comment", "(No comment)")
        create_time = review.get("createTime", "Unknown")

        draft = draft_response(review, templates)

        # Save pending response
        pending = {
            "review_id": review_id,
            "reviewer": reviewer,
            "stars": stars,
            "comment": comment,
            "create_time": create_time,
            "draft_response": draft["response_text"],
            "template_id": draft["template_id"],
            "service_detected": draft["service_detected"],
            "drafted_at": datetime.now().isoformat(),
            "status": "pending",
        }

        # Generate short ID for email threading
        short_id = uuid.uuid4().hex[:8].upper()
        pending["short_id"] = short_id

        safe_id = review_id.replace("/", "_").replace(" ", "_")[-40:]
        pending_file = os.path.join(PENDING_DIR, f"{safe_id}.json")
        with open(pending_file, "w") as f:
            json.dump(pending, f, indent=2)

        star_num = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}.get(stars, "?")
        log(f"New {star_num}-star review from {reviewer}. Draft saved: {pending_file}")

        # Email notification with Reply-To and REV-ID
        subject = f"[REV-{short_id}] New {star_num}-star GBP Review from {reviewer}"
        body = f"""New Google Business Profile Review
{'=' * 40}

Reviewer: {reviewer}
Rating: {'*' * int(star_num) if isinstance(star_num, int) else '?'} ({star_num}/5)
Date: {create_time}

Review:
{comment}

{'=' * 40}
Draft Response:
{draft['response_text']}

{'=' * 40}

HOW TO RESPOND (just reply to this email):

  - Reply "approved" to post the draft response above as-is
  - Reply with your own text to use that instead

Your reply goes to {REPLY_TO_ADDRESS} and will be
automatically picked up and posted to Google.

{'=' * 40}
CLI fallback (optional):
  python3 ~/projects/scripts/gbp-review-respond.py --approve {safe_id}
  python3 ~/projects/scripts/gbp-review-respond.py --edit {safe_id}
"""
        send_email(subject, body, reply_to=REPLY_TO_ADDRESS)

    save_known(known)
    log(f"Done. {len(new_reviews)} new review(s) processed.")


if __name__ == "__main__":
    main()
