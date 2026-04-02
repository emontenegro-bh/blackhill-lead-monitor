#!/usr/bin/env python3
"""
Proposal Reply Processor -- detect replies to proposal emails and respond.

Checks the Gmail inbox for replies to proposal emails, processes them
through the Claude API, and sends back an updated proposal or answer.

Usage:
    python3 scripts/check-proposal-replies.py               # Normal run
    python3 scripts/check-proposal-replies.py --dry-run      # Detect but don't respond
    python3 scripts/check-proposal-replies.py --test         # Test connections

Config: Environment variables (GitHub Actions) or ~/.config/ files (local).
"""

import json
import imaplib
import email
import os
import signal
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# --- Timeout ---
def timeout_handler(signum, frame):
    print("ERROR: Script timed out after 90 seconds", file=sys.stderr)
    sys.exit(1)

if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(90)

DRY_RUN = "--dry-run" in sys.argv
STATE_FILE = Path(__file__).parent.parent / "data" / "proposal-reply-state.json"

PROPOSAL_SUBJECT_MARKERS = [
    "proposal ready:",
    "proposal description:",
    "updated proposal:",
]


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr)


# --- Config ---

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
        if not config["anthropic_api_key"]:
            config["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return config


# --- State ---

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_uids": [], "stats": {"total_replies": 0}, "last_run": None}


def save_state(state):
    if DRY_RUN:
        log("DRY RUN: Would save state")
        return
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if len(state["processed_uids"]) > 500:
        state["processed_uids"] = state["processed_uids"][-500:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --- Email helpers ---

def decode_str(s):
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
    subj_lower = subject.lower()
    if not subj_lower.startswith("re:") and not subj_lower.startswith("fw:"):
        return False
    return any(marker in subj_lower for marker in PROPOSAL_SUBJECT_MARKERS)


# --- Claude API ---

def call_claude(config, reply_body, original_subject):
    system_prompt = """You are the Black Hill Landscaping proposal assistant. A team member has replied to a proposal email with a question or revision request.

Your job:
1. Read their reply and the original proposal (included as quoted text in the reply).
2. Answer any questions with specific calculations and numbers.
3. If they request changes, produce the full updated HTML proposal description.

RULES:
- Start with a brief answer to their question, then the updated proposal HTML if changes are needed.
- Wrap updated proposals in the standard Aspire format:
  <div style="font-size: 10pt;" id="fontFamilySizeSetting">
  <div style="font-family: Arial,sans-serif;" id="fontFamilySetting">
  (content)
  </div></div>
- Use only: <h3>, <p>, <ul>, <li> tags. No bold, no em dashes, no pricing.
- Always specify edging as "steel black edging."
- Always include planting soil quantities.
- Every bullet ends with a period.
- Mulch depth is 2 inches max. Black mulch. Bags (3 cuft) for small jobs, yards for large.
- If the question is about quantities (soil yards, sod pallets, etc.), show the math."""

    user_text = f"Subject: {original_subject}\n\nReply content:\n{reply_body[:3000]}"
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_text}],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": config["anthropic_api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        return result["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log(f"Claude API error: {e.code} {body[:200]}")
        return None
    except Exception as e:
        log(f"Claude API error: {e}")
        return None


# --- Send response ---

def send_response(config, original_subject, response_text):
    if DRY_RUN:
        log(f"DRY RUN: Would send response to {original_subject}")
        return True

    subject = f"Re: {original_subject}" if not original_subject.lower().startswith("re:") else original_subject

    html_body = f"""\
<html>
<body style="font-family: Arial, sans-serif; font-size: 11pt; color: #333;">
<h3 style="color: #B08A3C;">Proposal Update</h3>
<div style="white-space: pre-wrap;">{response_text}</div>
<hr style="border: 1px solid #C9A24D; margin: 16px 0;">
<p style="font-size: 9pt; color: #888;">Generated by Black Hill Assistant. Reply to request further changes.</p>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Black Hill Assistant <{config['gmail_email']}>"
    msg["To"] = config["recipient"]
    msg.attach(MIMEText(response_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(config["gmail_email"], config["gmail_app_password"])
            server.sendmail(config["gmail_email"], config["recipient"], msg.as_string())
        log(f"Response sent: {subject}")
        return True
    except Exception as e:
        log(f"Email send failed: {e}")
        return False


# --- Main ---

def check_and_process(config, state):
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(config["gmail_email"], config["gmail_app_password"])
    mail.select("INBOX")

    # Search ALL recent messages (last 7 days) -- use UID for stable IDs
    status, messages = mail.uid("search", None, "(SINCE 23-Mar-2026)")
    if status != "OK" or not messages[0]:
        log("No recent messages found")
        mail.logout()
        return

    uids = messages[0].split()
    log(f"Scanning {len(uids)} recent messages for proposal replies")

    processed_count = 0
    for uid in uids:
        uid_str = uid.decode()
        if uid_str in state["processed_uids"]:
            continue

        # Use BODY.PEEK to avoid marking as read
        status, data = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if status != "OK":
            continue
        header = data[0][1].decode("utf-8", errors="replace")
        header_msg = email.message_from_string(header)
        subject = decode_str(header_msg["Subject"])
        from_addr = decode_str(header_msg["From"])

        # Skip our own messages
        if config["gmail_email"] in from_addr:
            state["processed_uids"].append(uid_str)
            continue

        # Only process proposal replies
        if not is_proposal_reply(subject):
            state["processed_uids"].append(uid_str)
            continue

        # Fetch full body with PEEK
        log(f"Found proposal reply: {subject} from {from_addr}")
        status, body_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            continue
        msg = email.message_from_bytes(body_data[0][1])
        body = get_body(msg)

        if not body.strip():
            log("Empty reply body, skipping")
            state["processed_uids"].append(uid_str)
            continue

        # Process with Claude
        log("Processing reply with Claude API...")
        response = call_claude(config, body, subject)
        if not response:
            log("Claude API failed, will retry next run")
            continue

        # Send response
        if send_response(config, subject, response):
            state["processed_uids"].append(uid_str)
            state["stats"]["total_replies"] = state["stats"].get("total_replies", 0) + 1
            processed_count += 1
            # Mark original as read
            mail.uid("store", uid, "+FLAGS", "\\Seen")

    log(f"Processed {processed_count} proposal replies")
    mail.logout()


def test_connections(config):
    print("Testing Gmail IMAP...", end=" ")
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        m.login(config["gmail_email"], config["gmail_app_password"])
        m.logout()
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")

    print("Testing Claude API...", end=" ")
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514", "max_tokens": 10,
        "messages": [{"role": "user", "content": "Say OK"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"x-api-key": config["anthropic_api_key"],
                 "anthropic-version": "2023-06-01", "content-type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")

    print("Testing Gmail SMTP...", end=" ")
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(config["gmail_email"], config["gmail_app_password"])
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")


def main():
    config = load_config()

    if "--test" in sys.argv:
        test_connections(config)
        return

    state = load_state()
    try:
        check_and_process(config, state)
    except Exception as e:
        log(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
