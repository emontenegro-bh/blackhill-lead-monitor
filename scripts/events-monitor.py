#!/usr/bin/env python3
"""Networking Events Monitor - finds property-manager / industry events and puts
them in front of Evelin with everything needed to decide and show up.

The problem this solves: association event announcements arrive scattered across
email and websites. Sourcing each one (when, where, how much, member vs non-member,
registration deadline) takes long enough that events get skipped. This does the
sourcing so the only remaining step is deciding to go.

Two sources:
  1. Evelin's inbox via Microsoft Graph (app-only, reuses the lead-monitor MS_* app).
     Associations email her directly - this catches everything, including events
     that never appear on a public page.
  2. Association websites (DFW CAI, AATC, BOMA Fort Worth, FWHCC, GFWBA).

Output: one email per event, each carrying exactly one .ics invitation, so each
event lands on the calendar as its own bookable entry on its own day. Mail clients
render only one invitation per message - a digest with sixteen .ics parts attached
arrives as a single invite - so invites are never batched. Events that need a
ticket or an early registration are titled with the cost and the cutoff up front
and carry a reminder set against the registration deadline, not the event date.
Deliberately no Graph calendar-write permission needed.

Modes:
  --monthly   Invites for anything not yet invited, plus an attachment-free
              summary email of the whole look-ahead window. Runs the 1st.
  (default)   Invites for anything not yet invited. Silent when there is nothing,
              like bid-monitor. No summary email.
  --dry-run   Print the digest, send nothing, save no state.
  --test      Verify Graph auth and per-source reachability, then exit.

Usage:
    python3 events-monitor.py --monthly
    python3 events-monitor.py --dry-run
"""

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import smtplib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from email import encoders

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

STATE_NAME = "events-monitor"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MAILBOX = os.environ.get("EVENTS_MAILBOX", "evelin@blackhilltx.com")
RECIPIENTS = [e.strip() for e in os.environ.get(
    "EVENTS_RECIPIENTS", "evelin@blackhilltx.com").split(",") if e.strip()]

LOOKAHEAD_DAYS = int(os.environ.get("EVENTS_LOOKAHEAD_DAYS", "75"))
INBOX_LOOKBACK_DAYS = int(os.environ.get("EVENTS_INBOX_LOOKBACK_DAYS", "45"))
MAX_SEEN = 2000
# The first live run hit a 10-page cap at 2,000 messages without clearing the
# 45-day window, so actual volume is ~44/day. 20 pages covers it with headroom.
# The cap is a runaway guard; hitting it is reported in the digest, not silent.
MAX_INBOX_PAGES = 20

# Invites go out one per email, so a backlog would arrive as a wall of mail. The
# cap defers the tail to the next run (Mon/Thu) rather than dropping it; invites
# are sent soonest-event-first so nothing imminent waits behind a November webinar.
MAX_INVITES_PER_RUN = int(os.environ.get("EVENTS_MAX_INVITES_PER_RUN", "8"))

# CivicPlus/Akamai-style WAFs return a silent zero for bot user-agents, so send
# the complete Chrome header set. A source returning nothing means blocked, not empty.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Associations that matter for commercial maintenance. `domains` drives the inbox
# filter; `urls` are scanned for public event listings. Verified 2026-08-08 -
# any URL that starts failing shows up in the digest footer rather than dying quietly.
# `match` strings are tested against sender + subject + body TOGETHER, never the
# sender domain alone. This is load-bearing: CCC Fort Worth sends from
# info@11459012.brevosend.com, a numeric Brevo relay with no org identity in the
# address at all. FWHCC arrives as ...-fwhcc.org@shared1.ccsend.com and NALP via a
# hubspotemail.net relay. Domain matching misses most of the real mail.
# Verified against Evelin's actual mailbox 2026-08-08.
#
# `tier` drives sort order in the digest:
#   1 = customers (property managers and CRE) - the whole point
#   2 = adjacent buyers (public sector, builders, general BD)
#   3 = peer/industry (useful, but these are competitors and vendors, not buyers)
SOURCES = [
    # --- Tier 1: where the actual buyers are ---
    {"org": "BOMA Fort Worth", "tier": 1, "what": "Commercial office / retail / industrial",
     "match": ["bomafortworth", "boma fort worth"],
     "urls": ["https://www.bomafortworth.org/", "https://members.bomafortworth.org/event-calendar"]},
    {"org": "IWIRE North Texas", "tier": 1, "women": True,
     "what": "Women in commercial real estate - buyers AND women-focused",
     "match": ["iwirenorthtexas", "iwire north texas", "iwire ntx", "iwire"], "urls": []},
    {"org": "CCC Fort Worth", "tier": 1,
     "what": "Contractors, Closers & Connections - CRE + construction networking",
     "match": ["ccc fort worth", "contractors, closers", "contractors closers"], "urls": []},
    {"org": "DFW CAI", "tier": 1, "what": "HOA / community association managers",
     "match": ["dfwcai", "dfw cai", "community associations institute"],
     "urls": ["https://dfwcai.starchapter.com/"]},
    {"org": "AATC", "tier": 1, "what": "Multifamily owners and property managers",
     "match": ["aatcnet", "apartment association of tarrant"],
     "urls": ["https://www.aatcnet.org/"]},
    {"org": "IREM", "tier": 1, "what": "Institute of Real Estate Management",
     "match": ["instituteofrealestatemanagement", "iremdallas", "irem "], "urls": []},

    # --- Tier 2: adjacent buyers and BD ---
    {"org": "Fort Worth Hispanic Chamber", "tier": 2, "what": "BD and networking (member)",
     "match": ["fwhcc", "fortworthhcc", "hispanic chamber"], "urls": []},
    {"org": "Texas Veterans Commission", "tier": 2, "what": "Veteran-owned: state facility vendor expos",
     "match": ["tvc.texas.gov", "vboc", "texas facilities commission"], "urls": []},
    {"org": "TWU Center for Women Entrepreneurs", "tier": 2, "women": True,
     "what": "Women-owned business programs",
     "match": ["twu.edu", "women entrepreneurs"], "urls": []},
    {"org": "GFWBA", "tier": 2, "what": "Builders association (adjacent channel)",
     "match": ["gfwba", "greater fort worth builders"], "urls": []},

    # --- Tier 3: peer / industry (attendees are competitors, not customers) ---
    {"org": "NALP", "tier": 3, "what": "National landscape trade association",
     "match": ["landscapeprofessionals", "nalpinfo"], "urls": []},
    {"org": "Lawn & Landscape", "tier": 3, "what": "Trade publication events",
     "match": ["lawnandlandscape"], "urls": []},
    {"org": "Small Business Expo", "tier": 3, "what": "General small-business trade show",
     "match": ["thesmallbusinessexpo"], "urls": []},
]

# Anything not on the list still gets caught if it reads like an event AND is local.
# Without this, every new association Evelin starts hearing from would be invisible
# until someone remembered to edit this file.
LOCAL_HINTS = ["fort worth", "dallas", "dfw", "arlington", "tarrant", "grapevine",
               "southlake", "keller", "north texas", "metroplex", "irving", "plano"]

# Travel is a real constraint, not a preference. Dallas-proper events cost most of
# a working day in drive time; Tarrant-county ones cost an hour. Events are labelled
# so Evelin can skip the far ones at a glance instead of discovering it at signup.
NEAR_CITIES = [
    "fort worth", "north richland hills", "richland hills", "arlington", "irving",
    "keller", "southlake", "grapevine", "watauga", "haltom city", "bedford", "euless",
    "hurst", "colleyville", "roanoke", "trophy club", "mansfield", "burleson",
    "crowley", "weatherford", "benbrook", "saginaw", "white settlement", "azle",
    "aledo", "westlake", "tarrant",
]
FAR_CITIES = [
    "dallas", "plano", "frisco", "richardson", "garland", "mckinney", "allen",
    "addison", "carrollton", "rockwall", "mesquite", "denton", "lewisville",
]

# Women-focused programming is a stated priority, so it is surfaced explicitly
# rather than left for Evelin to infer from the org name. Word-boundary matched:
# a substring check flags "other" as containing "her".
WOMEN_RE = re.compile(
    r"\b(women|women's|womens|woman|female|wbe|wosb|wbenc|she[\-\s]?leads)\b", re.I)

EVENT_KEYWORDS = [
    "event", "luncheon", "lunch", "breakfast", "meeting", "mixer", "expo",
    "trade show", "tradeshow", "golf", "clay shoot", "sporting clay", "gala",
    "seminar", "workshop", "conference", "summit", "networking", "register",
    "registration", "rsvp", "save the date", "business exchange", "tournament",
]

# Ordered most-specific first so "March 10, 2026" wins over a bare "3/10".
# The last two have no year: event calendars very often render "August 12" alone,
# which is exactly why an earlier year-required version of this found nothing.
DATE_PATTERNS = [
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{4})",
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{4})",
    # Bare numeric "8/5" with no year. Very common in subject lines ("Weds 8/5").
    # Guarded so it cannot swallow part of a full 8/5/2026 date.
    r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])",
]
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
MONTHS.update({m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)})
MONTHS["sept"] = 9

COST_PATTERN = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")

# "Register by September 20", "RSVP by 9/20", "tickets close Sept 20",
# "sponsorship deadline is October 3", "early bird ends Sep 15". A hit means the
# decision has its own date, earlier than the event, and missing it means not going.
DEADLINE_PATTERN = re.compile(
    r"(?:register|registration|rsvp|reserve|tickets?|early[\s-]?bird|sponsorship)"
    r"[^.!?\n]{0,40}?"
    r"(?:\bby\b|\bbefore\b|\bends?\b|\bcloses?\b|\bdeadline(?:\s+is)?\b)\s*:?\s*"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|"
    r"December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2}"
    r"(?:st|nd|rd|th)?(?:,?\s*\d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
    re.I)

# Language that means a seat has to be claimed ahead of the day, even when no
# dollar figure or explicit date was parsed.
TICKET_WORDS = (
    "ticket", "buy your", "purchase your", "seats are limited", "space is limited",
    "limited seating", "sold out", "early bird", "registration closes",
    "register by", "rsvp by", "pre-register", "sponsorship deadline",
    "advance registration", "registration deadline",
)
TIME_PATTERN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.I)
# Street address: number + words + suffix, optionally through city/state/zip.
ADDRESS_PATTERN = re.compile(
    r"\b(\d{3,6}\s+[A-Z][A-Za-z0-9.'\-]*(?:\s+[A-Z][A-Za-z0-9.'\-]*){0,5}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Parkway|Pkwy|Way|Circle|Cir|Court|Ct|Highway|Hwy|Freeway|Fwy|Trail|Trl|Plaza)"
    r"\.?(?:\s*(?:Suite|Ste|#)\s*[\w\-]+)?(?:,\s*[A-Z][A-Za-z\s]{2,20})?(?:,?\s*(?:TX|Texas))?(?:\s*\d{5})?)")

DRY_RUN = False


# ---------------------------------------------------------------- state

def load_state():
    # No try/except. The file version fell back to an empty `seen` list on any
    # read error, which re-reports every event still on every source page as
    # brand new. The whole point of this monitor is that a luncheon announced
    # twice reports once; a wiped seen-list turns one bad read into a digest
    # full of events Evelin has already seen and probably already declined.
    # db.load_state() raises and notify-failure.yml says so.
    return db.load_state(STATE_NAME, default={
        "seen": [], "source_failures": {}, "last_monthly": None,
    })


def save_state(state):
    if DRY_RUN:
        return
    state["seen"] = state["seen"][-MAX_SEEN:]
    state["invited"] = state.get("invited", [])[-MAX_SEEN:]
    db.save_state(STATE_NAME, state)


def event_key(ev):
    """Stable identity so the same luncheon announced twice reports once."""
    basis = f"{ev['org']}|{ev.get('date_iso') or ev['title'][:40]}|{ev['title'][:60]}".lower()
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- parsing

def parse_dates(text, today):
    """Return future dates found in text, soonest first."""
    found = []
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            try:
                g = m.groups()
                has_year = len(g) >= 3 and g[2]
                month = int(g[0]) if g[0].isdigit() else MONTHS.get(g[0].lower())
                day = int(g[1])
                if has_year:
                    year = int(g[2])
                else:
                    # Year omitted ("August 12", "8/5"). Assume the next occurrence:
                    # this year if still ahead, otherwise next year.
                    year = today.year
                    if month and (month, day) < (today.month, today.day):
                        year += 1
                if not month or not 1 <= month <= 12 or not 1 <= day <= 31:
                    continue
                d = datetime(year, month, day, tzinfo=timezone.utc)
            except (ValueError, TypeError, IndexError):
                continue
            if today <= d <= today + timedelta(days=LOOKAHEAD_DAYS + 300):
                found.append(d)
    return sorted(set(found))


def parse_costs(text):
    """Dollar figures that plausibly describe a ticket, not a sponsorship tier."""
    costs = []
    for m in COST_PATTERN.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 5 <= v <= 3000:
            ctx = text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")
            costs.append((v, " ".join(ctx.split())))
    # Dedupe by amount, keep first context
    seen, out = set(), []
    for v, ctx in costs:
        if v not in seen:
            seen.add(v)
            out.append((v, ctx))
    return out[:4]


def parse_deadline(text, today):
    """The date she has to act by, when the listing states one.

    Reuses parse_dates on just the matched fragment so the two date vocabularies
    cannot drift apart.
    """
    for m in DEADLINE_PATTERN.finditer(text):
        found = parse_dates(m.group(1), today)
        if found:
            return found[0]
    return None


def needs_advance_purchase(text, costs, deadline):
    """True when showing up requires a decision before the day of the event.

    Money changing hands counts, and so does a registration window that closes
    early: both mean seeing the invite the morning of is already too late. This
    is the flag Evelin plans around, so it errs toward flagging.
    """
    if deadline or costs:
        return True
    low = text.lower()
    return any(w in low for w in TICKET_WORDS)


def parse_address(text):
    m = ADDRESS_PATTERN.search(text)
    if m:
        return " ".join(m.group(1).split())
    # Listings frequently name the venue instead of the street address; the venue
    # name alone is enough to search maps and is far better than nothing.
    m = VENUE_PATTERN.search(text)
    return " ".join(m.group(1).split()) if m else None


def parse_time(text):
    m = TIME_PATTERN.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if m.group(3).lower() == "p" and hour != 12:
        hour += 12
    elif m.group(3).lower() == "a" and hour == 12:
        hour = 0
    return (hour, minute) if 0 <= hour <= 23 else None


# Navigation and archive links that read like events but aren't one.
JUNK_TITLES = re.compile(
    r"^\s*(past|upcoming|all|view|more|previous)?\s*events?\s*$|^\s*event calendar\s*$|"
    r"^\s*calendar\s*$|^\s*register\s*$|^\s*(learn|read|see|show|find out) more\s*$|"
    r"^\s*(more info(rmation)?|details|view (event|details)|sign up|rsvp)\s*$", re.I)

# Venue names when no street address is given ("Join us at the Petroleum Club").
VENUE_PATTERN = re.compile(
    r"(?:at|join us at|hosted at|located at)\s+(?:the\s+)?(?:iconic\s+)?"
    r"([A-Z][A-Za-z0-9'&.\-]*(?:\s+[A-Z][A-Za-z0-9'&.\-]*){0,4}"
    r"\s*(?:Club|Center|Centre|Hotel|Ballroom|Hall|Resort|Ranch|Course|Lanes|Park|Museum|Convention Center))")


# A real invitation asks you to do something. Newsletters from the same senders
# discuss topics and happen to contain dates, addresses, and dollar figures, which
# is how IREM's "what insurance underwriters want to see" became an "event" at
# IREM's Chicago HQ costing $55 (scraped from a headline about a $55.7M loan).
INVITE_RE = re.compile(
    r"\b(register|registration|rsvp|join us|you'?re invited|save the date|"
    r"tickets?|doors open|agenda|reserve your|secure your (?:spot|seat)|"
    r"seating|attendees?|luncheon|mixer|happy hour|trade show|golf tournament|"
    r"clay shoot|networking event|will be held|hosted at|meet us at)\b", re.I)

# Date-stamped digest subjects ("... and more | August 5 2026"), and generic
# newsletter titles. These are publications, never invitations.
NEWSLETTER_RE = re.compile(
    r"(\band more\s*\|)|(\|\s*(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},?\s*\d{4}\s*$)|"
    r"(\bthe latest news\b)|(\bnewsletter\b)|(\bweekly (?:digest|roundup|recap)\b)",
    re.I)


def looks_like_event(text):
    low = text.lower()
    return sum(1 for k in EVENT_KEYWORDS if k in low) >= 2


def is_invitation(subject, text):
    """Known senders skip the keyword-count bar, but nothing skips this. Without
    it, every trade-association newsletter becomes an event."""
    if NEWSLETTER_RE.search(subject or ""):
        return False
    return bool(INVITE_RE.search(text or ""))


def classify_travel(text, address):
    """near / far / unknown. Checks the address first since it is the most reliable
    signal, then falls back to any city named in the body."""
    # Word-boundary matched: a substring check finds "allen" inside "challenges"
    # and mislabelled a Fairfax, Virginia address as a long drive to Allen, TX.
    hay = f"{address or ''} {text}".lower()

    def _find(cities):
        return next((c for c in cities
                     if re.search(rf"\b{re.escape(c)}\b", hay)), None)

    near, far = _find(NEAR_CITIES), _find(FAR_CITIES)
    if near and not far:
        return "near", near.title()
    if far and not near:
        return "far", far.title()
    if near and far:
        # Both named: the address wins, otherwise assume the nearer reading.
        addr_low = (address or "").lower()
        if any(c in addr_low for c in FAR_CITIES):
            return "far", far.title()
        return "near", near.title()
    return "unknown", None


def is_women_focused(text, src):
    if src and src.get("women"):
        return True
    return bool(WOMEN_RE.search(text or ""))


def clean_title(title, text):
    """Listing blocks often prepend the weekday/date and append the whole blurb.
    Strip the date prefix and cut at the time, which reliably ends the name."""
    t = " ".join((title or "").split())
    t = re.sub(r"^(MON|TUE|TUES|WED|THU|THUR|THURS|FRI|SAT|SUN)[A-Z]*\s+", "", t, flags=re.I)
    t = re.sub(r"^(January|February|March|April|May|June|July|August|September|October|"
               r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+"
               r"\d{1,2}(?:st|nd|rd|th)?,?\s*(?:\d{4})?\s*", "", t, flags=re.I)
    t = re.split(r"\s+\d{1,2}(?::\d{2})?\s*[APap]\.?[Mm]\.?", t)[0]
    return t.strip(" -–:") or " ".join((text or "").split())[:70]


def build_event(org, title, text, url, today, source_label, tier=2, src=None):
    if JUNK_TITLES.match(title or ""):
        return None
    dates = parse_dates(text, today)
    if not dates:
        return None

    # A registration deadline falls BEFORE the event, and parse_dates returns
    # soonest first, so "Rock the Boat on October 9, register by September 25"
    # was scheduling the boat cruise on the 25th. Take the deadline out of the
    # running for the event date first. If it was the only date in the text it
    # is the event date, not a deadline.
    deadline = parse_deadline(text, today)
    if deadline:
        rest = [d for d in dates if d.date() != deadline.date()]
        if rest:
            dates = rest
        else:
            deadline = None

    when = dates[0]
    if when > today + timedelta(days=LOOKAHEAD_DAYS):
        return None
    if deadline and deadline.date() >= when.date():
        deadline = None
    tod = parse_time(text)
    if tod:
        when = when.replace(hour=tod[0], minute=tod[1])
    address = parse_address(text)
    travel, city = classify_travel(text, address)
    costs = parse_costs(text)
    return {
        "org": org,
        "tier": tier,
        "travel": travel,
        "city": city,
        "women": is_women_focused(text, src),
        "title": clean_title(title, text)[:140],
        "date_iso": when.strftime("%Y-%m-%d"),
        "start": when.isoformat(),
        "has_time": bool(tod),
        "address": address,
        "costs": costs,
        "deadline_iso": deadline.strftime("%Y-%m-%d") if deadline else None,
        "needs_ticket": needs_advance_purchase(text, costs, deadline),
        "url": url,
        "source": source_label,
        "snippet": " ".join(text.split())[:400],
    }


# ---------------------------------------------------------------- sources

def graph_token():
    cid = os.environ.get("MS_CLIENT_ID")
    secret = os.environ.get("MS_CLIENT_SECRET")
    tenant = os.environ.get("MS_TENANT_ID")
    if not (cid and secret and tenant):
        cfg_path = os.path.expanduser("~/.config/lead-monitor/config.json")
        try:
            with open(cfg_path) as f:
                ms = json.load(f)["microsoft"]
            cid, secret, tenant = ms["client_id"], ms["client_secret"], ms["tenant_id"]
        except (OSError, ValueError, KeyError):
            return None
    try:
        r = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"client_id": cid, "client_secret": secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        return r.json().get("access_token")
    except requests.RequestException as e:
        print(f"  Graph auth failed: {e}", file=sys.stderr)
        return None


def fetch_full_body(token, msg_id):
    """bodyPreview truncates at ~255 chars, which loses the address, cost, and
    often the date. Pull the full body, but only for messages already matched to a
    known association so this stays a handful of calls per run."""
    if not msg_id:
        return None
    try:
        r = requests.get(
            f"{GRAPH_BASE}/users/{MAILBOX}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "body"}, timeout=30)
        r.raise_for_status()
        html = (r.json().get("body") or {}).get("content") or ""
    except requests.RequestException:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())[:6000]


def scan_inbox(today):
    """Association email is the richest source - it catches events that never
    get a public listing. Read-only against Evelin's mailbox."""
    events, errors = [], []
    token = graph_token()
    if not token:
        errors.append("Inbox: Graph auth unavailable (MS_* secrets missing or app lacks Mail.Read)")
        return events, errors

    since = (today - timedelta(days=INBOX_LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")

    # Paginate. Evelin receives roughly 33 messages a day, so a single 200-message
    # page reaches back only ~6 days and the 45-day lookback is fiction. That is
    # exactly how CCC Fort Worth's Aug 12 announcement was missed on the first
    # live runs: the email existed, the parser handled it, the fetch never saw it.
    messages = []
    url = f"{GRAPH_BASE}/users/{MAILBOX}/messages"
    params = {"$select": "id,subject,bodyPreview,from,receivedDateTime,webLink",
              "$filter": f"receivedDateTime ge {since}",
              "$top": "200", "$orderby": "receivedDateTime desc"}
    for page in range(MAX_INBOX_PAGES):
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                             params=params if page == 0 else None, timeout=45)
            if r.status_code == 403:
                errors.append("Inbox: 403 from Graph - the app registration cannot read "
                              f"{MAILBOX}. Check for an ApplicationAccessPolicy limiting "
                              "it to the shared mailbox.")
                return events, errors
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as e:
            # Keep whatever pages already succeeded rather than losing the run.
            errors.append(f"Inbox (page {page + 1}): {e}")
            break
        messages.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
        if not url:
            break
    else:
        errors.append(f"Inbox: hit the {MAX_INBOX_PAGES}-page cap "
                      f"({len(messages)} messages); older mail in the "
                      f"{INBOX_LOOKBACK_DAYS}-day window was not scanned.")

    # This monitor's own digest lands in the same inbox, names every org it
    # reports on, and is full of dates. Without this guard it re-ingests itself
    # and compounds every run - which it did on the second live run, emitting an
    # "event" titled "Industry Events - August 2026 (4 upcoming)".
    self_addr = os.environ.get("GMAIL_EMAIL", "").strip().lower()
    SELF_SUBJECTS = ("industry events -", "new industry event", "new industry events")
    # Invite emails are now subject-titled after the event itself, so a subject
    # prefix can no longer carry this guard alone. Every message this script sends
    # contains SELF_MARKER in its body, which makes the guard subject-shape
    # independent.
    SELF_MARKER = "black hill events monitor"

    for msg in messages:
        addr = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        subject = msg.get("subject") or "(no subject)"
        preview = msg.get("bodyPreview") or ""
        if (self_addr and self_addr in addr) or subject.lower().startswith(SELF_SUBJECTS) \
                or SELF_MARKER in f"{subject} {preview}".lower():
            continue
        text = f"{subject}\n{preview}"

        # Match sender + subject + body together. Sender alone is not enough:
        # CCC Fort Worth comes from info@11459012.brevosend.com, which identifies
        # nothing. The org name is in the subject and body instead.
        hay = f"{addr} {text}".lower()
        src = next((s for s in SOURCES if any(m in hay for m in s["match"])), None)

        # A known association gets the benefit of the doubt on keyword COUNT:
        # bodyPreview is only ~255 chars, so requiring two event keywords silently
        # dropped real events (CCC's Aug 12 announcement was missed this way).
        # Unlisted senders still have to clear the keyword bar.
        if not src and not looks_like_event(text):
            continue

        if src:
            org, tier = src["org"], src["tier"]
            full = fetch_full_body(token, msg.get("id"))
            if full:
                text = f"{subject}\n{full}"

        # Nobody skips this. Trade associations send far more newsletters than
        # invitations, and a newsletter has dates, a footer address, and dollar
        # figures - everything the parser needs to fabricate a plausible event.
        if not is_invitation(subject, text):
            continue

        # This was an `elif` chained onto the invitation check, and that single
        # keyword cost every inbox event its identity: a KNOWN association's
        # invitation fell into the unlisted-sender branch and had `org` overwritten
        # with "Unlisted (shared1.ccsend.com)", then got dropped outright whenever
        # the body carried no local hint. Every calendar invite from the inbox
        # arrived titled "Unlisted". The fallback belongs only to senders that
        # matched no known source.
        if not src:
            if not any(h in text.lower() for h in LOCAL_HINTS):
                continue
            # Reads like a local event from a sender we do not track. Surface it
            # rather than drop it, named after the sending domain so it is obvious
            # the org is a guess.
            org = f"Unlisted ({addr.split('@')[-1]})"
            tier = 2

        ev = build_event(org, subject, text, msg.get("webLink"), today, "inbox", tier, src)
        if ev:
            events.append(ev)
    return events, errors


def fetch_via_firecrawl(url):
    """Some association sites serve 403 to datacenter IPs. The page loads fine from
    a laptop and fails from a GitHub runner, which is how DFW CAI behaved on the
    first live run. Firecrawl is already used for the same reason in bid-monitor."""
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["html"]}, timeout=60)
        r.raise_for_status()
        return (r.json().get("data") or {}).get("html")
    except requests.RequestException:
        return None


def scan_sites(today):
    events, errors = [], []
    for src in SOURCES:
        for url in src["urls"]:
            html = None
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                html = r.text
            except requests.RequestException as e:
                html = fetch_via_firecrawl(url)
                if not html:
                    errors.append(f"{src['org']} ({url}): {e}"
                                  f"{' [Firecrawl fallback also failed]' if os.environ.get('FIRECRAWL_API_KEY') else ' [no FIRECRAWL_API_KEY set]'}")
                    continue
            try:
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                # Blocks likely to hold one event each; fall back to whole page.
                blocks = soup.select("[class*=event], [class*=Event], li, article") or [soup]
                for block in blocks[:120]:
                    text = block.get_text(" ", strip=True)
                    if not (60 < len(text) < 2500) or not looks_like_event(text):
                        continue
                    link = block.find("a", href=True)
                    href = requests.compat.urljoin(url, link["href"]) if link else url
                    title = (link.get_text(strip=True) if link else text[:80]) or text[:80]
                    ev = build_event(src["org"], title, text, href, today, "web", src["tier"], src)
                    if ev:
                        events.append(ev)
            except Exception as e:  # parsing is best-effort; never kill the run
                errors.append(f"{src['org']} parse ({url}): {e}")
    return events, errors


# ---------------------------------------------------------------- output

def ticket_flag(ev):
    """Short bracket tag naming the reason she could miss this one.

    Outlook shows roughly the first 30 characters of a title in month view, so the
    money and the cutoff go in front of the event name, not after the org.
    """
    if not ev.get("needs_ticket"):
        return ""
    cost = f"${ev['costs'][0][0]:,.0f}" if ev.get("costs") else ""
    by = (datetime.fromisoformat(ev["deadline_iso"]).strftime("%b %-d").upper()
          if ev.get("deadline_iso") else "")
    if cost and by:
        return f"[TICKET {cost} BY {by}]"
    if cost:
        return f"[TICKET {cost}]"
    if by:
        return f"[REGISTER BY {by}]"
    return "[REGISTER AHEAD]"


def event_label(ev):
    """One string used as both the calendar title and the email subject, so the
    invite she accepts and the mail it arrived in cannot disagree.

    "Unlisted (shared1.ccsend.com)" is a parser diagnostic, not an organization,
    and it has no business in a calendar title - it stays in the email body where
    it tells her the org is a guess.
    """
    label = ev["title"]
    org = ev.get("org") or ""
    if org and not org.startswith("Unlisted"):
        label = f"{label} - {org}"
    return f"{ticket_flag(ev)} {label}".strip()


def make_ics(ev):
    """All-day unless the listing gave a time, in which case a 2-hour block."""
    start = datetime.fromisoformat(ev["start"])
    uid = f"{event_key(ev)}@blackhilltx.com"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if ev["has_time"]:
        dt_start = f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}"
        dt_end = f"DTEND:{(start + timedelta(hours=2)).strftime('%Y%m%dT%H%M%S')}"
    else:
        dt_start = f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}"
        dt_end = f"DTEND;VALUE=DATE:{(start + timedelta(days=1)).strftime('%Y%m%d')}"

    def esc(s):
        return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    # The action goes first. She opens this on a phone, where the description is
    # truncated, so "register by the 20th" has to survive the truncation.
    desc_lines = []
    if ev.get("needs_ticket"):
        act = "ACTION NEEDED: register/buy in advance"
        if ev.get("deadline_iso"):
            act += " by " + datetime.fromisoformat(ev["deadline_iso"]).strftime("%B %-d")
        if ev["costs"]:
            act += " - " + "; ".join(f"${v:,.0f}" for v, _ in ev["costs"])
        desc_lines += [act, ""]
    elif ev["costs"]:
        desc_lines += ["Cost: " + "; ".join(f"${v:,.0f}" for v, _ in ev["costs"]), ""]
    if ev["url"]:
        desc_lines += [f"Registration and details: {ev['url']}", ""]
    desc_lines.append(ev["snippet"])
    desc = "\n".join(desc_lines)

    # An alarm on a ticketed event fires against the REGISTRATION cliff, not the
    # event: a reminder the morning of a sold-out luncheon is useless.
    if ev.get("needs_ticket"):
        if ev.get("deadline_iso"):
            warn = datetime.fromisoformat(ev["deadline_iso"]) - timedelta(days=1)
            trigger = f"TRIGGER;VALUE=DATE-TIME:{warn.strftime('%Y%m%dT140000Z')}"
        else:
            trigger = "TRIGGER:-P7D"
        alarm_text = f"Register / buy ticket: {ev['title']}"
    else:
        trigger, alarm_text = "TRIGGER:-P1D", ev["title"]
    alarm = ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(alarm_text)}",
             trigger, "END:VALARM"]

    # These are candidates, not commitments. Marking them busy would black out her
    # calendar with events she has not decided to attend.
    categories = "Networking" + (",Ticket Required" if ev.get("needs_ticket") else "")

    # METHOD:REQUEST, not PUBLISH. A PUBLISH .ics is a valid calendar file but
    # Outlook and Superhuman render it as a plain file attachment with no
    # "add to calendar" affordance, so the events sat there unusable. REQUEST
    # with the recipient as ATTENDEE renders as a real invitation with
    # Accept/Decline. The organizer is our own sending address, so any responses
    # go to the automation mailbox and reach nobody else.
    organizer = os.environ.get("GMAIL_EMAIL", "noreply@blackhilltx.com").strip()
    attendee_lines = [
        f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
        f"RSVP=TRUE:mailto:{r}" for r in RECIPIENTS
    ]
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Black Hill//Events Monitor//EN",
        "CALSCALE:GREGORIAN", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{stamp}", "SEQUENCE:0", "STATUS:CONFIRMED",
        dt_start, dt_end,
        f"ORGANIZER;CN=Black Hill Events Monitor:mailto:{organizer}",
        *attendee_lines,
        f"SUMMARY:{esc(event_label(ev))}",
        f"LOCATION:{esc(ev['address'] or 'See registration link')}",
        f"DESCRIPTION:{esc(desc)}",
        f"CATEGORIES:{categories}",
        "TRANSP:TRANSPARENT", "X-MICROSOFT-CDO-BUSYSTATUS:FREE",
        *alarm,
        "END:VEVENT", "END:VCALENDAR",
    ])


def render(events, errors, monthly, heading=None):
    today = datetime.now(timezone.utc)
    # Tier first (a property-manager luncheon beats a peer webinar), then travel
    # (a far event is a whole day), then date.
    TIER_LABEL = {1: "Customers are here", 2: "Adjacent / BD", 3: "Peer industry"}
    TRAVEL_RANK = {"near": 0, "unknown": 1, "far": 2}
    events = sorted(events, key=lambda e: (e.get("tier", 2),
                                           TRAVEL_RANK.get(e.get("travel"), 1),
                                           e["date_iso"]))
    title = heading or ("Upcoming Industry Events" if monthly else "New Event Found")

    plain = ["Black Hill Events Monitor", f"{title} - {today.strftime('%B %d, %Y')}", ""]
    rows = []
    for ev in events:
        d = datetime.fromisoformat(ev["start"])
        days_out = (d.date() - today.date()).days
        when = d.strftime("%A, %B %-d") + (d.strftime(" at %-I:%M %p") if ev["has_time"] else "")
        cost = "; ".join(f"${v:,.0f} <span style='color:#777'>({c[:70]})</span>"
                         for v, c in ev["costs"]) or "Not listed - call to confirm"
        cost_plain = "; ".join(f"${v:,.0f}" for v, _ in ev["costs"]) or "Not listed"
        addr = ev["address"] or "Address not listed"
        maps = (f"<a href='https://www.google.com/maps/search/?api=1&query="
                f"{requests.utils.quote(ev['address'])}' style='color:#8a6d3b'>Map</a>"
                if ev["address"] else "")

        badges = ""
        if ev.get("needs_ticket"):
            badges += ("<span style='background:#fdf3d7;color:#7a5c11;font-size:11px;"
                       "padding:2px 7px;border-radius:9px;margin-left:6px'>"
                       f"{html_mod.escape(ticket_flag(ev).strip('[]'))}</span>")
        if ev.get("women"):
            badges += ("<span style='background:#f3e8f7;color:#6b3f7a;font-size:11px;"
                       "padding:2px 7px;border-radius:9px;margin-left:6px'>Women-focused</span>")
        if ev.get("travel") == "far":
            badges += ("<span style='background:#fdecea;color:#8a3028;font-size:11px;"
                       f"padding:2px 7px;border-radius:9px;margin-left:6px'>Long drive"
                       f"{' - ' + ev['city'] if ev.get('city') else ''}</span>")
        elif ev.get("travel") == "near":
            badges += ("<span style='background:#eaf5ea;color:#2c6b34;font-size:11px;"
                       f"padding:2px 7px;border-radius:9px;margin-left:6px'>Close by"
                       f"{' - ' + ev['city'] if ev.get('city') else ''}</span>")

        flags_plain = ""
        if ev.get("needs_ticket"):
            flags_plain += f" {ticket_flag(ev)}"
        if ev.get("women"):
            flags_plain += " [WOMEN-FOCUSED]"
        if ev.get("travel") == "far":
            flags_plain += f" [LONG DRIVE{' - ' + ev['city'] if ev.get('city') else ''}]"

        plain.append(f"- [{ev['org']}] {ev['title']}{flags_plain}\n  {when} ({days_out} days out)\n"
                     f"  {addr}\n  Cost: {cost_plain}\n  {ev['url'] or ''}")
        rows.append(f"""
        <tr><td style="padding:14px 12px;border-bottom:1px solid #e8e4dd;vertical-align:top">
          <div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#8a6d3b;font-weight:700">{html_mod.escape(ev['org'])}
            <span style="color:#aaa;font-weight:400;text-transform:none;letter-spacing:0">&middot; {TIER_LABEL.get(ev.get('tier', 2), '')}</span></div>
          <div style="font-size:16px;font-weight:700;color:#1a1a1a;margin:3px 0 6px">{html_mod.escape(ev['title'])} {badges}</div>
          <div style="font-size:14px;color:#333"><b>{when}</b> &nbsp;<span style="color:#777">({days_out} days out)</span></div>
          <div style="font-size:14px;color:#333;margin-top:3px">{html_mod.escape(addr)} {maps}</div>
          <div style="font-size:14px;color:#333;margin-top:3px">Cost: {cost}</div>
          {f'<div style="margin-top:7px"><a href="{html_mod.escape(ev["url"])}" style="color:#8a6d3b;font-weight:600">Registration &amp; details</a></div>' if ev.get('url') else ''}
        </td></tr>""")

    if not events:
        plain.append("Nothing new found this run.")
        rows.append("<tr><td style='padding:16px;color:#777'>Nothing new found this run.</td></tr>")

    footer = ""
    if errors:
        plain += ["", "Sources that failed this run:"] + [f"  - {e}" for e in errors]
        items = "".join(f"<li>{html_mod.escape(e)}</li>" for e in errors)
        footer = (f"<div style='margin-top:18px;padding:12px;background:#fdf6e3;"
                  f"border-left:3px solid #d4a017;font-size:12px;color:#665'>"
                  f"<b>Sources that failed this run</b><ul style='margin:6px 0 0;padding-left:18px'>{items}</ul></div>")

    html = f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:660px;margin:0 auto;padding:22px;background:#fffdf8">
      <h1 style="font-size:21px;color:#1a1a1a;margin:0 0 4px">{title}</h1>
      <p style="color:#777;font-size:13px;margin:0 0 16px">Black Hill Events Monitor &middot; {today.strftime('%B %d, %Y')} &middot; next {LOOKAHEAD_DAYS} days</p>
      <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e8e4dd">{''.join(rows)}</table>
      {footer}
      <p style="color:#999;font-size:11px;margin-top:18px">Black Hill Events Monitor. Each event arrives as its own separate calendar invite; this summary carries no attachments.</p>
    </div>"""
    return "\n".join(plain), html


def send_message(subject, plain, html, ics=None, ics_name="event.ics"):
    """Send one email, carrying at most ONE calendar part. The single-part limit
    is the whole point - see send_invites."""
    user = os.environ.get("GMAIL_EMAIL", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not (user and pw):
        print("GMAIL_EMAIL / GMAIL_APP_PASSWORD not set; skipping send", file=sys.stderr)
        return False

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Events Monitor", user))
    msg["To"] = ", ".join(RECIPIENTS)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    if ics:
        part = MIMEBase("text", "calendar", method="REQUEST", name=ics_name)
        part.set_payload(ics)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=ics_name)
        msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, RECIPIENTS, msg.as_string())
    return True


def send_digest(subject, plain, html):
    """The monthly reading list. No calendar part, on purpose: a digest that
    carries invitations is the bug this file was split up to fix."""
    return send_message(subject, plain, html)


def send_invites(events):
    """One email, one event, one invite.

    Outlook collapses a message carrying several text/calendar REQUEST parts into
    a SINGLE meeting request. The September digest went out with sixteen attached
    and arrived as one invite titled after whichever part Outlook picked, which
    made fifteen events unbookable and is why this loops instead of batching.
    However valid the MIME, no mail client renders more than one invitation per
    message.

    Soonest first: the per-run cap defers the tail to the next run, and an event
    nine days out cannot wait behind a November webinar.
    """
    sent = []
    for ev in sorted(events, key=lambda e: e["date_iso"])[:MAX_INVITES_PER_RUN]:
        subject = event_label(ev)[:160]
        plain, html = render([ev], [], monthly=False, heading=subject)
        name = re.sub(r"[^A-Za-z0-9]+", "-", f"{ev['org']}-{ev['date_iso']}").strip("-")[:60]
        try:
            if send_message(subject, plain, html, ics=make_ics(ev),
                            ics_name=f"{name or 'event'}.ics"):
                sent.append(ev)
        except Exception as e:
            # One bad address or one SMTP hiccup must not cost the other invites.
            # An event that failed here stays out of `invited`, so the next run
            # retries it.
            print(f"Invite failed for {ev['title'][:60]}: {e}", file=sys.stderr)
    return sent


# ---------------------------------------------------------------- main

def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--monthly", action="store_true", help="Full look-ahead digest")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true", help="Check auth and source reachability")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    today = datetime.now(timezone.utc)

    if args.test:
        print("Graph auth:", "OK" if graph_token() else "FAILED")
        for src in SOURCES:
            for url in src["urls"]:
                try:
                    r = requests.get(url, headers=HEADERS, timeout=20)
                    print(f"  {src['org']:28} {url} -> {r.status_code}")
                except requests.RequestException as e:
                    print(f"  {src['org']:28} {url} -> FAILED {e}")
        return 0

    state = load_state()
    inbox_events, inbox_errors = scan_inbox(today)
    site_events, site_errors = scan_sites(today)
    errors = inbox_errors + site_errors

    # Inbox first so its richer detail wins the dedupe against a thin web listing.
    merged = {}
    for ev in inbox_events + site_events:
        merged.setdefault(event_key(ev), ev)

    # event_key includes the title, so one listing scraped twice - once as the link
    # text, once as the surrounding blurb - survives as two entries with the same
    # org and date. That was tolerable when everything shared one digest email; now
    # that each entry costs its own calendar invite it is a duplicate on her
    # calendar. Collapse by org+date, keeping the shorter title: the link text is
    # the event's name, the blurb is a paragraph that happens to start with it.
    by_day = {}
    for key, ev in merged.items():
        slot = (ev["org"].lower(), ev["date_iso"])
        best = by_day.get(slot)
        if best is None or len(ev["title"]) < len(merged[best]["title"]):
            by_day[slot] = key
    merged = {k: merged[k] for k in by_day.values()}

    seen = set(state.get("seen", []))
    new_keys = [k for k in merged if k not in seen]

    # `invited` is tracked separately from `seen`. An event can be known (so it
    # stops being announced as new) yet still owe Evelin a calendar invite,
    # which is exactly the state everything found before this change is in.
    invited = set(state.get("invited", []))
    to_invite = [ev for k, ev in merged.items() if k not in invited]
    deferred = max(0, len(to_invite) - MAX_INVITES_PER_RUN)

    to_report = list(merged.values()) if args.monthly else [merged[k] for k in new_keys]
    subject = f"Industry Events - {today.strftime('%B %Y')} ({len(to_report)} upcoming)"
    plain, html = render(to_report, errors, args.monthly)
    if deferred:
        note = (f"{deferred} more invite(s) held back this run and sent on the next one "
                f"(cap {MAX_INVITES_PER_RUN}).")
        plain += "\n\n" + note
        html = html.replace("</div>", f"<p style='color:#999;font-size:11px'>{note}</p></div>", 1)

    if args.dry_run:
        print(plain)
        for ev in sorted(to_invite, key=lambda e: e["date_iso"])[:MAX_INVITES_PER_RUN]:
            print(f"  [invite] {event_label(ev)}")
        print(f"\n[dry-run] {len(merged)} known, {len(new_keys)} new, "
              f"{len(to_invite)} uninvited ({deferred} deferred), {len(errors)} source errors")
        return 0

    # Silent when there is nothing to do, same contract as bid-monitor.
    if not to_invite and not args.monthly:
        print(f"No new events ({len(merged)} known). No email sent.")
        save_state({**state, "seen": list(seen | set(merged)), "invited": list(invited)})
        return 0

    # Invites first. If the digest send fails, she still has the bookable events.
    sent = send_invites(to_invite)
    print(f"Sent {len(sent)} invite(s){f', {deferred} deferred' if deferred else ''}")

    # The digest is a monthly reading list only, and carries no calendar parts.
    # Attaching them here is what produced one invite for sixteen events.
    if args.monthly and send_digest(subject, plain, html):
        print(f"Sent digest: {subject}")

    state["seen"] = list(seen | set(merged))
    state["invited"] = list(invited | {event_key(ev) for ev in sent})
    if args.monthly:
        state["last_monthly"] = today.strftime("%Y-%m-%d")
    save_state(state)

    # Surface persistent breakage without failing the run over one flaky site.
    if errors and len(errors) >= sum(len(s["urls"]) for s in SOURCES):
        print("All sources failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    with db.track("events-monitor"):
        sys.exit(main())
