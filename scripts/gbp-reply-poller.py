#!/usr/bin/env python3
"""GBP Reply Poller - monitors Gmail for review approval replies.

Runs every 5 minutes via launchd. Looks for replies to review notification
emails (matching [REV-*] in subject), parses the reply, and either posts
the draft response or the custom reply text to Google Business Profile.

Workflow:
  1. Search Gmail for unread messages to monte24negro+gbp@gmail.com
  2. Match [REV-{short_id}] in subject to pending-response files
  3. Parse reply body (strip quoted/forwarded text)
  4. "approved"/"approve" -> post draft as-is
  5. Any other text -> use as custom response
  6. Post via GBP v4 REST API
  7. Mark Gmail message as read
  8. Archive pending file to responded/
  9. Send confirmation email to Evelin

Usage:
  python3 scripts/gbp-reply-poller.py           # Normal run
  python3 scripts/gbp-reply-poller.py --dry-run  # Preview without posting
  python3 scripts/gbp-reply-poller.py --test     # Test Gmail connection
"""

import json, os, sys, re, glob, base64, shutil, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

sys.path.insert(0, os.path.dirname(__file__))

_CLOUD_MODE = bool(os.environ.get("GBP_CLIENT_ID"))
if _CLOUD_MODE:
    _DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gbp")
    CONFIG_DIR = _DATA_DIR
else:
    CONFIG_DIR = os.path.expanduser("~/.config/gbp")
PENDING_DIR = os.path.join(CONFIG_DIR, "pending-responses")
RESPONDED_DIR = os.path.join(CONFIG_DIR, "responded")
LOG_FILE = os.path.join(CONFIG_DIR, "reply-poller.log")
EMAIL_ADDRESS = "evelin@blackhilltx.com"
GMAIL_SMTP = "smtp.gmail.com"
GMAIL_PORT = 587
GMAIL_SENDER_CONFIG = os.path.expanduser("~/.config/gmail-sender/config.json")

DRY_RUN = "--dry-run" in sys.argv

os.makedirs(RESPONDED_DIR, exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_gbp_auth():
    """Lazy-load gbp_auth module."""
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("gbp_auth", os.path.join(os.path.dirname(__file__), "gbp-auth.py"))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_short_id(subject):
    """Extract REV-XXXXXXXX from email subject."""
    match = re.search(r"\[REV-([A-Z0-9]+)\]", subject, re.IGNORECASE)
    return match.group(1).upper() if match else None


def find_pending_by_short_id(short_id):
    """Find pending response file matching a short_id."""
    for filepath in glob.glob(os.path.join(PENDING_DIR, "*.json")):
        with open(filepath) as f:
            data = json.load(f)
        if data.get("short_id", "").upper() == short_id.upper():
            return filepath, data
    return None, None


def strip_quoted_text(body):
    """Strip quoted/forwarded text from an email reply.

    Handles common patterns:
    - "On {date}, {person} wrote:" (Gmail, Outlook)
    - "-----Original Message-----" (Outlook)
    - "> quoted lines"
    - "From: ..." header blocks
    - "====" separator lines (from our notification emails)
    """
    # Normalize \r\n and \r to \n, strip BOM characters
    body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    lines = body.split("\n")
    clean_lines = []

    for line in lines:
        stripped = line.strip()

        # Stop at common quote markers
        if re.match(r"^On .+ wrote:$", stripped):
            break
        if stripped.startswith("-----Original Message"):
            break
        if stripped.startswith("________________________________"):
            break
        if re.match(r"^From:\s+", stripped):
            break
        if re.match(r"^Sent:\s+", stripped):
            break
        # Stop at our notification email separator lines
        if re.match(r"^={10,}$", stripped):
            break

        # Skip quoted lines (> prefix)
        if stripped.startswith(">"):
            continue

        clean_lines.append(line)

    result = "\n".join(clean_lines).strip()

    # Remove any email signature after common delimiters
    for sig_marker in ["--\n", "-- \n", "\n---\n", "\nSent from my ", "\nGet Outlook for "]:
        if sig_marker in result:
            result = result[:result.index(sig_marker)].strip()

    # Strip Outlook/Exchange image-based signatures and trailing junk
    lines_final = []
    for line in result.split("\n"):
        if line.strip().startswith("Get Outlook for "):
            break
        # Stop at Outlook inline image signatures [cid:...]
        if re.match(r"^\[cid:[a-f0-9-]+\]$", line.strip(), re.IGNORECASE):
            break
        lines_final.append(line)
    result = "\n".join(lines_final).strip()

    return result


# Phrases that should NEVER appear in a posted review response.
# If any are found, the reply still contains notification email content.
LEAK_MARKERS = [
    "HOW TO RESPOND",
    "CLI fallback",
    "python3 ~/projects/scripts/",
    "gbp-review-respond.py",
    "automatically picked up and posted",
    "reply to this email",
    "Reply \"approved\"",
    "New Google Business Profile Review",
    "Draft Response:",
    "monte24negro+gbp@gmail.com",
]


def contains_leaked_content(text):
    """Check if response text contains internal notification data."""
    lower = text.lower()
    for marker in LEAK_MARKERS:
        if marker.lower() in lower:
            return marker
    return None


def get_message_body(message):
    """Extract plain text body from a Gmail message."""
    payload = message.get("payload", {})

    # Simple message with body directly
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    # Multipart message - find text/plain part
    for part in payload.get("parts", []):
        mime_type = part.get("mimeType", "")
        if mime_type == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        # Nested multipart
        for subpart in part.get("parts", []):
            if subpart.get("mimeType") == "text/plain" and subpart.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(subpart["body"]["data"]).decode("utf-8", errors="replace")

    return ""


def get_message_headers(message):
    """Extract headers as a dict from a Gmail message."""
    headers = message.get("payload", {}).get("headers", [])
    return {h["name"]: h["value"] for h in headers}


def post_review_response(gbp_auth, review_name, response_text):
    """Post a review reply via GBP v4 REST API."""
    try:
        gbp_auth.v4_put(f"{review_name}/reply", {"comment": response_text})
        return True
    except Exception as e:
        log(f"ERROR posting response: {e}")
        return False


def send_confirmation(reviewer, stars, response_text, was_custom):
    """Send confirmation email to Evelin via Gmail SMTP."""
    if DRY_RUN:
        log("DRY RUN - Would send confirmation email")
        return

    gmail_user = os.environ.get("GMAIL_EMAIL", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not (gmail_user and gmail_pass) and os.path.exists(GMAIL_SENDER_CONFIG):
        with open(GMAIL_SENDER_CONFIG) as f:
            creds = json.load(f)
        gmail_user = creds.get("email", "")
        gmail_pass = creds.get("app_password", "")

    if not (gmail_user and gmail_pass):
        log("No Gmail SMTP credentials available. Confirmation email skipped.")
        return

    action = "custom response" if was_custom else "draft response"
    subject = f"GBP Review Response Posted ({reviewer})"
    body = f"""Review response posted successfully.

Reviewer: {reviewer}
Rating: {stars}
Action: {action}

Response posted:
{response_text}
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = EMAIL_ADDRESS
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(GMAIL_SMTP, GMAIL_PORT, timeout=10) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, EMAIL_ADDRESS, msg.as_string())
        log("Confirmation email sent via Gmail SMTP.")
    except Exception as e:
        log(f"Confirmation email failed: {e}")


def process_reply(gbp_auth, message):
    """Process a single Gmail reply message."""
    headers = get_message_headers(message)
    subject = headers.get("Subject", "")
    from_addr = headers.get("From", "")

    short_id = extract_short_id(subject)
    if not short_id:
        log(f"No REV-ID in subject: {subject}")
        return False

    log(f"Processing reply for REV-{short_id} from {from_addr}")

    filepath, pending = find_pending_by_short_id(short_id)
    if not pending:
        log(f"No pending response found for REV-{short_id}. May be already responded.")
        return False

    # Extract and clean reply body
    raw_body = get_message_body(message)
    reply_text = strip_quoted_text(raw_body)

    if not reply_text:
        log(f"Empty reply body for REV-{short_id}. Skipping.")
        return False

    # Determine action — normalize: lowercase, strip whitespace and punctuation
    normalized = re.sub(r"[^\w\s]", "", reply_text.lower().strip())
    APPROVAL_WORDS = ("approved", "approve", "yes", "ok", "post it", "looks good", "lgtm")
    is_approval = normalized in APPROVAL_WORDS
    # Fallback: check first line only (handles residual signature content)
    if not is_approval:
        first_line = re.sub(r"[^\w\s]", "", reply_text.strip().split("\n")[0].lower().strip())
        is_approval = first_line in APPROVAL_WORDS
    was_custom = not is_approval

    if is_approval:
        response_text = pending["draft_response"]
        log(f"Approval received. Using draft response for REV-{short_id}.")
    else:
        response_text = reply_text
        log(f"Custom response received for REV-{short_id}: {response_text[:80]}...")

    # Safety check: block posting if response contains internal notification data
    leaked = contains_leaked_content(response_text)
    if leaked:
        log(f"BLOCKED REV-{short_id}: Response contains leaked notification data (matched: '{leaked}'). "
            f"Email reply was not cleanly stripped. Will NOT post to Google.")
        return False

    # Post to GBP
    if DRY_RUN:
        log(f"DRY RUN - Would post response for {pending['reviewer']}: {response_text[:80]}...")
    else:
        review_name = pending.get("review_id", "")
        if not post_review_response(gbp_auth, review_name, response_text):
            log(f"Failed to post response for REV-{short_id}. Keeping pending.")
            return False

    # Archive pending file
    pending["status"] = "responded"
    pending["responded_at"] = datetime.now().isoformat()
    pending["final_response"] = response_text
    pending["response_method"] = "email_custom" if was_custom else "email_approved"

    dest = os.path.join(RESPONDED_DIR, os.path.basename(filepath))
    if not DRY_RUN:
        with open(dest, "w") as f:
            json.dump(pending, f, indent=2)
        os.remove(filepath)
        log(f"Archived to {dest}")

    # Send confirmation
    send_confirmation(
        pending.get("reviewer", "Unknown"),
        pending.get("stars", "?"),
        response_text,
        was_custom,
    )

    return True


def main():
    log("GBP Reply Poller starting...")

    gbp_auth = load_gbp_auth()

    # Search for unread replies to the +gbp address with REV- in subject
    query = "to:monte24negro+gbp@gmail.com subject:REV- is:unread"
    try:
        messages = gbp_auth.gmail_search(query, max_results=20)
    except Exception as e:
        log(f"ERROR searching Gmail: {e}")
        sys.exit(1)

    if not messages:
        log("No new reply emails found.")
        return

    log(f"Found {len(messages)} unread reply email(s).")

    processed = 0
    for msg_stub in messages:
        msg_id = msg_stub["id"]
        try:
            message = gbp_auth.gmail_get_message(msg_id)
        except Exception as e:
            log(f"ERROR fetching message {msg_id}: {e}")
            continue

        if process_reply(gbp_auth, message):
            processed += 1

        # Mark as read regardless of outcome to avoid re-processing
        if not DRY_RUN:
            try:
                gbp_auth.gmail_mark_read(msg_id)
            except Exception as e:
                log(f"WARNING: Could not mark message {msg_id} as read: {e}")

    log(f"Done. Processed {processed}/{len(messages)} reply email(s).")


def test_connection():
    """Test Gmail API connectivity."""
    print("Testing Gmail API for reply poller...")
    gbp_auth = load_gbp_auth()

    try:
        msgs = gbp_auth.gmail_search("is:inbox", max_results=3)
        print(f"  Gmail API working. Found {len(msgs)} recent messages.")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    # Check for any existing REV- replies
    rev_msgs = gbp_auth.gmail_search("to:monte24negro+gbp@gmail.com subject:REV-", max_results=5)
    print(f"  Found {len(rev_msgs)} REV- reply message(s) total.")

    # Check pending files
    pending_files = glob.glob(os.path.join(PENDING_DIR, "*.json"))
    with_short_id = 0
    for pf in pending_files:
        with open(pf) as f:
            if json.load(f).get("short_id"):
                with_short_id += 1
    print(f"  Pending responses: {len(pending_files)} total, {with_short_id} with short_id")
    print("\nReply poller test passed.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_connection()
    else:
        main()
