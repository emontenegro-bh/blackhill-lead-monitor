#!/usr/bin/env python3
"""
Reply Pipeline — Monitor, classify, and act on proposal email replies.

Two-stage pipeline:
  Stage 1 (Monitor):   IMAP poll with dynamic date filtering and deduplication
  Stage 2 (Process):   Classify reply (approved/questions/declined) and respond

Usage:
    python3 reply-pipeline.py                # Normal run
    python3 reply-pipeline.py --dry-run      # Detect and classify, no replies sent
    python3 reply-pipeline.py --test         # Test connections
    python3 reply-pipeline.py --reset-date   # Reset the search-since date to now

Config: Environment variables (GitHub Actions) or ~/.config/ files (local).

Env vars:
    GMAIL_EMAIL             Gmail sender address
    GMAIL_APP_PASSWORD      Gmail app password (NOT account password)
    ANTHROPIC_API_KEY       Claude API key
    PROPOSAL_RECIPIENT      Email recipient (default: evelin@blackhilltx.com)
"""

import email
import imaplib
import json
import os
import signal
import smtplib
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR.parent / "data" / "reply-pipeline-state.json"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_TIMEOUT = 60
SCRIPT_TIMEOUT = 90
MAX_PROCESSED_UIDS = 1000
DRY_RUN = "--dry-run" in sys.argv

# Reply classification categories
CLASSIFICATION_APPROVED = "approved"
CLASSIFICATION_QUESTIONS = "questions"
CLASSIFICATION_REVISION = "revision"
CLASSIFICATION_DECLINED = "declined"
CLASSIFICATION_UNKNOWN = "unknown"

# Subject markers that identify proposal-related emails
PROPOSAL_SUBJECT_MARKERS = [
    "proposal ready:",
    "proposal description:",
    "updated proposal:",
    "proposal update",
]


# ---------------------------------------------------------------------------
# Timeout guard
# ---------------------------------------------------------------------------

def _timeout_handler(signum, frame):
    log("FATAL: Script timed out after %d seconds" % SCRIPT_TIMEOUT)
    sys.exit(1)

if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(SCRIPT_TIMEOUT)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config():
    config = {
        "gmail_email": os.environ.get("GMAIL_EMAIL", ""),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "recipient": os.environ.get("PROPOSAL_RECIPIENT", "evelin@blackhilltx.com"),
    }

    if not config["gmail_email"]:
        p = Path.home() / ".config" / "gmail-sender" / "config.json"
        if p.exists():
            gm = json.loads(p.read_text())
            config["gmail_email"] = gm.get("email", "")
            config["gmail_app_password"] = gm.get("app_password", "")

    if not config["anthropic_api_key"]:
        for p in [Path.home() / ".config" / "anthropic" / "config.json",
                   Path.home() / ".anthropic" / "config.json"]:
            if p.exists():
                config["anthropic_api_key"] = json.loads(p.read_text()).get("api_key", "")
                break

    return config


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("WARNING: Corrupt state file, starting fresh")
    return {
        "processed_uids": [],
        "search_since": None,  # Dynamic: set on first run
        "stats": {
            "total_replies": 0,
            "approved": 0,
            "questions": 0,
            "revisions": 0,
            "declined": 0,
        },
        "last_run": None,
    }


def save_state(state):
    if DRY_RUN:
        log("SKIP: State not saved (dry-run)")
        return
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    # FIFO cap on processed UIDs
    if len(state["processed_uids"]) > MAX_PROCESSED_UIDS:
        state["processed_uids"] = state["processed_uids"][-MAX_PROCESSED_UIDS:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def decode_str(s):
    """Decode RFC2047-encoded email header."""
    if s is None:
        return ""
    decoded = decode_header(s)
    parts = []
    for part, charset in decoded:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return " ".join(parts)


def get_body(msg):
    """Extract text body from email message (prefer plain text, fallback to HTML)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


def is_proposal_reply(subject):
    """Check if a subject line indicates a reply to a proposal email."""
    subj_lower = subject.lower()
    # Must be a reply or forward
    if not (subj_lower.startswith("re:") or subj_lower.startswith("fw:")):
        return False
    return any(marker in subj_lower for marker in PROPOSAL_SUBJECT_MARKERS)


def get_search_date(state):
    """Get the IMAP SINCE date string for searching.

    Uses created_after semantics: search from the last known run time,
    or default to 7 days ago on first run. This prevents reprocessing
    historical messages on every run.
    """
    if state.get("search_since"):
        # Parse stored ISO date and use it
        try:
            dt = datetime.fromisoformat(state["search_since"])
            return dt.strftime("%d-%b-%Y")
        except (ValueError, TypeError):
            pass

    # First run or corrupt date: start from 7 days ago
    dt = datetime.now(timezone.utc) - timedelta(days=7)
    return dt.strftime("%d-%b-%Y")


def parse_internal_date(date_str):
    """Parse email Date header into datetime. Returns None on failure."""
    if not date_str:
        return None
    try:
        return email.utils.parsedate_to_datetime(date_str)
    except Exception:
        return None


# ===================================================================
# STAGE 1: MONITOR — IMAP polling with deduplication
# ===================================================================

def fetch_new_replies(config, state):
    """Stage 1: Connect to IMAP, find unprocessed proposal replies.

    Uses UID-based deduplication (stable across sessions) plus
    dynamic date filtering to avoid scanning the entire inbox.
    """
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    except Exception as e:
        log(f"ERROR: IMAP connection failed: {e}")
        return []

    try:
        mail.login(config["gmail_email"], config["gmail_app_password"])
    except imaplib.IMAP4.error as e:
        log(f"ERROR: IMAP login failed — check GMAIL_EMAIL and GMAIL_APP_PASSWORD: {e}")
        return []

    mail.select("INBOX")

    # Search with dynamic date filter (created_after semantics)
    since_date = get_search_date(state)
    log(f"MONITOR: Searching since {since_date}")

    status, messages = mail.uid("search", None, f"(SINCE {since_date})")
    if status != "OK" or not messages[0]:
        log("MONITOR: No messages found in date range")
        mail.logout()
        return []

    uids = messages[0].split()
    log(f"MONITOR: Scanning {len(uids)} messages")

    processed_set = set(state["processed_uids"])  # O(1) lookups
    replies = []

    for uid in uids:
        uid_str = uid.decode()

        # Client-side deduplication
        if uid_str in processed_set:
            continue

        # Fetch just headers first (lightweight)
        status, data = mail.uid(
            "fetch", uid,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
        )
        if status != "OK" or not data or not data[0]:
            continue

        try:
            header = data[0][1].decode("utf-8", errors="replace")
        except (IndexError, AttributeError):
            continue

        header_msg = email.message_from_string(header)
        subject = decode_str(header_msg["Subject"])
        from_addr = decode_str(header_msg["From"])
        date_str = header_msg["Date"]

        # Skip our own outgoing messages
        if config["gmail_email"] in from_addr:
            state["processed_uids"].append(uid_str)
            continue

        # Only process proposal replies
        if not is_proposal_reply(subject):
            state["processed_uids"].append(uid_str)
            continue

        # Additional date check: skip messages older than search_since
        # (IMAP SINCE is date-granular, so we need this for precision)
        msg_date = parse_internal_date(date_str)
        if msg_date and state.get("search_since"):
            try:
                cutoff = datetime.fromisoformat(state["search_since"])
                if msg_date.astimezone(timezone.utc) < cutoff:
                    state["processed_uids"].append(uid_str)
                    continue
            except (ValueError, TypeError):
                pass

        # Fetch full body
        log(f"MONITOR: Found reply: {subject} from {from_addr}")
        status, body_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not body_data or not body_data[0]:
            continue

        try:
            msg = email.message_from_bytes(body_data[0][1])
        except Exception:
            continue

        body = get_body(msg)
        if not body.strip():
            log("MONITOR: Empty body, skipping")
            state["processed_uids"].append(uid_str)
            continue

        replies.append({
            "uid": uid_str,
            "subject": subject,
            "from": from_addr,
            "body": body,
            "date": date_str,
        })

    mail.logout()
    log(f"MONITOR: {len(replies)} new proposal replies to process")
    return replies


# ===================================================================
# STAGE 2: PROCESS — Classify and respond
# ===================================================================

class ClaudeClient:
    """Claude API client for reply processing."""

    KNOWN_ERRORS = {
        400: "Bad request — check payload structure",
        401: "Invalid ANTHROPIC_API_KEY",
        403: "API key lacks permission for this model",
        404: "Model not found — verify CLAUDE_MODEL",
        429: "Rate limited",
        500: "Anthropic server error — transient",
        529: "Anthropic overloaded — transient",
    }

    def __init__(self, config):
        self.api_key = config["anthropic_api_key"]

    def call(self, system_prompt, user_text, max_tokens=4096):
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_text}],
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT)
            result = json.loads(resp.read())
            if "content" not in result or not result["content"]:
                log("ERROR: Claude returned empty content")
                return None
            text = result["content"][0].get("text", "")
            if not text.strip():
                log("ERROR: Claude returned blank text (silent failure)")
                return None
            return text
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            hint = self.KNOWN_ERRORS.get(e.code, "Unknown error")
            log(f"ERROR: Claude API {e.code}: {hint}")
            if e.code in (404, 400) and "model" in body.lower():
                log(f"  Model ID: {CLAUDE_MODEL}")
                log(f"  Response: {body}")
            return None
        except urllib.error.URLError as e:
            log(f"ERROR: Claude API connection failed: {e.reason}")
            return None
        except Exception as e:
            log(f"ERROR: Claude API unexpected: {e}")
            return None


def classify_reply(claude_client, reply):
    """Classify a reply into: approved, questions, revision, declined."""
    system_prompt = """You are classifying an email reply to a landscaping proposal.

Classify the reply into EXACTLY ONE category and respond with JSON only:

{
  "classification": "approved|questions|revision|declined",
  "confidence": 0.0-1.0,
  "summary": "one-line summary of what the customer said"
}

Categories:
- "approved": Customer accepts the proposal as-is, says looks good, wants to proceed, or asks to schedule.
- "questions": Customer has questions about materials, quantities, timeline, or process but hasn't rejected.
- "revision": Customer wants specific changes (add/remove items, adjust quantities, different plants, etc.).
- "declined": Customer declines, says not interested, too expensive, going with someone else, or wants to cancel.

Reply with ONLY the JSON object, no other text."""

    user_text = f"Subject: {reply['subject']}\nFrom: {reply['from']}\n\nReply:\n{reply['body'][:2000]}"
    result = claude_client.call(system_prompt, user_text, max_tokens=256)

    if not result:
        return CLASSIFICATION_UNKNOWN, 0.0, "Classification failed"

    try:
        # Parse JSON from response (handle potential markdown wrapping)
        text = result.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        classification = parsed.get("classification", CLASSIFICATION_UNKNOWN)
        confidence = float(parsed.get("confidence", 0.0))
        summary = parsed.get("summary", "")

        valid = {CLASSIFICATION_APPROVED, CLASSIFICATION_QUESTIONS,
                 CLASSIFICATION_REVISION, CLASSIFICATION_DECLINED}
        if classification not in valid:
            classification = CLASSIFICATION_UNKNOWN

        return classification, confidence, summary
    except (json.JSONDecodeError, ValueError, KeyError):
        log(f"WARNING: Could not parse classification: {result[:100]}")
        return CLASSIFICATION_UNKNOWN, 0.0, result[:100]


def generate_response(claude_client, reply, classification):
    """Generate an appropriate response based on classification."""
    prompts = {
        CLASSIFICATION_APPROVED: """The customer approved this proposal. Write a brief, warm confirmation email:
- Thank them for approving
- Let them know the team will be in touch to schedule
- Keep it under 3 sentences
- Sign off as "Black Hill Landscaping Team"
Do NOT include any HTML tags. Plain text only.""",

        CLASSIFICATION_QUESTIONS: """The customer has questions about this landscaping proposal.
Answer their questions with specific details, calculations, and numbers.
- Show math for quantities (soil yards, sod pallets, mulch, etc.)
- Be specific about materials and methods
- Keep the tone helpful and professional
- If the question is about timeline, give a realistic range
- Sign off as "Black Hill Landscaping Team"

If the answer requires an updated proposal, include the full updated HTML after your answer.
Wrap updated proposals in:
<div style="font-size: 10pt;" id="fontFamilySizeSetting">
<div style="font-family: Arial,sans-serif;" id="fontFamilySetting">
(content using only h3, p, ul, li tags)
</div></div>""",

        CLASSIFICATION_REVISION: """The customer wants changes to this landscaping proposal.
1. Acknowledge the requested changes.
2. Produce the FULL updated HTML proposal with changes applied.

RULES:
- Always specify edging as "steel black edging."
- Always include planting soil quantities.
- Every bullet ends with a period.
- Mulch depth is 2 inches max. Black mulch. Bags (3 cuft) for small, yards for large.
- Use only: <h3>, <p>, <ul>, <li> tags. No bold, no em dashes, no pricing.
- Wrap in Aspire format divs.
- Show the math if quantities changed.""",

        CLASSIFICATION_DECLINED: """The customer has declined this proposal. Write a brief, gracious response:
- Thank them for considering Black Hill
- Let them know you're available if they change their mind or need anything in the future
- Keep it to 2-3 sentences
- Professional and warm, no hard sell
- Sign off as "Black Hill Landscaping Team"
Do NOT include any HTML tags. Plain text only.""",
    }

    system_prompt = prompts.get(classification, prompts[CLASSIFICATION_QUESTIONS])
    user_text = f"Subject: {reply['subject']}\n\nCustomer's reply:\n{reply['body'][:3000]}"
    return claude_client.call(system_prompt, user_text)


def send_response_email(config, reply, response_text, classification):
    """Send the response email."""
    if DRY_RUN:
        log(f"SEND: [skip] Response to {reply['subject']} ({classification})")
        return True

    subject = reply["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    # Build HTML wrapper
    cls_labels = {
        CLASSIFICATION_APPROVED: ("Booking Confirmed", "#4CAF50"),
        CLASSIFICATION_QUESTIONS: ("Proposal Update", "#B08A3C"),
        CLASSIFICATION_REVISION: ("Updated Proposal", "#B08A3C"),
        CLASSIFICATION_DECLINED: ("Thank You", "#888"),
    }
    label, color = cls_labels.get(classification, ("Proposal Update", "#B08A3C"))

    html_body = f"""\
<html>
<body style="font-family: Arial, sans-serif; font-size: 11pt; color: #333;">
<h3 style="color: {color};">{label}</h3>
<div style="white-space: pre-wrap;">{response_text}</div>
<hr style="border: 1px solid #C9A24D; margin: 16px 0;">
<p style="font-size: 9pt; color: #888;">
  Generated by Black Hill Proposal Pipeline. Reply to continue the conversation.
</p>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Black Hill Assistant <{config['gmail_email']}>"
    msg["To"] = config["recipient"]
    msg.attach(MIMEText(response_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(config["gmail_email"], config["gmail_app_password"])
            server.sendmail(config["gmail_email"], config["recipient"], msg.as_string())
        log(f"SEND: Response delivered — {subject} ({classification})")
        return True
    except smtplib.SMTPAuthenticationError:
        log("ERROR: Gmail auth failed — check GMAIL_EMAIL and GMAIL_APP_PASSWORD")
        return False
    except Exception as e:
        log(f"ERROR: Email send failed: {e}")
        return False


def process_reply(config, claude_client, reply, state):
    """Full Stage 2: classify → generate response → send → update state."""
    log(f"PROCESS: Classifying reply from {reply['from']}")
    classification, confidence, summary = classify_reply(claude_client, reply)
    log(f"PROCESS: {classification} (confidence={confidence:.2f}) — {summary}")

    # Generate appropriate response
    response_text = generate_response(claude_client, reply, classification)
    if not response_text:
        log("PROCESS: Response generation failed, will retry next cycle")
        return False

    # Send
    if send_response_email(config, reply, response_text, classification):
        state["processed_uids"].append(reply["uid"])
        state["stats"]["total_replies"] = state["stats"].get("total_replies", 0) + 1

        # Track classification stats
        stat_key = {
            CLASSIFICATION_APPROVED: "approved",
            CLASSIFICATION_QUESTIONS: "questions",
            CLASSIFICATION_REVISION: "revisions",
            CLASSIFICATION_DECLINED: "declined",
        }.get(classification, "questions")
        state["stats"][stat_key] = state["stats"].get(stat_key, 0) + 1

        return True

    return False


# ===================================================================
# Orchestration
# ===================================================================

def run_pipeline(config, state):
    """Full pipeline: monitor → classify → respond."""
    claude_client = ClaudeClient(config)

    # Initialize search_since on first run
    if not state.get("search_since"):
        state["search_since"] = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).isoformat()
        log(f"PIPELINE: First run, searching from {state['search_since']}")

    # Stage 1: Monitor
    replies = fetch_new_replies(config, state)

    # Stage 2: Process each reply
    processed = 0
    for reply in replies:
        if process_reply(config, claude_client, reply, state):
            processed += 1

    # Advance search_since to now (so next run only sees newer messages)
    state["search_since"] = datetime.now(timezone.utc).isoformat()

    log(f"PIPELINE: Processed {processed}/{len(replies)} replies")
    log(f"PIPELINE: Stats — {json.dumps(state['stats'])}")


# ===================================================================
# CLI
# ===================================================================

def test_connections(config):
    """Test IMAP, SMTP, and Claude API connections."""
    errors = []

    print("Testing Gmail IMAP...", end=" ", flush=True)
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
        m.login(config["gmail_email"], config["gmail_app_password"])
        m.logout()
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")
        errors.append("IMAP")

    print(f"Testing Claude API ({CLAUDE_MODEL})...", end=" ", flush=True)
    claude = ClaudeClient(config)
    result = claude.call("Say OK", "Say OK", max_tokens=10)
    if result:
        print("OK")
    else:
        print("FAILED")
        errors.append("Claude")

    print("Testing Gmail SMTP...", end=" ", flush=True)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as s:
            s.starttls()
            s.login(config["gmail_email"], config["gmail_app_password"])
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")
        errors.append("SMTP")

    if errors:
        print(f"\nFailed: {', '.join(errors)}")
        sys.exit(1)
    else:
        print("\nAll connections OK")


def main():
    config = load_config()

    if "--test" in sys.argv:
        test_connections(config)
        return

    if "--reset-date" in sys.argv:
        state = load_state()
        state["search_since"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        log(f"Reset search_since to {state['search_since']}")
        return

    state = load_state()
    try:
        run_pipeline(config, state)
    except Exception:
        traceback.print_exc()
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
