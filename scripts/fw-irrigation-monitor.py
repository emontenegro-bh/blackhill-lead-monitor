#!/usr/bin/env python3
"""Fort Worth Water irrigation quote requests -> Teams card.

Fort Worth Water's Field Operations Division sends Black Hill irrigation
repair work through the irrigation@blackhilltx.com shared mailbox. Each job
produces roughly four emails, only one of which is a new job:

  1. QUOTE REQUEST  "Please see below ... and provide a quote", with a
                    MAXIMO P.O. line and the Maximo dispatch quoted beneath.
                    This is the only one worth a card.
  2. PO ISSUED      "Here's your PO Information: PO #: 45747". Authorisation
                    to actually do the work. Posted as a REPLY to the card.
  3. "WILL NOTE / when replying please reply to all"  from a supervisor,
                    fires about a minute after most requests. No content.
  4. "PO noted"     two words, from a different supervisor.

Between 2026-09-29 and 2026-10-08 the mailbox held 55 messages covering 13
actual jobs, nine of which arrived in one afternoon. Carlos spent most of
2026-10-08 asking Fort Worth which emails were duplicates.

THE DEDUPE KEY IS THE WORK ORDER NUMBER FROM THE BODY

Not the subject, and not the Outlook conversation. On 2026-10-07 Alicia Payne
sent a quote request whose subject read "5149 GADSEN AVE / Work Order
26-105133" while its body read "6A-80241 - 5828 JAPONICA ST - WO# 26-104493".
Five minutes later a "CORRECTION----" email carried the real Gadsden job. A
subject-keyed monitor would have raised a card for a job that did not exist
and missed one that did. A thread-keyed monitor would have swallowed the
correction, because the new job arrived inside a thread that had already
fired. The body WO# was correct on all 13.

Addresses are not a key either: the same street appears as "GADSEN" and
"Gadsden", and "Warm Springs Trl" as "Warm SPrings Trl".

WHERE THE PARTS COME FROM

Evelin needs the materials list, because it goes straight into the proposal.
It is never in the Fort Worth staffer's own note -- that is only ever "provide
a quote" plus the address line. It is in the Maximo dispatch quoted
underneath, phrased a different way every time:

  "Plumber needs 4 feet soaker hose & couplings"
  "Said plumber needs (1) 1-1/2 inch Connector"   (plus a damaged 3/4" soaker hose)
  "need routine plumber to repair 8 feet of 1" pvc irrigation line, pvc to
   pvc coupling and a sprinkler head"
  "Plumber needs 8 feet 1" PVC & (2) couplings"

A regex over that would break the first time someone writes it differently,
so Claude extracts it. Both halves matter: at 16000 Rein Ave the crew damaged
a 3/4" soaker hose AND the plumber needs a 1-1/2" connector, and the proposal
needs both lines.

WHAT IS DELIBERATELY NOT ON THE CARD

Work order number, Maximo PO number, damage cause and crew initials. Evelin
asked for address and parts only (2026-10-10). The WO# is still parsed and
stored, because it is the dedupe key and the join between a request and its
later PO, but it is not displayed.

Usage:
  python scripts/fw-irrigation-monitor.py
  python scripts/fw-irrigation-monitor.py --dry-run   # parse + print, no post
  python scripts/fw-irrigation-monitor.py --test      # check mailbox access
"""

import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

import msal

DRY_RUN = "--dry-run" in sys.argv
TEST_MODE = "--test" in sys.argv

CONFIG_DIR = os.path.expanduser("~/.config/lead-monitor")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CLOUD_MODE = bool(os.environ.get("MS_CLIENT_ID"))

SCRIPT_NAME = "fw-irrigation-monitor"
MAILBOX = os.environ.get("FW_IRRIGATION_MAILBOX", "irrigation@blackhilltx.com")

# Only Fort Worth sends work here. Anything from our own domain is Carlos or
# Evelin talking in the thread, never a new job.
SENDER_DOMAIN = "fortworthtexas.gov"

# 24 hours, matching lead-monitor.py. The cron runs every 15 minutes, so the
# window is enormous overlap on purpose: a run that fails or a GitHub Actions
# outage must not lose a job permanently, and db.is_processed() makes
# re-reading the same message free.
LOOKBACK_MINUTES = int(os.environ.get("FW_LOOKBACK_MINUTES", "1440"))
MAX_MESSAGES = int(os.environ.get("FW_MAX_MESSAGES", "100"))

# Work orders are "26-105267": a two-digit fiscal year, a dash, five or six
# digits. Seen as "WO# 26-105267", "WO#26-106087", "WO # 26-91610",
# "Work Order 26-104322" and "Work Order #: 26-104493".
WO_RE = re.compile(r"\b(\d{2}-\d{5,6})\b")

# "MAXIMO P.O.#: 6A-80222 - 7704 SHORTHORN WAY - WO# 26-105267". The dash
# before the address is an en dash in Alicia's template and a plain hyphen in
# Patricia's, and Patricia sometimes uses runs of spaces instead.
MAXIMO_RE = re.compile(
    r"MAXIMO\s*P\.?O\.?\s*#?\s*:?\s*"          # label
    r"(?P<maximo>6A-\d+)\s*"                    # 6A-80222
    r"[-–—\s]+"                       # separator
    r"(?P<address>.+?)\s*"                      # 7704 SHORTHORN WAY
    r"[-–—\s]+"
    r"(?:WO|Work\s*Order)\s*#?\s*:?\s*(?P<wo>\d{2}-\d{5,6})",
    re.IGNORECASE | re.DOTALL,
)

# "PO #: 45747", "PO #45667", "PO#: 45740". Distinct from the Maximo number,
# which is the internal requisition; this is the financial PO that authorises
# the work.
PO_RE = re.compile(r"\bPO\s*#\s*:?\s*(\d{4,6})\b", re.IGNORECASE)

# PO emails sandwich the address between the PO number and the work order:
#   "PO #: 45740 / 16000 Rein Ave / Work Order 26-109588"
#   "PO #: 45661 / 10000 HAVERSHAM DR / WO # 26-91610"
#   "PO #45667 1280 HWY 114 RD WO 26-93844"
# Needed because Patricia sometimes sends the PO as a reply on OUR proposal
# thread, where the subject is "Proposal for your review #3804 ..." and the
# subject fallback would put our own subject line on the card.
PO_ADDRESS_RE = re.compile(
    r"\bPO\s*#\s*:?\s*\d{4,6}\b\s*"
    r"(?P<address>.+?)\s*"
    r"(?:Work\s*Order|WO)\s*#?\s*:?\s*\d{2}-\d{5,6}",
    re.IGNORECASE | re.DOTALL,
)

QUOTE_MARKER = "provide a quote"
PO_MARKERS = ("your po information", "here's your po", "here is your po",
              "below for your po", "your po number")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Haiku is plenty for pulling a parts list out of two sentences, and this runs
# on every request. Same model the WhatConverts spam check uses.
PARTS_MODEL = os.environ.get("FW_PARTS_MODEL", "claude-haiku-4-5-20251001")

TEAMS_WEBHOOK_URL = os.environ.get("FW_TEAMS_WEBHOOK_URL", "").strip()


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------- Graph ---

def load_ms_config():
    if CLOUD_MODE:
        return {
            "client_id": os.environ["MS_CLIENT_ID"],
            "tenant_id": os.environ["MS_TENANT_ID"],
            "client_secret": os.environ["MS_CLIENT_SECRET"],
        }
    with open(CONFIG_FILE) as f:
        ms = json.load(f)["microsoft"]
    return {"client_id": ms["client_id"], "tenant_id": ms["tenant_id"],
            "client_secret": ms["client_secret"]}


def get_token():
    ms = load_ms_config()
    app = msal.ConfidentialClientApplication(
        ms["client_id"],
        authority=f"https://login.microsoftonline.com/{ms['tenant_id']}",
        client_credential=ms["client_secret"],
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Graph auth failed: {result.get('error_description', result)}")
    return result["access_token"]


def graph_get(token, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code in (429,) or 500 <= e.code < 600:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
            raise RuntimeError(f"Graph {e.code}: {body}")
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return None


def fetch_recent(token):
    """Messages into the irrigation mailbox within the lookback window."""
    since = datetime.now(timezone.utc).timestamp() - LOOKBACK_MINUTES * 60
    since_iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # $expand with an explicit $select on attachments keeps contentBytes out
    # of the response. Without the inner $select Graph inlines every
    # attachment as base64, and these carry 4-5 MB job photos.
    url = (
        f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/messages"
        f"?$filter=receivedDateTime%20ge%20{since_iso}"
        f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,webLink"
        f"&$expand=attachments($select=name,contentType,isInline,size)"
        f"&$orderby=receivedDateTime%20desc"
        f"&$top={MAX_MESSAGES}"
    )
    return (graph_get(token, url) or {}).get("value", [])


def count_photos(msg):
    """Job photos only.

    Every Fort Worth email carries image001.png and image002.png inline: the
    city logo and a social icon in the signature. Counting those would report
    "2 photos" on a request that has none.
    """
    n = 0
    for att in msg.get("attachments") or []:
        if att.get("isInline"):
            continue
        name = (att.get("name") or "").lower()
        ctype = (att.get("contentType") or "").lower()
        # Maximo saves photos as .jpeg but serves them as
        # application/octet-stream, so the extension is the reliable test.
        if ctype.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".heic")):
            n += 1
    return n


# ---------------------------------------------------------- Parsing ---

def html_to_text(content):
    """Flatten a Graph HTML body to text.

    Outlook puts a copy of the Maximo dispatch in a hidden preview div
    (display:none, white text, 1px line-height) and then repeats it visibly.
    Stripping tags naively yields the parts list twice, which then reaches
    Claude as a doubled prompt. Drop display:none blocks first.
    """
    text = re.sub(r"<div[^>]*display:\s*none[^>]*>.*?</div>", " ", content,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def classify(text):
    """Return 'quote', 'po' or None.

    Checked in that order. A single email is never both: the quote request
    carries the Maximo requisition and no financial PO, and the PO email
    carries the financial PO and never asks for a quote.
    """
    low = text.lower()
    if QUOTE_MARKER in low:
        return "quote"
    if any(m in low for m in PO_MARKERS) and PO_RE.search(text):
        return "po"
    return None


def split_maximo_block(text):
    """Return (staffer_note, maximo_dispatch).

    Everything from the quoted 'From: donotreply@emaximo.com' header down is
    the Maximo dispatch, which is where the parts live. If the header is
    missing (Patricia occasionally forwards without it) the whole body is
    returned as the dispatch so extraction still has something to read.
    """
    m = re.search(r"From:\s*donotreply@emaximo\.com", text, re.IGNORECASE)
    if not m:
        return text, text
    return text[:m.start()].strip(), text[m.start():].strip()


def parse_address(text, subject):
    """Address from the MAXIMO line, falling back to the subject.

    The MAXIMO line is authoritative. It was wrong exactly once, on the
    2026-10-07 Japonica/Gadsden mixup, and on that occasion the SUBJECT was
    the wrong one -- the body matched its own WO#.
    """
    m = MAXIMO_RE.search(text)
    if m:
        addr = " ".join(m.group("address").split())
        if addr:
            return addr
    # Fall back to the subject: "... at 16000 Rein Ave (640R)", "RTN PLUMBER
    # @ 8941 MOSSY CREEK LN (104T)", "Routine Plumber  10420 Patron Trail
    # Work Order 26-102794".
    s = re.sub(r"^((RE|FW|Fw|Re)\s*:\s*)+", "", subject or "").strip()
    s = re.sub(r"CORRECTION-*\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(work\s*order|WO)\s*#?\s*:?\s*\d{2}-\d{5,6}", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"(?i)\b(routine plumber( request)?|rtn plumber( req)?|need(ed)?|emg plumber( rqt)?|plumber)\b", " ", s)
    s = re.sub(r"\(\w{1,5}\)", " ", s)           # grid refs like (640R)
    s = re.sub(r"\*[^*]*\*", " ", s)             # "*Pic Attached*"
    s = s.strip(" -–@/").strip()
    s = re.sub(r"^(at|@)\s+", "", s, flags=re.IGNORECASE)
    return " ".join(s.split()) or "(address not parsed)"


def parse_po_address(text, subject):
    """Address for a PO email, reading the PO block before the subject.

    Only used when the work order was never carded, so there is no stored
    address to prefer.
    """
    m = PO_ADDRESS_RE.search(text)
    if m:
        addr = " ".join(m.group("address").split())
        # Guard against the regex swallowing a paragraph when the address is
        # missing from the block entirely.
        if addr and len(addr) <= 60:
            return addr
    return parse_address(text, subject)


def parse_wo(text, subject):
    """Work order number, body first.

    Body over subject is the whole lesson of 2026-10-07. Inside the body the
    MAXIMO line wins over a bare match, because the quoted Maximo dispatch
    also repeats the WO# and a stray number elsewhere should not outrank the
    structured line.
    """
    m = MAXIMO_RE.search(text)
    if m:
        return m.group("wo")
    m = WO_RE.search(text)
    if m:
        return m.group(1)
    m = WO_RE.search(subject or "")
    return m.group(1) if m else None


def extract_parts(dispatch_text):
    """Ask Claude for the materials list. Returns a list of strings.

    Fails soft: if the key is missing or the call errors, the card still
    posts with the raw dispatch text, because a card with clumsy wording
    beats no card at all.
    """
    if not ANTHROPIC_KEY:
        log("  WARNING: ANTHROPIC_API_KEY not set, falling back to raw text")
        return []

    prompt = f"""Extract the irrigation materials from this City of Fort Worth
work order dispatch. These go into a repair proposal, so list every physical
part and quantity mentioned.

Include BOTH:
  - parts the crew damaged and that must be replaced
  - parts the plumber says are needed for the repair

Start every line with its quantity and size exactly as written: "4 feet
soaker hose", "(1) 1-1/2 inch connector", "8 feet of 1 inch PVC line". Never
drop a number. Do not add parts that are not mentioned. Do not include
labour, crew names, operator initials, damage cause, addresses or phone
numbers.

Dispatch:
{dispatch_text[:3000]}

Respond with one part per line, no bullets, no numbering, no preamble.
If no parts are mentioned at all, respond with exactly: NONE"""

    payload = json.dumps({
        "model": PARTS_MODEL,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        reply = (data.get("content", [{}])[0].get("text") or "").strip()
    except Exception as e:
        log(f"  WARNING: parts extraction failed ({e}), falling back to raw text")
        return []

    if reply.upper().startswith("NONE"):
        return []

    # Strip bullets and ordered-list markers ONLY. An earlier version used
    # ^[-*0-9.)\s]+ and silently ate the quantities: "4 feet soaker hose"
    # became "feet soaker hose" and "8 feet 1\" PVC" became "feet 1\" PVC".
    # The quantity is the part of the line the proposal actually needs, so a
    # leading bare number must survive. Only a digit bound to a '.' or ')'
    # and then whitespace counts as numbering.
    parts = []
    for line in reply.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^(?:[-*•]|\d{1,2}[.)])\s+", "", line).strip()
        if line:
            parts.append(line)
    return parts


# ------------------------------------------------------------ Teams ---

def post_card(address, parts, photo_count, dispatch_text, web_link=None):
    """Post the request card. Returns the Teams message id, or None.

    The flow behind FW_TEAMS_WEBHOOK_URL must answer with the posted
    message's id (Power Automate: "When an HTTP request is received" ->
    "Post card in a chat or channel" -> "Response"). Without it the card
    still posts, but the PO can never be threaded underneath and will land
    as its own card instead.
    """
    if parts:
        parts_text = "\n\n".join(f"- {p}" for p in parts)
    else:
        # No extraction: show the dispatch so Evelin can read the parts off
        # it herself rather than getting an empty card.
        parts_text = f"_Parts not parsed. From the work order:_\n\n{dispatch_text[:600]}"

    # Photos cannot be embedded: Teams caps an Adaptive Card payload at 28 KB
    # and these attachments run 0.6-4.9 MB each, so a data: URI is off by two
    # orders of magnitude. Card images must come from an anonymously
    # reachable HTTPS URL, which rules out SharePoint and OneDrive too.
    # The count plus a deep link to the message is the usable version: it
    # says whether there is anything to attach to the Aspire opportunity and
    # opens the exact email in one tap.
    if photo_count:
        photo_value = f"{photo_count} attached - add to the Aspire opportunity"
    else:
        photo_value = "None"
    facts = [
        {"title": "Address", "value": address},
        {"title": "Photos", "value": photo_value},
    ]

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "Container", "style": "emphasis", "items": [{
                        "type": "TextBlock",
                        "text": "New FW Irrigation Request",
                        "weight": "Bolder", "size": "Medium", "color": "Good"}]},
                    {"type": "TextBlock", "text": address,
                     "weight": "Bolder", "size": "Large", "wrap": True,
                     "spacing": "Small"},
                    {"type": "FactSet", "facts": facts},
                    {"type": "TextBlock", "text": "**Parts**", "spacing": "Medium"},
                    {"type": "TextBlock", "text": parts_text, "wrap": True,
                     "spacing": "Small"},
                ],
            },
        }],
    }
    if web_link:
        card["attachments"][0]["content"]["actions"] = [{
            "type": "Action.OpenUrl",
            "title": "Open email" + (" (photos)" if photo_count else ""),
            "url": web_link,
        }]
    return _post(card)


def post_po_reply(message_id, po_number, address):
    """Thread the PO under its request card. Returns True if it posted."""
    payload = {
        "replyToId": message_id,
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{
                    "type": "TextBlock",
                    "text": f"**PO #{po_number} issued** - cleared to do the work.",
                    "wrap": True,
                }],
            },
        }],
    }
    return _post(payload) is not None


def _post(payload):
    """POST to the flow. Returns the Teams message id when the flow sends one.

    A flow with no Response action returns an empty 202, which is a success
    for the card and a None for the id. That is handled rather than treated
    as an error, so the channel keeps working while the flow is being set up.
    """
    if DRY_RUN:
        log("  DRY RUN: would post to Teams")
        return "dry-run-message-id"
    if not TEAMS_WEBHOOK_URL:
        log("  WARNING: FW_TEAMS_WEBHOOK_URL not set, nothing posted")
        return None

    data = json.dumps(payload).encode()
    last_err = "unknown error"
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                TEAMS_WEBHOOK_URL, data=data,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace").strip()
            if not raw:
                return None
            try:
                body = json.loads(raw)
            except ValueError:
                return None
            if isinstance(body, dict):
                return body.get("id") or body.get("messageId") or body.get("message_id")
            return None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {detail}".strip()
            if not (e.code == 429 or 500 <= e.code < 600):
                break
        except Exception as e:
            last_err = str(e)
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Teams post failed: {last_err}")


# ------------------------------------------------------------- Main ---

def handle_quote(msg, text, state):
    subject = msg.get("subject", "")
    wo = parse_wo(text, subject)
    if not wo:
        log(f"  SKIP: no work order number in '{subject[:60]}'")
        return False

    cards = state.setdefault("cards", {})
    if wo in cards:
        log(f"  WO {wo}: already carded, skipping")
        return False

    address = parse_address(text, subject)
    _, dispatch = split_maximo_block(text)
    parts = extract_parts(dispatch)
    photos = count_photos(msg)

    log(f"  WO {wo}: {address} | {photos} photo(s) | parts: {parts or '(none extracted)'}")
    message_id = post_card(address, parts, photos, dispatch, msg.get("webLink"))
    cards[wo] = {
        "address": address,
        "message_id": message_id,
        "carded_at": datetime.now(timezone.utc).isoformat(),
    }
    if message_id is None:
        log(f"  WO {wo}: card posted but the flow returned no message id; "
            f"its PO will post as a separate card")
    return True


def handle_po(msg, text, state):
    subject = msg.get("subject", "")
    wo = parse_wo(text, subject)
    po_match = PO_RE.search(text)
    if not wo or not po_match:
        log(f"  SKIP: PO email missing WO# or PO# in '{subject[:60]}'")
        return False
    po_number = po_match.group(1)

    cards = state.setdefault("cards", {})
    entry = cards.get(wo)
    seen_pos = set(entry.get("pos", [])) if entry else set()
    if po_number in seen_pos:
        log(f"  WO {wo}: PO #{po_number} already posted, skipping")
        return False

    # The carded address wins: it came off the MAXIMO line on the original
    # request, which is the most reliable form of it.
    address = (entry or {}).get("address") or parse_po_address(text, subject)
    parent = (entry or {}).get("message_id")

    if parent:
        log(f"  WO {wo}: PO #{po_number} -> reply on existing card")
        post_po_reply(parent, po_number, address)
    else:
        # Either the request predates this monitor or the flow gave us no
        # message id. Post standalone rather than drop the PO on the floor.
        log(f"  WO {wo}: PO #{po_number} with no parent card, posting standalone")
        post_card(address, [f"PO #{po_number} issued - cleared to do the work."],
                  0, "")

    entry = entry or {"address": address, "message_id": None}
    entry["pos"] = sorted(seen_pos | {po_number})
    cards[wo] = entry
    return True


def main():
    log("Fort Worth irrigation monitor starting")
    if DRY_RUN:
        log("DRY RUN: no Teams posts, no state writes")

    token = get_token()

    if TEST_MODE:
        msgs = fetch_recent(token)
        log(f"[OK] {MAILBOX} reachable, {len(msgs)} message(s) in the last "
            f"{LOOKBACK_MINUTES} minutes")
        for m in msgs[:10]:
            text = html_to_text(m.get("body", {}).get("content", ""))
            kind = classify(text) or "-"
            log(f"  [{kind:5}] {m.get('subject', '')[:70]}")
        return 0

    state = db.load_state(SCRIPT_NAME, {"cards": {}}) if not DRY_RUN else {"cards": {}}
    messages = fetch_recent(token)
    log(f"{len(messages)} message(s) in the last {LOOKBACK_MINUTES} minutes")

    quotes = pos = 0
    for msg in messages:
        sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        if not sender.endswith(SENDER_DOMAIN):
            continue

        msg_id = msg.get("id", "")
        if not DRY_RUN and db.is_processed(SCRIPT_NAME, msg_id):
            continue

        text = html_to_text(msg.get("body", {}).get("content", ""))
        kind = classify(text)
        if kind is None:
            # Supervisor "reply to all" notes, "PO noted", quote corrections.
            # 42 of the mailbox's first 55 messages. Mark them so a later run
            # does not re-read them.
            if not DRY_RUN:
                db.mark_processed(SCRIPT_NAME, [msg_id], kind="noise")
            continue

        # Flag-and-continue: one malformed email must not stop the batch.
        try:
            if kind == "quote" and handle_quote(msg, text, state):
                quotes += 1
            elif kind == "po" and handle_po(msg, text, state):
                pos += 1
        except Exception as e:
            log(f"  ERROR on '{msg.get('subject', '')[:60]}': {e}")
            continue

        if not DRY_RUN:
            db.mark_processed(SCRIPT_NAME, [msg_id], kind=kind)

    if not DRY_RUN:
        db.save_state(SCRIPT_NAME, state)
    log(f"Done. {quotes} new request card(s), {pos} PO update(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
