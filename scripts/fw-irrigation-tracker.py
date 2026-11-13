#!/usr/bin/env python3
"""FW irrigation tracker — Black Hill Landscaping.

Every opportunity on the "FW Irrigation Repairs" property (PropertyID 1096),
one row each, with the stage it is at right now: estimate in progress, sent
for PO, scheduled, complete, invoiced, sent, paid. Emails Evelin and Carlos an
.xlsx twice a day (6:45am and 3pm Central, Mon-Fri).

Stage comes from live Aspire data on every run, so nothing is stored between
runs. "Changed" marks rows whose opportunity, ticket or invoice was touched
in the last 24 hours.

cron-job.org dispatches the workflow at 6:45 and 15:00 Central; GitHub's own
cron is only a backstop, since its scheduler drops runs in this repo. The
script sends only on weekdays when the Central hour is 6 or 15, and only once per slot:
the first run records the slot in Supabase and every later run skips it.

Usage:
  python3 fw-irrigation-tracker.py              # Run and email
  python3 fw-irrigation-tracker.py --force      # Skip the Central-hour guard
  python3 fw-irrigation-tracker.py --dry-run    # Build the file, don't email
  python3 fw-irrigation-tracker.py --draft-test # Print both drafts, create nothing
  python3 fw-irrigation-tracker.py --seed-completions  # Mark worked repairs as emailed
  python3 fw-irrigation-tracker.py --force --drafts=schedule,completion
"""

import io, json, os, re, sys, signal, smtplib, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from html import escape

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

from zoneinfo import ZoneInfo


def _timeout_handler(signum, frame):
    log("TIMEOUT: Script exceeded time limit")
    sys.exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(300)

# --- Config ---
CONFIG_FILE = os.path.expanduser("~/.config/aspire/config.json")
RECIPIENTS = [
    "evelin@blackhilltx.com",
    "branchadmin@blackhilltx.com",   # Carlos Garcia
]
ASPIRE_PORTAL = "https://cloud.youraspire.com"
FW_PROPERTY_ID = 1096
SEND_HOURS = (6, 15)          # Central; slots are 6:45am and 3pm
ORPHAN_CANCEL_DAYS = 7         # unmatched City cancel notes older than this are dropped
STALE_COMPLETE_DAYS = 3       # complete but not invoiced longer than this gets flagged
CENTRAL = ZoneInfo("America/Chicago")

DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv
# Build both drafts from live data and print them; no Graph, no state, no email.
DRAFT_TEST = "--draft-test" in sys.argv
# Mark every repair worked so far as already emailed, then exit.
SEED_COMPLETIONS = "--seed-completions" in sys.argv
# Which drafts to make on a --force run: --drafts=schedule,completion (or none).
# Unset, the slot decides: 15:00 makes the schedule draft, 6:00 the
# completion draft plus the schedule draft when an EMERGENCY job is waiting.
DRAFTS_ARG = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--drafts=")), None)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --- Aspire API ---

def load_config():
    """Prefer ASPIRE_REPORTING_* env vars (GH Actions), fall back to config file."""
    client_id = os.environ.get("ASPIRE_REPORTING_CLIENT_ID")
    secret = os.environ.get("ASPIRE_REPORTING_SECRET")
    if client_id and secret:
        return {"api_base_url": "https://cloud-api.youraspire.com",
                "api_client_id": client_id, "api_secret": secret}
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    if cfg.get("reporting_client_id"):
        return {"api_base_url": cfg.get("api_base_url", "https://cloud-api.youraspire.com"),
                "api_client_id": cfg["reporting_client_id"],
                "api_secret": cfg["reporting_secret"]}
    return cfg


class Aspire:
    def __init__(self, config):
        self.base = config["api_base_url"].rstrip("/")
        data = json.dumps({"ClientId": config["api_client_id"],
                           "Secret": config["api_secret"]}).encode()
        req = urllib.request.Request(f"{self.base}/Authorization", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.token = json.loads(resp.read().decode())["Token"]

    def get(self, endpoint, odata_filter):
        url = f"{self.base}/{endpoint}?$filter={urllib.parse.quote(odata_filter)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"{endpoint} {e.code}: {body[:300]}") from e

    def get_in(self, endpoint, field, ids, chunk=20):
        """Fetch rows where field is any of ids. Aspire caps a filter at 100
        OData nodes, and each "x eq n" costs several, so chunk at 20."""
        out, ids = [], sorted(set(i for i in ids if i is not None))
        for i in range(0, len(ids), chunk):
            part = " or ".join(f"{field} eq {x}" for x in ids[i:i + chunk])
            out += self.get(endpoint, part)
        return out


# --- Shaping ---

def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def day(s):
    d = parse_dt(s)
    return d.date() if d else None


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s.replace("&nbsp;", " ")).strip()


def address_of(name):
    parts = re.split(r"\s+-\s+", name or "")
    return parts[-1].strip().rstrip(",") if len(parts) > 1 else ""


def po_of(opp, desc):
    if opp.get("CustomerPONum"):
        return str(opp["CustomerPONum"]).strip()
    m = re.search(r"\bPO\b\s*#?:?\s*(\d{4,})", desc)
    return m.group(1) if m else ""


ASSESSMENT_SERVICE_ID = 149
INVOICE_DUE_DAYS = 30
# From 2026-10-23 the contract is built and won before the assessment, so Jon
# clocks straight onto it and there is no quick ticket or time move. The
# repair waits for the City's PO, sent after his parts are added.
WON_FIRST_START = "2026-10-23"
REPRICE_TOLERANCE = 5.00      # actual vs PO difference that needs a revised PO

# The City's sequence, one row per job:
#   request email -> quick ticket (assessment visit) -> Jon assesses
#   -> "FW Assessment & Repair" contract built and sent -> PO received (won)
#   -> quick-ticket time moved to the contract -> repair scheduled and done
#   -> revised PO if the actual differs -> invoiced -> paid within 30 days.
STAGES = {
    "00": "New request - create opportunity",
    "01": "Assessment - needs scheduling",
    "02": "Assessment scheduled",
    "03": "Assessed - build estimate",
    "04": "Estimate in progress",
    "05": "Estimate sent - awaiting PO",
    "06": "PO received - mark Won, move time, schedule",
    "07": "Repair - needs scheduling",
    "08": "Repair scheduled",
    "09": "Repair worked - not marked complete",
    "10": "Complete - ready to invoice",
    "11": "Invoiced - not sent",
    "12": "Invoice sent - awaiting payment",
    "13": "Paid",
}


# Jon sometimes arrives for the assessment and the repair has already been
# made. Then there is no repair: the contract bills the $100 assessment only,
# and its proposal description says so.
ALREADY_REPAIRED = re.compile(
    r"repair\w*\s+(was|were|has been|had been)\s+(already\s+)?(completed|made|done)"
    r"|completed by another|work was completed|trip charge only"
    r"|already\s+(been\s+)?(repaired|fixed|completed|made)", re.I)


def stage(code):
    return f"{code}. {STAGES[code]}"


def kind_of(name):
    """Three opportunity shapes are on this property:
    "FW Irrigation Assessment - <addr>"  the quick ticket Jon clocks into,
    "FW Assessment & Repair - <addr>"    the contract sent for the PO,
    "FW Irrigation Repairs - <addr>"     the older repair-only estimate."""
    n = (name or "").lower()
    if "assessment" in n and "repair" in n:
        return "Assessment + Repair"
    if "assessment" in n:
        return "Quick Ticket"
    return "Repair Only"


def addr_key(addr):
    """House number + first street word, so "11944 Yarmouth Ln" on the
    quick ticket matches "11944 YARMOUTH LANE" on the contract."""
    m = re.match(r"\s*(\d+)\s+([A-Za-z]+)", addr or "")
    return f"{m.group(1)} {m.group(2).lower()}" if m else None


def visit_state(ts, v_by_t, today):
    dates = sorted({day(v["ScheduledDate"]) for t in ts for v in v_by_t.get(t["WorkTicketID"], [])
                    if v.get("ScheduledDate")})
    return next((d for d in dates if d >= today), None), (dates[-1] if dates else None)


INBOX_LOOKBACK_DAYS = 45
EXPEDITE_RE = re.compile(r"\b(expedite[ds]?|emergency|safety issue)\b", re.I)
# The City calls a job off in a reply, e.g. "Please disregard this request. A
# supervisor confirmed it has already been repaired." (2026-10-30).
CANCEL_RE = re.compile(r"\b(disregard|cancel(?:l?ed|ling|lation)?|already (?:been )?(?:repaired|fixed)"
                       r"|no longer (?:need|required))\b", re.I)
CORRECTION_RE = re.compile(r"\b(?:send|sending|resend)\b.{0,30}\bcorrect", re.I)
# The City asks for a corrected quote before it will issue the PO, e.g.
# "Please revise po line that is off .01" (2026-11-04).
REVISE_RE = re.compile(r"\b(revise|revising|revision|redo(?:ing)? the quote|off by|"
                       r"not calculating correctly)\b", re.I)
NO_REVISE_RE = re.compile(r"\b(no need to (?:revise|redo)|(?:don'?t|doesn'?t|do not|does not) need (?:to be )?revis)", re.I)
# City replies often go only to the person who sent the proposal, not to the
# irrigation inbox (a PO and a revision request were missed this way, 11/2026).
EXTRA_MAILBOXES = [m.strip() for m in os.environ.get("FW_TRACKER_EXTRA_MAILBOXES",
                                                      "evelin@blackhilltx.com").split(",") if m.strip()]
CITY_DOMAIN = "@fortworthtexas.gov"


def inbox_pos():
    """PO numbers Fort Worth emailed to the irrigation inbox, keyed by work
    order and by address. A PO can land days after the quote went out, and
    Aspire knows nothing about it until someone marks the job Won, so the
    inbox is the earliest sign a repair is cleared to schedule. Parsing is
    shared with fw-irrigation-monitor.py so both read PO emails the same way.
    Returns ({}, {}) if the mailbox can't be read; the sheet still builds."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(
            "fw_monitor", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "fw-irrigation-monitor.py"))
        mon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mon)
        token = mon.get_token()
        since = (datetime.now(timezone.utc) - timedelta(days=INBOX_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        def fetch(box, cap):
            url = (f"https://graph.microsoft.com/v1.0/users/{box}/messages"
                   f"?$filter=receivedDateTime%20ge%20{since}"
                   f"&$select=subject,receivedDateTime,body,from,internetMessageId&$top=100")
            out = []
            while url and len(out) < cap:
                page = mon.graph_get(token, url) or {}
                out += page.get("value", [])
                url = page.get("@odata.nextLink")
            return out
        msgs = fetch(mon.MAILBOX, 1000)
    except Exception as e:
        log(f"Inbox PO check skipped: {e}")
        return {}, {}, {}, {}, []
    # Other mailboxes add only City mail, and skip copies already in the
    # irrigation inbox. A failure here must not lose the main inbox.
    seen = {m.get("internetMessageId") for m in msgs}
    for box in EXTRA_MAILBOXES:
        try:
            extra = [m for m in fetch(box, 3000)
                     if ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
                     .endswith(CITY_DOMAIN) and m.get("internetMessageId") not in seen]
        except Exception as e:
            log(f"Extra mailbox {box} skipped: {e}")
            continue
        seen.update(m.get("internetMessageId") for m in extra)
        msgs += extra
    by_wo, by_key, requests, rush, cancels = {}, {}, {}, {}, []
    for m in sorted(msgs, key=lambda m: m["receivedDateTime"]):
        text = mon.html_to_text((m.get("body") or {}).get("content", ""))
        kind = mon.classify(text)
        top = mon.top_of_message(text)
        # The City marks rush jobs "EXPEDITE" or "EMERGENCY"/"SAFETY ISSUE",
        # in the subject or at the top of a follow-up.
        if EXPEDITE_RE.search(m.get("subject", "")) or EXPEDITE_RE.search(top[:300]):
            r_wo = mon.parse_wo(text, m.get("subject", ""))
            subj = re.sub(r"\b\d{2}-\d{5,6}\b", " ", m.get("subject", ""))
            hit_addr = re.search(r"\b\d{3,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z]+", subj)
            r_key = addr_key(hit_addr.group(0)) if hit_addr else None
            for k in filter(None, (r_wo, r_key)):
                rush.setdefault(k, day(m["receivedDateTime"]))
        # Cancellations come from the City, never from our own replies. The
        # reply often names no job ("disregard this request"), so fall back
        # to the first WO quoted underneath it.
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        hit_c = CANCEL_RE.search(m.get("subject", "")) or CANCEL_RE.search(top)
        # "Disregard this request, I will send the correct one" is a resend,
        # not a cancellation (2026-11-02).
        if hit_c and CORRECTION_RE.search(top):
            hit_c = None
        if hit_c and not sender.endswith("@blackhilltx.com"):
            c_wo = mon.parse_wo(top, m.get("subject", ""))
            if not c_wo:
                q = mon.WO_RE.search(text[len(top):] if text.startswith(top) else text)
                c_wo = q.group(1) if q else None
            subj = re.sub(r"\b\d{2}-\d{5,6}\b", " ", m.get("subject", ""))
            hit_addr = re.search(r"\b\d{3,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z]+", subj + " " + top)
            sentence = next((x for x in re.split(r"(?<=[.!?])\s+|\s*\n\s*", top)
                             if CANCEL_RE.search(x)), "")
            cancels.append({"wo": c_wo, "key": addr_key(hit_addr.group(0)) if hit_addr else None,
                            "date": day(m["receivedDateTime"]), "quote": " ".join(sentence.split())[:140]})
        # "Only off by .06 ... no need to revise" is an approval, not a request.
        hit_r = REVISE_RE.search(top) and not NO_REVISE_RE.search(top)
        if hit_r and not hit_c and sender.endswith(CITY_DOMAIN):
            r_wo = mon.parse_wo(top, m.get("subject", ""))
            if not r_wo:
                q = mon.WO_RE.search(text[len(top):] if text.startswith(top) else text)
                r_wo = q.group(1) if q else None
            subj = re.sub(r"\b\d{2}-\d{5,6}\b", " ", m.get("subject", ""))
            hit_addr = re.search(r"\b\d{3,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z]+", subj + " " + top)
            sentence = next((x for x in re.split(r"(?<=[.!?])\s+|\s*\n\s*", top)
                             if REVISE_RE.search(x)), "")
            cancels.append({"wo": r_wo, "key": addr_key(hit_addr.group(0)) if hit_addr else None,
                            "date": day(m["receivedDateTime"]), "quote": " ".join(sentence.split())[:140],
                            "kind": "revise"})
        if kind == "quote":
            wo = mon.parse_wo(text, m.get("subject", ""))
            addr = mon.parse_address(text, m.get("subject", ""))
            subj = mon.parse_address("", m.get("subject", ""))
            if addr_key(subj) and addr_key(subj) != addr_key(addr):
                addr = f"{subj} / {addr}"      # City sometimes pastes the wrong one
            key = f"{wo}|{addr_key(addr)}"     # one WO can cover two houses
            if key and key not in requests:
                requests[key] = {"wo": wo, "address": addr, "date": day(m["receivedDateTime"])}
            continue
        if kind != "po":
            continue
        po = mon.PO_RE.search(top)
        wo = mon.parse_wo(top, m.get("subject", ""))
        addr = mon.parse_po_address(top, m.get("subject", ""))
        if not po:
            continue
        hit = {"po": po.group(1), "date": day(m["receivedDateTime"]), "wo": wo}
        if wo:
            by_wo[wo] = hit
        if addr_key(addr):
            by_key[addr_key(addr)] = hit
    log(f"Inbox: {len(msgs)} messages, {len(by_wo)} PO emails, {len(requests)} quote requests")
    return by_wo, by_key, requests, rush, cancels


# Raw data build_rows fetched, reused by the drafts so they need no second pull.
CTX = {}


def build_rows(api):
    opps = api.get("Opportunities", f"PropertyID eq {FW_PROPERTY_ID}")
    opp_ids = [o["OpportunityID"] for o in opps]
    tickets = api.get_in("WorkTickets", "OpportunityID", opp_ids)
    services = api.get_in("OpportunityServices", "OpportunityID", opp_ids)
    # The City checks that each printed unit price x qty equals the line total.
    # Aspire's own line (cost x 1.35) often misses by a cent, so FW estimates
    # carry small P/P overrides, and marking a job Won can re-price a line whose
    # override did not stick. Flag any active part line that no longer adds up.
    live_svc = [s["OpportunityServiceID"] for s in services if not s.get("RemovedOpportunityRevisionID")]
    line_math = {}
    svc_opp = {s["OpportunityServiceID"]: s["OpportunityID"] for s in services}
    for it in api.get_in("OpportunityServiceItems", "OpportunityServiceID", live_svc):
        qty, ext = it.get("ItemQuantity") or 0, it.get("ExtendedPrice") or 0
        if it.get("ItemType") == "Labor" or qty <= 1:
            continue
        unit = float(Decimal(str(ext / qty)).quantize(Decimal("0.01"), ROUND_HALF_UP))
        if abs(unit * qty - ext) > 0.005:
            line_math.setdefault(svc_opp[it["OpportunityServiceID"]], []).append(
                f"{it.get('ItemName')} {qty:g} x ${unit:.2f} = ${unit * qty:.2f}, not ${ext:.2f}")
    visits = api.get_in("WorkTicketVisits", "WorkTicketID", [t["WorkTicketID"] for t in tickets])
    # Jon's visit notes, e.g. "looks to be already completed", flag a job
    # where the repair was done before he got there.
    vnotes = api.get_in("WorkTicketVisitNotes", "WorkTicketVisitID", [v["WorkTicketVisitID"] for v in visits])
    note_by_t = {}
    for n in vnotes:
        note_by_t[n["WorkTicketID"]] = note_by_t.get(n["WorkTicketID"], "") + " " + (n.get("Note") or "")
    invoices = api.get("Invoices", f"PropertyID eq {FW_PROPERTY_ID}")
    times = api.get_in("WorkTicketTimes", "WorkTicketID", [t["WorkTicketID"] for t in tickets])
    # Re-approving an estimate after it was emailed resets Aspire's status to
    # Approved, so the emailed-proposal activity is the reliable "sent" signal.
    approved = [o["OpportunityID"] for o in opps if o["OpportunityStatusName"] == "Approved"
                or (o["OpportunityStatusName"] == "Won" and o["CreatedDateTime"][:10] >= WON_FIRST_START)]
    sent_on = {}
    for a in api.get_in("Activities", "OpportunityID", approved):
        if (a.get("Subject") or "").startswith("Proposal for your review") and a.get("SentDate"):
            sent_on[a["OpportunityID"]] = max(sent_on.get(a["OpportunityID"], ""), a["SentDate"])
    emailed = set(sent_on)
    log(f"Fetched {len(opps)} opportunities, {len(tickets)} tickets, "
        f"{len(visits)} visits, {len(invoices)} invoices")

    svc_id = {s["OpportunityServiceID"]: s.get("ServiceID") for s in services}
    # "FW Emergency During Normal Hours" and its kin mark a rush repair.
    emergency = {s["OpportunityID"] for s in services if not s.get("RemovedOpportunityRevisionID")
                 and "emergency" in (s.get("DisplayName") or "").lower()}
    CTX.update(times=times, visits=visits)
    has_assess = {s["OpportunityID"] for s in services if s.get("ServiceID") == ASSESSMENT_SERVICE_ID}
    # Parts go on a won contract through a change order, which adds a service
    # stamped with its revision; one still active means the estimate is built.
    revised = {s["OpportunityID"] for s in services
               if s.get("AddedOpportunityRevisionID") and not s.get("RemovedOpportunityRevisionID")}
    hours_by_t, t_by_opp, v_by_t, i_by_opp = {}, {}, {}, {}
    for tm in times:
        hours_by_t[tm["WorkTicketID"]] = hours_by_t.get(tm["WorkTicketID"], 0) + (tm.get("Hours") or 0)
    for t in tickets:
        t_by_opp.setdefault(t["OpportunityID"], []).append(t)
    for v in visits:
        v_by_t.setdefault(v["WorkTicketID"], []).append(v)
    for inv in invoices:
        if inv.get("InvoiceVoid") == "Y":
            continue
        # One invoice carries a line per invoice type (T&M + the Fixed Payment
        # assessment), so count it once per job, not once per line.
        for oid in {io_["OpportunityID"] for io_ in inv.get("InvoiceOpportunities") or []}:
            i_by_opp.setdefault(oid, []).append(inv)

    def hrs(ts):
        return round(sum(hours_by_t.get(t["WorkTicketID"], 0) for t in ts), 2)

    def is_assess(t):
        return svc_id.get(t.get("OpportunityServiceID")) == ASSESSMENT_SERVICE_ID

    # Group into jobs: each contract (or repair-only estimate) is a job, and
    # each quick ticket joins the newest contract at its address created
    # after it. A quick ticket with no contract yet is a job on its own.
    closed = ("Lost", "Canceled", "Cancelled")
    by_key = {}
    for o in opps:
        # Canceling a won contract leaves OpportunityStatusName "Won" and
        # records the cancel only in JobStatusName.
        if o.get("JobStatusName") in closed:
            o["OpportunityStatusName"] = o["JobStatusName"]
        o["_kind"] = kind_of(o["OpportunityName"])
        # An assessment-named opportunity that never got a ticket and was not
        # created Won is the assessment-only estimate sent for a PO, not a
        # quick ticket.
        if (o["_kind"] == "Quick Ticket" and o["OpportunityStatusName"] != "Won"
                and not t_by_opp.get(o["OpportunityID"])):
            o["_kind"] = "Assessment + Repair"
        # Quick tickets are created Won within minutes; one won days after it
        # was created waited on a PO, so it is that assessment-only contract.
        if o["_kind"] == "Quick Ticket" and o.get("WonDate") and o.get("CreatedDateTime"):
            won = datetime.fromisoformat(o["WonDate"][:19])
            made = datetime.fromisoformat(o["CreatedDateTime"][:19])
            if (won - made).total_seconds() > 12 * 3600:
                o["_kind"] = "Assessment + Repair"
        # An older repair-only estimate revised to add the $100 assessment
        # is now the full contract.
        if o["_kind"] == "Repair Only" and o["OpportunityID"] in has_assess:
            o["_kind"] = "Assessment + Repair"
        o["_key"] = addr_key(address_of(o["OpportunityName"]))
        by_key.setdefault(o["_key"] or f"#{o['OpportunityNumber']}", []).append(o)
    jobs = []
    for group in by_key.values():
        mains = sorted([o for o in group if o["_kind"] != "Quick Ticket"],
                       key=lambda o: o["CreatedDateTime"])
        qts = sorted([o for o in group if o["_kind"] == "Quick Ticket"],
                     key=lambda o: o["CreatedDateTime"])
        attached = {o["OpportunityID"]: [] for o in mains}
        loose = []
        for q in qts:
            later = [m for m in mains if m["CreatedDateTime"] >= q["CreatedDateTime"]
                     and m["OpportunityStatusName"] not in closed]
            if later:
                attached[later[0]["OpportunityID"]].append(q)
            else:
                loose.append(q)
        jobs += [(m, attached[m["OpportunityID"]]) for m in mains]
        # Duplicate quick tickets for one address (e.g. one never scheduled)
        # fold into the one that has a ticket.
        if loose:
            with_t = [q for q in loose if t_by_opp.get(q["OpportunityID"])] or loose
            jobs.append((None, with_t))

    now = datetime.now(timezone.utc)
    today = now.astimezone(CENTRAL).date()
    rows = []
    for main_opp, qts in jobs:
        qt_tickets = [t for q in qts for t in t_by_opp.get(q["OpportunityID"], [])]
        qt_invs = [i for q in qts for i in i_by_opp.get(q["OpportunityID"], [])]
        qt_next, qt_last = visit_state(qt_tickets, v_by_t, today)
        qt_hours = hrs(qt_tickets)
        qt_open = [t for t in qt_tickets if t["WorkTicketStatusName"] not in ("Complete", "Canceled")]
        qt_done = [day(t["CompleteDate"]) for t in qt_tickets if t.get("CompleteDate")]

        ts = sorted(t_by_opp.get(main_opp["OpportunityID"], []), key=lambda t: t["WorkTicketNumber"]) \
            if main_opp else []
        assess_t = [t for t in ts if is_assess(t)]
        repair_t = [t for t in ts if not is_assess(t)]
        invs = sorted(i_by_opp.get(main_opp["OpportunityID"], []), key=lambda i: i["InvoiceNumber"]) \
            if main_opp else []
        r_next, r_last = visit_state(repair_t, v_by_t, today)
        r_hours = hrs(repair_t)
        r_open = [t for t in repair_t if t["WorkTicketStatusName"] not in ("Complete", "Canceled")]
        r_done = [day(t["CompleteDate"]) for t in repair_t if t.get("CompleteDate")]
        complete = max(r_done) if repair_t and len(r_done) == len(repair_t) else None
        a_hours = hrs(assess_t)
        tm_calc = sum(t.get("TMCalcAmount") or 0 for t in ts)
        tm_override = sum(t.get("TMOverrideAmount") or 0 for t in ts) or None
        est = (main_opp or {}).get("EstimatedDollars") or 0
        status = main_opp["OpportunityStatusName"] if main_opp else None
        desc = strip_html((main_opp or qts[0]).get("ProposalDescription1"))
        for q in qts:
            desc += " " + strip_html(q.get("ProposalDescription1"))
        main_desc = strip_html(main_opp.get("ProposalDescription1")) if main_opp else ""
        jon_notes = " ".join(note_by_t.get(t["WorkTicketID"], "") for t in qt_tickets + ts)
        assess_only = bool(main_opp and (ALREADY_REPAIRED.search(main_desc)
                                         or ALREADY_REPAIRED.search(jon_notes)))
        billed_on_qt = bool(main_opp and qt_invs and not invs)
        time_moved = a_hours > 0 or (qt_tickets and qt_hours == 0 and not qt_open)
        # Every repair ticket canceled (already repaired, or the City called it
        # off): only the assessment is left to bill, so skip the estimate step.
        repair_canceled = bool(repair_t) and all(t["WorkTicketStatusName"] == "Canceled" for t in repair_t)
        if repair_canceled:
            a_done = [day(t["CompleteDate"]) for t in assess_t if t.get("CompleteDate")]
            complete = max(a_done) if assess_t and len(a_done) == len(assess_t) else None
        won_first = bool(main_opp and status == "Won" and not qts
                         and main_opp["CreatedDateTime"][:10] >= WON_FIRST_START
                         and not po_of(main_opp, desc) and not repair_canceled)
        flags = []

        if main_opp is None:
            if qt_open and qt_next and qt_hours == 0:
                code = "02"
            elif qt_hours > 0 or (qt_tickets and not qt_open):
                code = "03"
                flags.append("Build the Assessment & Repair estimate")
            elif qt_last:
                code = "02"
                flags.append(f"Assessment visit was {qt_last:%m/%d}, no time logged; confirm he went")
            else:
                code = "01"
                if qt_tickets:
                    flags.append("Quick ticket has no visit scheduled")
        elif status in closed:
            continue        # lost or canceled: off the tracker
        elif billed_on_qt:
            # The City was billed on the quick ticket. The contract only holds
            # Jon's time (an invoiced ticket can't take time moved onto it), so
            # it is parked and must never be invoiced.
            code = ("11" if any(i.get("EmailStatus") != "Sent" for i in qt_invs) else
                    "12" if sum(i.get("AmountRemaining") or 0 for i in qt_invs) > 0.005 else "13")
            flags.append(f"Billed on quick ticket invoice {', '.join(str(i['InvoiceNumber']) for i in qt_invs)}. "
                         f"Contract #{main_opp['OpportunityNumber']} only holds Jon's time "
                         f"({hrs(ts)} hr); leave its ticket open and do not invoice it")
        elif status == "Approved" and main_opp["OpportunityID"] in emailed:
            code = "05"
            flags.append("Emailed to the City, but Aspire still shows Approved; mark it Delivered")
        elif status in ("Bidding", "Approved"):
            code = "04"
            if main_opp["CreatedDateTime"][:10] >= WON_FIRST_START and not qts:
                flags.append("Mark it Won and put the assessment on Jon's schedule")
            elif status == "Approved":
                flags.append("Approved but not emailed to the City yet")
        elif status == "Delivered":
            code = "05"
        elif status != "Won":
            code = None
        elif won_first:
            wf_next, wf_last = visit_state(assess_t, v_by_t, today)
            wf_done = [day(t["CompleteDate"]) for t in assess_t if t.get("CompleteDate")]
            sent = sent_on.get(main_opp["OpportunityID"], "")
            if not assess_t or len(wf_done) < len(assess_t):
                code = "02" if wf_next or wf_last else "01"
                if not (wf_next or wf_last):
                    flags.append("Put the assessment on Jon's schedule")
                elif wf_last and a_hours > 0:
                    flags.append(f"Jon logged {a_hours} hr on the assessment; mark it complete")
            elif sent and day(sent) >= max(wf_done):
                code = "05"
            else:
                code = "03"
                if main_opp["OpportunityID"] in revised:
                    flags.append("Parts are on the estimate; email the proposal to the City for the PO "
                                 "(Revision Through = latest); keep the repair off the schedule until the PO is in")
                else:
                    flags.append("Add Jon's parts to the repair service and send to the City for the PO; "
                                 "keep the repair off the schedule until the PO is in")
        elif qts and not time_moved and not po_of(main_opp, desc):
            # Marked Won before the City sent a PO: still waiting on it.
            code = "05"
            flags.append(f"Won in Aspire but no PO yet; when it comes, move quick ticket time ({qt_hours} hr) "
                         f"to the contract")
        elif qts and not time_moved:
            code = "06"
            flags.append(f"Move quick ticket time ({qt_hours} hr) to the contract")
        elif assess_only and not r_open and not invs:
            code = "10"
        elif not repair_t and not ts:
            code = "07"
            flags.append("Won, but no work ticket yet")
        # A single-ticket contract priced above the assessment usually still owes
        # a repair, unless it was a trip charge that has already been invoiced.
        elif r_open or (assess_t and not repair_t and est > 100.005 and not invs):
            if r_next:
                code = "08"
            elif r_last and r_hours > 0:
                code = "09"
                flags.append(f"Jon logged {r_hours} hr, visit {r_last:%m/%d}; check parts vs PO and mark complete")
            elif r_last:
                code = "08"
                flags.append(f"Repair visit was {r_last:%m/%d}, no time logged; confirm he went")
            else:
                code = "07"
                flags.append("Repair not on the schedule yet")
        elif repair_canceled and not complete and not invs:
            code = "02"
            flags.append("Repair canceled; mark the assessment complete so it can be invoiced")
        elif not invs:
            code = "10"
            if repair_canceled and not po_of(main_opp, desc):
                flags.append("Repair canceled; assessment only. Get the City's PO for the assessment, then invoice it")
            if complete and (today - complete).days > STALE_COMPLETE_DAYS:
                flags.append(f"Complete {(today - complete).days} days, not invoiced")
        elif any(i.get("EmailStatus") != "Sent" for i in invs):
            code = "11"
            flags.append("Invoice not emailed yet")
        elif sum(i.get("AmountRemaining") or 0 for i in invs) > 0.005:
            code = "12"
            age = (today - day(invs[-1]["InvoiceDate"])).days
            if age > INVOICE_DUE_DAYS:
                flags.append(f"Invoice is {age} days old, past the 30 day terms")
        else:
            code = "13"

        stage_label = stage(code) if code else f"00. Closed - {status}"
        # Only the new process (Assessment & Repair contract, PO after the
        # quote) needs a revised PO. Old repair-only jobs bill at actual.
        # Under the PO just bills actual; only an overrun needs a new PO.
        if main_opp and code and "03" <= code <= "08" and line_math.get(main_opp["OpportunityID"]):
            flags.append("Estimate line doesn't add up, fix the P/P before it goes to the City: "
                         + "; ".join(line_math[main_opp["OpportunityID"]]))
        if code in ("09", "10") and est and tm_calc and tm_calc - est > REPRICE_TOLERANCE \
                and main_opp["_kind"] == "Assessment + Repair":
            flags.append(f"Actual ${tm_calc:,.2f} is over the ${est:,.2f} PO; get a revised PO")
        if code == "10" and any((t.get("TMOverrideAmount") or 0) > (t.get("TMCalcAmount") or 0) + 0.005
                                for t in ts):
            flags.append("Override is above T&M (minimum charge?)")
        if main_opp and main_opp["_kind"] == "Assessment + Repair" and code and int(code) >= 6:
            qt_billed = [i for i in qt_invs]
            if billed_on_qt:
                pass
            elif qt_billed:
                flags.append(f"Quick ticket was invoiced ({', '.join(str(i['InvoiceNumber']) for i in qt_billed)}) "
                             f"and the contract bills the assessment too; $100 billed twice")
            elif any((q.get("EstimatedDollars") or 0) > 0 for q in qts):
                flags.append("Quick ticket is priced $100; completing it will invoice the assessment "
                             "a second time")

        if main_opp and main_opp["_kind"] == "Repair Only" and not qts:
            assess_date = None
        else:
            assess_date = max(qt_done) if qt_done and not qt_open else (qt_last if qt_hours > 0 else None)
        # Once the quick ticket's time is moved and the QT deleted, the
        # assessment lives on the contract's own assessment ticket.
        a_next, a_last = visit_state(assess_t, v_by_t, today)
        a_done = [day(t["CompleteDate"]) for t in assess_t if t.get("CompleteDate")]
        assess_sched = qt_next or qt_last or a_next or a_last
        if not assess_date and assess_t:
            assess_date = max(a_done) if a_done and len(a_done) == len(assess_t) else \
                (a_last if a_hours > 0 else None)
        assess_tickets = qt_tickets or assess_t

        all_opps = ([main_opp] if main_opp else []) + qts
        all_t = qt_tickets + ts
        touched = [parse_dt(o.get("ModifiedDate")) for o in all_opps]
        touched += [parse_dt(o.get("CreatedDateTime")) for o in all_opps]
        touched += [parse_dt(t.get("LastModifiedDateTime")) for t in all_t]
        touched += [parse_dt(i.get("LastModifiedDateTime")) for i in invs + qt_invs]
        touched = [d if d and d.tzinfo else (d.replace(tzinfo=timezone.utc) if d else None) for d in touched]
        last_touch = max((d for d in touched if d), default=None)
        changed = bool(last_touch and now - last_touch <= timedelta(hours=24))
        # The PO lands in different places: the estimate description, the
        # quick ticket, or only on the invoice once billed.
        po = po_of(main_opp, desc) if main_opp else ""
        if not po:
            inv_text = " ".join(strip_html(io_.get("Description")) for i in invs + qt_invs
                                for io_ in i.get("InvoiceOpportunities") or [])
            po = po_of({}, desc + " " + inv_text)

        bill_invs = qt_invs if billed_on_qt else invs
        rows.append({
            "Stage": stage_label,
            "Address": address_of((main_opp or qts[0])["OpportunityName"]),
            "Quick Ticket Opp #": ", ".join(str(q["OpportunityNumber"]) for q in qts),
            "Assessment Ticket #": ", ".join(str(t["WorkTicketNumber"]) for t in assess_tickets),
            "Assessment Scheduled": assess_sched,
            "Assessment Done": assess_date,
            "Contract Opp #": main_opp["OpportunityNumber"] if main_opp else None,
            "Contract Type": ("Assessment Only (already repaired)" if assess_only
                              else main_opp["_kind"]) if main_opp else "",
            "Contract Status": status or "",
            "Estimate $": est or None,
            "PO #": po,
            "Won Date": day(main_opp.get("WonDate")) if main_opp else None,
            "Time Moved": (("Yes" if time_moved else "No") if qts else ("Yes" if a_hours > 0 else ""))
                          if main_opp and status == "Won" else "",
            "Repair Ticket #": ", ".join(str(t["WorkTicketNumber"]) for t in repair_t),
            "Repair Scheduled": r_next or r_last,
            "Repair Done": complete,
            "T&M Calc $": round(tm_calc, 2) if ts else None,
            "Override $": tm_override,
            "Invoice #": ", ".join(str(i["InvoiceNumber"]) for i in bill_invs),
            "Invoice Date": day(bill_invs[-1]["InvoiceDate"]) if bill_invs else None,
            "Invoiced $": round(sum(i.get("Amount") or 0 for i in bill_invs), 2) if bill_invs else None,
            "Invoice Email Status": ", ".join(sorted({i.get("EmailStatus") or "" for i in bill_invs})),
            "Balance $": round(sum(i.get("AmountRemaining") or 0 for i in bill_invs), 2) if bill_invs else None,
            "Needs Attention": "; ".join(flags),
            "Changed (24h)": "Yes" if changed else "",
            "Last Activity": last_touch.astimezone(CENTRAL).replace(tzinfo=None) if last_touch else None,
            "_qt_id": qts[0]["OpportunityID"] if qts else None,
            "_wo": (re.search(r"\b(\d{2}-\d{5,6})\b", desc) or [None, None])[1],
            "_main_id": main_opp["OpportunityID"] if main_opp else None,
            # Kept for the drafts built after the sheet (see drafts section).
            "_opp_no": (main_opp or qts[0])["OpportunityNumber"],
            "_assess_t": assess_tickets,
            "_repair_t": repair_t,
            "_emergency": any(o["OpportunityID"] in emergency for o in all_opps),
        })

    # An open estimate with a newer job at the same address was superseded.
    # Fold it into the newer job's row so the address appears once, with a
    # reminder to close the old one, or it sits in the pipeline forever.
    newest = {}
    for r in rows:
        k = addr_key(r["Address"])
        if k and r["Contract Opp #"] and (k not in newest or r["Contract Opp #"] > newest[k]["Contract Opp #"]):
            newest[k] = r
    kept = []
    for r in rows:
        n = newest.get(addr_key(r["Address"]))
        if r["Stage"][:2] in ("04", "05") and r["Contract Opp #"] and n is not None and n is not r:
            n["Needs Attention"] = "; ".join(filter(None, [
                n["Needs Attention"], f"Older estimate #{r['Contract Opp #']} still open; mark it lost"]))
            continue
        kept.append(r)
    rows = kept

    # A PO in the inbox for a job Aspire still shows as awaiting one means
    # the City approved it: the repair is cleared and needs scheduling.
    by_wo, by_key, requests, rush, cancels = inbox_pos()
    CTX.update(by_wo=by_wo, by_key=by_key, requests=requests)
    for r in rows:
        if r["Stage"][:2] not in ("03", "04", "05") or not r["Contract Opp #"]:
            continue
        hit = by_wo.get(r["_wo"]) or by_key.get(addr_key(r["Address"]))
        if not hit or hit["po"] == r["PO #"]:
            continue
        r["PO #"] = hit["po"]
        if r["Contract Status"] == "Won" and "move quick ticket time" in (r["Needs Attention"] or ""):
            r["Stage"] = stage("06")
            todo = f"add the PO to #{r['Contract Opp #']} and move the quick ticket time to it"
        elif r["Contract Status"] == "Won":
            r["Stage"] = stage("07")
            todo = f"add the PO to #{r['Contract Opp #']} and put the repair on Jon's schedule"
        else:
            r["Stage"] = stage("06")
            todo = (f"mark #{r['Contract Opp #']} Won, move the assessment time, "
                    f"put the repair on Jon's schedule")
        r["Needs Attention"] = "; ".join(filter(None, [
            f"PO {hit['po']} came in {hit['date']:%m/%d}: {todo}", r["Needs Attention"]]))

    # A quote request in the inbox with no opportunity anywhere on the
    # property (open or closed) has not been entered yet: show it on top.
    known_wo = set()
    known_key = set()
    known_names = [o["OpportunityName"].lower() for o in opps]
    for o in opps:
        known_wo.update(re.findall(r"\b\d{2}-\d{5,6}\b", strip_html(o.get("ProposalDescription1"))))
        known_key.add(addr_key(address_of(o["OpportunityName"])))
    for q in requests.values():
        keys = {addr_key(a) for a in q["address"].split(" / ")}
        # One opportunity can cover two houses, e.g. "100 & 102 Main St".
        named = any(k and k.split()[0] in n and " ".join(k.split()[1:]) in n
                    for k in keys for n in known_names)
        if (q["wo"] and q["wo"] in known_wo) or keys & known_key or named:
            continue
        r = {c: None for c in (rows[0] if rows else COLS)}
        r.update({"Stage": stage("00"), "Address": q["address"], "Contract Opp #": None,
                  "_wo": q["wo"], "_qt_id": None, "_main_id": None,
                  "Needs Attention": f"City asked for a quote {q['date']:%m/%d}"
                                     f"{' (WO ' + q['wo'] + ')' if q['wo'] else ''}: "
                                     "create the FW Assessment & Repair opportunity and schedule the assessment"})
        rows.append(r)

    # City rush requests: flag every open job (before invoicing) they touch.
    for r in rows:
        if r["Stage"][:2] >= "11":
            continue
        when = rush.get(r["_wo"]) or rush.get(addr_key(r["Address"].split(" / ")[0]))
        if when:
            r["Needs Attention"] = "; ".join(filter(None, [f"EXPEDITE (City, {when:%m/%d})", r["Needs Attention"]]))

    # City cancellations: flag the open job so it gets marked Lost. One that
    # matches no open job is shown on its own row, unless it matches a job
    # already closed in Aspire (handled). Aspire descriptions often lack the
    # WO, so it maps to an address through the City's original quote request.
    wo_addr = {q["wo"]: addr_key(q["address"].split(" / ")[0]) for q in requests.values() if q["wo"]}
    for c in cancels:
        c["key"] = c["key"] or wo_addr.get(c["wo"])
        hits = [r for r in rows if r["Stage"][:2] < "11" and r["Address"]
                and ((c["wo"] and r["_wo"] == c["wo"])
                     or (c["key"] and c["key"] == addr_key(r["Address"].split(" / ")[0])))]
        revise = c.get("kind") == "revise"
        if revise:
            # Only matters while the quote is still waiting on the City.
            hits = [r for r in hits if r["Stage"][:2] in ("04", "05")]
        note = (f'CITY ASKED FOR A REVISED QUOTE {c["date"]:%m/%d}: "{c["quote"]}"' if revise
                else f'CITY CANCELED? {c["date"]:%m/%d}: "{c["quote"]}"')
        for r in hits:
            if revise:
                todo = (f"; fix it with a change order on #{r['Contract Opp #']} and re-send"
                        if r["Contract Opp #"] else "")
            else:
                todo = (f"; if so, close out #{r['Contract Opp #']}: bill any trip already made, "
                        "otherwise mark it Lost") if r["Contract Opp #"] else ""
            r["Needs Attention"] = "; ".join(filter(None, [note + todo, r["Needs Attention"]]))
        if hits or revise or (c["wo"] and c["wo"] in known_wo) or (c["key"] and c["key"] in known_key):
            continue
        # An unmatched cancel note older than a week has been seen and dealt
        # with; left in, it sat at the top of every run as a "new request".
        if (today - c["date"]).days > ORPHAN_CANCEL_DAYS:
            continue
        r = {k: None for k in (rows[0] if rows else COLS)}
        r.update({"Stage": stage("00"), "Address": f"WO {c['wo']}" if c["wo"] else "(no job named)",
                  "Contract Opp #": None, "_wo": c["wo"], "_qt_id": None, "_main_id": None,
                  "Needs Attention": note + "; find the job this refers to"})
        rows.append(r)

    # Manual holds, e.g. "wait until we talk to Jon": {"<opp #>": "reason"} in
    # Supabase state, so a job can be parked without touching Aspire.
    holds = db.load_state("fw-irrigation-tracker-holds", {}) if db.is_configured() else {}
    for r in rows:
        why = holds.get(str(r["Contract Opp #"]))
        if why:
            # The hold note is the current plan; drop the cancel prompt it already answers.
            rest = re.sub(r'CITY CANCELED\? .*?otherwise mark it Lost(; )?', "", r["Needs Attention"] or "")
            r["Needs Attention"] = "; ".join(filter(None, [f"ON HOLD: {why}", rest.strip("; ")]))

    # A paid job is finished; its notes are just noise.
    for r in rows:
        if r["Stage"][:2] == "13":
            r["Needs Attention"] = ""

    rows.sort(key=lambda r: (r["Stage"], -(r["Contract Opp #"] or 0), r["Address"]))
    return rows


# --- Excel ---

STAGE_COLORS = {
    "00": "E4DFEC",                                          # new request
    "01": "FFF2CC", "02": "FFF2CC", "03": "FFE699",          # assessment
    "04": "FCE4D6", "05": "F8CBAD",                          # estimate / PO
    "06": "F4B183",                                          # PO in, move time
    "07": "DDEBF7", "08": "BDD7EE", "09": "9BC2E6",          # repair
    "10": "FFD966", "11": "F4B183",                          # invoicing
    "12": "E2EFDA", "13": "A9D08E",                          # payment
}
HEADER_FILL = PatternFill("solid", fgColor="000000")
HEADER_FONT = Font(bold=True, color="D4AF37")
FLAG_FILL = PatternFill("solid", fgColor="FF9999")
CHANGED_FILL = PatternFill("solid", fgColor="FFFF00")
MONEY = '"$"#,##0.00'
COLS = [
    "Stage", "Address", "Needs Attention",
    "Quick Ticket Opp #", "Assessment Ticket #", "Assessment Scheduled", "Assessment Done",
    "Contract Opp #", "Contract Type", "Contract Status", "PO #", "Won Date",
    "Time Moved", "Repair Ticket #", "Repair Scheduled", "Repair Done",
    "Invoice #", "Invoice Date", "Invoiced $", "Invoice Email Status",
    "Changed (24h)", "Last Activity"]
DATE_COLS = ("Assessment Scheduled", "Assessment Done", "Won Date", "Repair Scheduled", "Repair Done",
             "Invoice Date")
WIDTHS = {"Stage": 38, "Address": 28, "Needs Attention": 50, "Last Activity": 17,
          "Contract Type": 30, "Contract Status": 12}
LINKS = {"Quick Ticket Opp #": "_qt_id", "Contract Opp #": "_main_id"}


def write_table(ws, rows):
    ws.append(COLS)
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in rows:
        ws.append([r[c] for c in COLS])
        i = ws.max_row
        stage_fill = PatternFill("solid", fgColor=STAGE_COLORS.get(r["Stage"][:2], "FFFFFF"))
        # Whole row in the stage colour so a job reads across the sheet.
        for j in range(1, len(COLS) + 1):
            ws.cell(i, j).fill = stage_fill
        for col, key in LINKS.items():
            if r[key]:
                link = ws.cell(i, COLS.index(col) + 1)
                link.hyperlink = f"{ASPIRE_PORTAL}/app/opportunities/details/{r[key]}"
                link.font = Font(color="0563C1", underline="single")
        for j, c in enumerate(COLS, 1):
            cell = ws.cell(i, j)
            if c.endswith("$"):
                cell.number_format = MONEY
            elif c == "Last Activity":
                cell.number_format = "m/d/yy h:mm AM/PM"
            elif c in DATE_COLS:
                cell.number_format = "m/d/yy"
        if r["Needs Attention"]:
            note = ws.cell(i, COLS.index("Needs Attention") + 1)
            # Red is reserved for jobs on hold, so it stands out.
            if "ON HOLD" in r["Needs Attention"]:
                note.fill = FLAG_FILL
            note.alignment = Alignment(wrap_text=True, vertical="top")
        if r["Changed (24h)"]:
            ws.cell(i, COLS.index("Changed (24h)") + 1).fill = CHANGED_FILL
    for j, c in enumerate(COLS, 1):
        ws.column_dimensions[get_column_letter(j)].width = WIDTHS.get(c, 12)
    ws.freeze_panes = "D2"      # Stage, Address and Needs Attention stay in view
    ws.auto_filter.ref = ws.dimensions


def summarize(rows):
    out = {}
    for r in rows:
        s = out.setdefault(r["Stage"], {"count": 0, "estimate": 0.0, "invoiced": 0.0, "balance": 0.0})
        s["count"] += 1
        s["estimate"] += r["Estimate $"] or 0
        s["invoiced"] += r["Invoiced $"] or 0
        s["balance"] += r["Balance $"] or 0
    return dict(sorted(out.items()))


def build_xlsx(rows, generated):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([f"FW Irrigation Tracker - {generated:%a %m/%d/%y %I:%M %p} Central"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Stage", "Jobs", "Estimate $", "Invoiced $", "Balance Owed $"])
    for c in ws[3]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
    summary = summarize(rows)
    for stage, s in summary.items():
        ws.append([stage, s["count"], s["estimate"], s["invoiced"] or None, s["balance"] or None])
        ws.cell(ws.max_row, 1).fill = PatternFill("solid", fgColor=STAGE_COLORS.get(stage[:2], "FFFFFF"))
        for j in (3, 4, 5):
            ws.cell(ws.max_row, j).number_format = MONEY
    ws.append(["Total", sum(s["count"] for s in summary.values()),
               sum(s["estimate"] for s in summary.values()),
               sum(s["invoiced"] for s in summary.values()),
               sum(s["balance"] for s in summary.values())])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    for j in (3, 4, 5):
        ws.cell(ws.max_row, j).number_format = MONEY
    ws.append([])
    ws.append([f"Needs attention: {sum(1 for r in rows if r['Needs Attention'])}    "
               f"Changed in last 24h: {sum(1 for r in rows if r['Changed (24h)'])}"])
    ws.column_dimensions["A"].width = 40
    for col in "BCDE":
        ws.column_dimensions[col].width = 15

    write_table(wb.create_sheet("All Jobs"), rows)
    write_table(wb.create_sheet("Needs Attention"), [r for r in rows if r["Needs Attention"]])
    write_table(wb.create_sheet("Changed 24h"), [r for r in rows if r["Changed (24h)"]])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), summary


# --- Email ---

def job_ref(r):
    if r["Contract Opp #"]:
        return f"#{r['Contract Opp #']}"
    return f"QT #{r['Quick Ticket Opp #']}" if r["Quick Ticket Opp #"] else "NEW"


def build_email(rows, summary, generated):
    slot = "Morning" if generated.hour < 12 else "End of Day"
    attention = [r for r in rows if r["Needs Attention"]]
    changed = [r for r in rows if r["Changed (24h)"]]
    subject = (f"FW Irrigation Tracker ({slot}) - {generated:%a %m/%d}: "
               f"{len(attention)} need attention, {len(changed)} changed")

    srows = "".join(
        f"<tr><td style='padding:4px 10px;background:#{STAGE_COLORS.get(k[:2], 'FFFFFF')}'>{escape(k)}</td>"
        f"<td style='padding:4px 10px;text-align:right'>{v['count']}</td>"
        f"<td style='padding:4px 10px;text-align:right'>${v['balance']:,.2f}</td></tr>"
        for k, v in summary.items())
    arows = "".join(
        f"<tr><td style='padding:4px 10px'>{escape(job_ref(r))}</td><td style='padding:4px 10px'>{escape(r['Address'])}</td>"
        f"<td style='padding:4px 10px'>{escape(r['Stage'])}</td><td style='padding:4px 10px'>{escape(r['Needs Attention'])}</td></tr>"
        for r in attention) or "<tr><td style='padding:4px 10px' colspan='4'>Nothing flagged.</td></tr>"
    html = f"""<html><body style="font-family:Arial,sans-serif;font-size:14px">
<p><b>FW Irrigation Tracker</b>, {generated:%A %m/%d/%y %I:%M %p} Central. Full list attached.</p>
<table style="border-collapse:collapse;border:1px solid #ccc">
<tr style="background:#000;color:#D4AF37"><th style="padding:4px 10px;text-align:left">Stage</th><th style="padding:4px 10px">Jobs</th><th style="padding:4px 10px">Balance owed</th></tr>
{srows}</table>
<p><b>Needs attention ({len(attention)})</b></p>
<table style="border-collapse:collapse;border:1px solid #ccc">
<tr style="background:#000;color:#D4AF37"><th style="padding:4px 10px">Opp</th><th style="padding:4px 10px">Address</th><th style="padding:4px 10px">Stage</th><th style="padding:4px 10px">Issue</th></tr>
{arows}</table>
<p style="color:#666;font-size:12px">{len(changed)} jobs changed in the last 24 hours (see the "Changed 24h" tab).</p>
</body></html>"""
    plain = (f"FW Irrigation Tracker {generated:%m/%d/%y %I:%M %p}\n\n"
             + "\n".join(f"{k}: {v['count']}" for k, v in summary.items())
             + f"\n\nNeeds attention ({len(attention)}):\n"
             + "\n".join(f"{job_ref(r)} {r['Address']}: {r['Needs Attention']}" for r in attention))
    return subject, html, plain


def send_email(subject, html_body, plain_body, xlsx_bytes, xlsx_filename):
    sender = os.environ.get("GMAIL_EMAIL")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        raise RuntimeError("GMAIL_EMAIL / GMAIL_APP_PASSWORD not set")

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill FW Tracker", sender))
    msg["To"] = ", ".join(RECIPIENTS)
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(plain_body, "plain"))
    body.attach(MIMEText(html_body, "html"))
    msg.attach(body)
    part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    part.set_payload(xlsx_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{xlsx_filename}"')
    msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(sender, password)
        s.sendmail(sender, RECIPIENTS, msg.as_string())
    log(f"Email sent to {len(RECIPIENTS)} recipients with {xlsx_filename} attached")


# --- Drafts ---
#
# Two Outlook drafts, created in Evelin's mailbox through Graph and never sent.
# She reviews and sends each one herself.
#
#   3pm run: "jobs ready to schedule" to the ops manager, listing every FW
#            ticket that is not on Jon's schedule yet. Also on the 6:45am run
#            when an EMERGENCY job is waiting.
#   6:45am run: "Completed:" to the City for the repairs Jon worked since the
#            last run, one table row per repair, each backed by a completed
#            CompanyCam checklist. The checklist PDFs are exported later by a
#            local session from the queue in Supabase (CompanyCam's API has no
#            PDF export), and Evelin attaches them.

DRAFT_MAILBOX = os.environ.get("FW_DRAFT_MAILBOX", "evelin@blackhilltx.com")
SCHEDULE_TO = ["opsmanager@blackhilltx.com"]
SCHEDULE_CC = ["Denisse@blackhilltx.com"]
SCHEDULE_SUBJECT = "FW Irrigation: jobs ready to schedule for Jon"
SCHEDULE_STATE = "fw-schedule-draft"
COMPLETION_CC = ["BranchAdmin@blackhilltx.com", "irrigation@blackhilltx.com", "Denisse@blackhilltx.com"]
COMPLETION_STATE = "fw-completion-drafts"
PDF_QUEUE_STATE = "fw-completion-pdf-queue"
PENDING_DAYS = 21             # a flagged repair is retried this long, then dropped
COMPANYCAM_BASE = "https://api.companycam.com/v2"
COMPANYCAM_FW_PROJECT = os.environ.get("FW_COMPANYCAM_PROJECT_ID", "116203662")
OPEN_TICKET = lambda t: t.get("WorkTicketStatusName") not in ("Complete", "Canceled")


def city_contacts():
    """(to, greeting) for the City completion email. Kept out of this public
    repo: FW_CITY_TO (comma-separated) and FW_CITY_GREETING (the first names,
    e.g. "Name1, Name2, Name3") come from secrets in CI, and from
    ~/.config/fw-irrigation/config.json ({"city_to": [...], "city_greeting": "..."})
    locally."""
    to = [a.strip() for a in os.environ.get("FW_CITY_TO", "").split(",") if a.strip()]
    greeting = os.environ.get("FW_CITY_GREETING", "").strip()
    path = os.path.expanduser("~/.config/fw-irrigation/config.json")
    if not (to and greeting) and os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        to = to or cfg.get("city_to", [])
        greeting = greeting or cfg.get("city_greeting", "")
    if not (to and greeting):
        raise RuntimeError("FW_CITY_TO / FW_CITY_GREETING not set")
    return to, greeting



def _li(items):
    if not items:
        return "<p>None</p>"
    return "<ol>" + "".join(f"<li>{escape(x)}</li>" for x in items) + "</ol>"


def schedule_lists(api, rows):
    """(assessments, repairs, has_emergency) not on Jon's schedule.

    Candidates come from the stage (01, or 07 with a PO), then every ticket is
    re-checked against Aspire: still open, and no WorkTicketVisit with a
    ScheduledDate. A repair without a PO is never listed."""
    cands = []
    for r in rows:
        code = r["Stage"][:2]
        if code == "01":
            cands += [("a", r, t) for t in r.get("_assess_t") or [] if OPEN_TICKET(t)]
        elif code == "07" and r.get("PO #"):
            cands += [("r", r, t) for t in r.get("_repair_t") or [] if OPEN_TICKET(t)]
    ids = [t["WorkTicketID"] for _, _, t in cands]
    fresh = {t["WorkTicketID"]: t for t in api.get_in("WorkTickets", "WorkTicketID", ids)}
    on_sched = {v["WorkTicketID"] for v in api.get_in("WorkTicketVisits", "WorkTicketID", ids)
                if v.get("ScheduledDate")}
    out = {"a": [], "r": []}
    for kind, r, t in cands:
        tid = t["WorkTicketID"]
        if tid in on_sched or not OPEN_TICKET(fresh.get(tid, t)):
            continue
        line = f"{r['Address']}, Opp #{r['_opp_no']}, WT {t['WorkTicketNumber']}"
        out[kind].append((not r.get("_emergency"), line + (" (EMERGENCY)" if r.get("_emergency") else "")))
    a = [x for _, x in sorted(out["a"])]        # emergencies first
    rp = [x for _, x in sorted(out["r"])]
    return a, rp, any(x.endswith("(EMERGENCY)") for x in a + rp)


def schedule_draft(assess, repairs):
    html = ("<html><body style=\"font-family:Arial,sans-serif;font-size:14px\">"
            "<p>Hi Aaron,</p>"
            "<p><b>Assessments to schedule</b></p>" + _li(assess) +
            "<p><b>Repairs to schedule (PO received)</b></p>" + _li(repairs) +
            "<p>Thanks,<br>Evelin</p></body></html>")
    return SCHEDULE_SUBJECT, html


def _addr_parts(addr):
    m = re.match(r"\s*(\d+)\s+(.*)", addr or "")
    if not m:
        return None, ""
    return m.group(1), re.sub(r"[^a-z]", "", m.group(2).lower())


def _same_street(a, b):
    """First street word on one side, the street run together on the other:
    "oakhollow" matches "Oak Hollow Dr", "elm way" matches "Elm Dr"."""
    _, sa = _addr_parts(a)
    _, sb = _addr_parts(b)
    wa = re.match(r"\s*\d+\s+([A-Za-z]+)", a or "")
    wb = re.match(r"\s*\d+\s+([A-Za-z]+)", b or "")
    if not (wa and wb and sa and sb):
        return False
    wa, wb = wa.group(1).lower(), wb.group(1).lower()
    return wa == wb or sb.startswith(wa) or sa.startswith(wb)


class CompanyCam:
    def __init__(self):
        token = os.environ.get("COMPANYCAM_TOKEN", "").strip()
        if not token:
            with open(os.path.expanduser("~/.config/companycam/config.json")) as f:
                token = json.load(f)["access_token"]
        self.token = token

    def get(self, path):
        req = urllib.request.Request(COMPANYCAM_BASE + path,
                                     headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"CompanyCam {path} {e.code}: {body[:300]}") from e

    def checklists(self):
        out, page = [], 1
        while page <= 20:
            batch = self.get(f"/projects/{COMPANYCAM_FW_PROJECT}/checklists?per_page=100&page={page}")
            out += batch or []
            if not batch or len(batch) < 100:
                break
            page += 1
        return out

    def property_answer(self, checklist_id):
        d = self.get(f"/projects/{COMPANYCAM_FW_PROJECT}/checklists/{checklist_id}")
        tasks = list(d.get("sectionless_tasks") or [])
        for sec in d.get("sections") or []:
            tasks += sec.get("tasks") or []
        for t in tasks:
            if "property address" in (t.get("title") or "").lower():
                return " ".join(filter(None, (s.get("answer_text") for s in t.get("sub_tasks") or [])))
        return ""


def _central_date(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(CENTRAL).date() if epoch else None


def worked_repairs(rows, state, now):
    """Repair tickets Jon worked since the watermark (plus earlier ones still
    pending a checklist), minus those already drafted. Worked = a time entry
    with hours, or a visit dated in the window (before today: a visit dated
    today has not happened yet at 6:45am). Returns [(row, ticket, date)]."""
    today = now.astimezone(CENTRAL).date()
    wm = parse_dt(state["watermark"])
    wm_date = wm.astimezone(CENTRAL).date()
    done = set(state.get("processed", []))
    pending = {int(k) for k in state.get("pending", {})}
    times, visits = CTX.get("times", []), CTX.get("visits", [])
    out = []
    for r in rows:
        for t in r.get("_repair_t") or []:
            tid = t["WorkTicketID"]
            if tid in done or t.get("WorkTicketStatusName") == "Canceled":
                continue
            hit = []
            for tm in times:
                if tm["WorkTicketID"] != tid or not (tm.get("Hours") or 0) > 0:
                    continue
                d = day(tm.get("WorkTicketTimeDate") or tm.get("StartTime"))
                made = parse_dt(tm.get("CreatedDateTime"))
                if made and not made.tzinfo:
                    made = made.replace(tzinfo=timezone.utc)
                if (d and d >= wm_date) or (made and made >= wm) or tid in pending:
                    hit.append(d)
            for v in visits:
                if v["WorkTicketID"] != tid or not v.get("ScheduledDate"):
                    continue
                d = day(v["ScheduledDate"])
                if (wm_date <= d < today) or (tid in pending and d < today):
                    hit.append(d)
            hit = [d for d in hit if d]
            if hit:
                out.append((r, t, max(hit)))
    return out


def all_worked_repair_ids(rows):
    times, visits = CTX.get("times", []), CTX.get("visits", [])
    worked = {tm["WorkTicketID"] for tm in times if (tm.get("Hours") or 0) > 0}
    worked |= {v["WorkTicketID"] for v in visits if v.get("ScheduledDate")}
    return sorted(t["WorkTicketID"] for r in rows for t in r.get("_repair_t") or []
                  if t["WorkTicketID"] in worked)


def match_checklist(cc, lists, address, work_date):
    """(checklist, problem). Prefer a completed checklist created on or after
    the repair date for this street; older ones at the address are the
    assessment or an earlier job. The house number must agree with Aspire in
    both the checklist name and Jon's "Property Address" answer."""
    num, _ = _addr_parts(address)
    street = [c for c in lists if _same_street(c.get("name") or "", address)
              and _central_date(c.get("created_at")) and _central_date(c["created_at"]) >= work_date]
    if not street:
        return None, "no CompanyCam checklist"
    street.sort(key=lambda c: c["created_at"])
    exact = [c for c in street if _addr_parts(c.get("name"))[0] == num]
    c = (exact or street)[0]
    if not c.get("completed_at"):
        return None, f"CompanyCam checklist \"{c.get('name')}\" is not completed"
    if _addr_parts(c.get("name"))[0] != num:
        return None, f"checklist address says {_addr_parts(c.get('name'))[0]}, Aspire says {num}"
    answer = cc.property_answer(c["id"])
    a_num = (re.match(r"\s*(\d+)", answer or "") or [None, None])[1]
    if a_num and a_num != num:
        return None, f"checklist address says {a_num}, Aspire says {num}"
    return c, None


def wo_po_for(r):
    """The City's WO and PO for a row: inbox first, then the opportunity."""
    by_wo, by_key = CTX.get("by_wo", {}), CTX.get("by_key", {})
    key = addr_key(r["Address"])
    wo = r.get("_wo")
    if not wo:
        hit = by_key.get(key)
        wo = hit["wo"] if hit and hit.get("wo") else None
    if not wo:
        wo = next((q["wo"] for q in CTX.get("requests", {}).values()
                   if q.get("wo") and key and key in {addr_key(a) for a in q["address"].split(" / ")}), None)
    hit = (by_wo.get(wo) if wo else None) or by_key.get(key)
    aspire_po = re.sub(r"^\s*P\.?\s*O\.?\s*#?\s*:?\s*", "", r.get("PO #") or "", flags=re.I).strip()
    po = hit["po"] if hit else aspire_po
    flags = []
    if hit and aspire_po and hit["po"] != aspire_po:
        flags.append(f"City email has PO {hit['po']}, Aspire has {aspire_po}; check the completion draft")
    return wo or "", po, flags


def completion_draft(jobs, greeting):
    """jobs: [{address, wo, po, date}]"""
    if len(jobs) == 1:
        j = jobs[0]
        subject = f"Completed: {j['address']}, Work Order {j['wo']}, PO {j['po']}"
    else:
        subject = f"Completed: {len(jobs)} Irrigation Work Orders, Completion Pictures Attached"
    cell = "border:1px solid #000;padding:4px 10px"
    head = "".join(f"<th style=\"{cell};text-align:left\">{h}</th>"
                   for h in ("Address", "Work Order", "PO", "Date completed"))
    body = "".join("<tr>" + "".join(f"<td style=\"{cell}\">{escape(str(x))}</td>"
                                    for x in (j["address"], j["wo"], j["po"], f"{j['date']:%m/%d/%Y}"))
                   + "</tr>" for j in jobs)
    html = ("<html><body style=\"font-family:Arial,sans-serif;font-size:14px\">"
            f"<p>Hello {escape(greeting)},</p>"
            "<p>The irrigation repairs below have been completed. The completion pictures for each "
            "address are attached for your records.</p>"
            f"<table style=\"border-collapse:collapse;border:1px solid #000\"><tr>{head}</tr>{body}</table>"
            "<p>Thank you,<br>Evelin</p></body></html>")
    return subject, html


def plan_completions(rows, state, now):
    """(jobs, flags_by_row). Each job carries its checklist for the PDF queue."""
    cands = worked_repairs(rows, state, now)
    if not cands:
        return [], []
    cc = CompanyCam()
    lists = cc.checklists()
    jobs, flags = [], []
    for r, t, d in cands:
        wt = t["WorkTicketNumber"]
        try:
            c, problem = match_checklist(cc, lists, r["Address"], d)
        except Exception as e:          # flag and continue per record
            c, problem = None, f"CompanyCam lookup failed ({e})"
        if problem:
            flags.append((r, t, f"Completion email: repair WT {wt} left out, {problem}"))
            continue
        wo, po, notes = wo_po_for(r)
        if not wo:
            notes.append(f"Completion draft: no City work order found for WT {wt}; fill it in")
        for n in notes:
            flags.append((r, None, n))
        jobs.append({"address": r["Address"], "wo": wo, "po": po, "date": d,
                     "wt_id": t["WorkTicketID"], "wt": wt, "checklist_id": str(c["id"]),
                     "checklist_name": c.get("name")})
    jobs.sort(key=lambda j: (j["date"], j["address"]))
    return jobs, flags


# --- Graph (drafts only; never sends) ---

def graph_call(token, method, path, body=None):
    """Returns (status, json). 404 comes back as (404, None) so a draft that
    was sent or deleted can be told apart from a real failure."""
    req = urllib.request.Request("https://graph.microsoft.com/v1.0" + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, None
        detail = e.read().decode(errors="replace")[:300] if e.fp else ""
        raise RuntimeError(f"Graph {method} {path.split('?')[0]} {e.code}: {detail}") from e


def _recips(addrs):
    return [{"emailAddress": {"address": a}} for a in addrs]


def graph_token():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fw_monitor", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fw-irrigation-monitor.py"))
    mon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mon)
    return mon.get_token()


def create_draft(token, subject, html, to, cc):
    status, msg = graph_call(token, "POST", f"/users/{DRAFT_MAILBOX}/messages", {
        "subject": subject, "body": {"contentType": "HTML", "content": html},
        "toRecipients": _recips(to), "ccRecipients": _recips(cc)})
    if not msg or not msg.get("id"):
        raise RuntimeError(f"Draft create returned {status} with no id")
    # Read it back: a 201 is a claim, the message is the result.
    _, back = graph_call(token, "GET", f"/users/{DRAFT_MAILBOX}/messages/{msg['id']}?$select=id,isDraft")
    if not back or not back.get("isDraft"):
        raise RuntimeError("Draft create did not read back as a draft")
    return msg["id"]


def upsert_schedule_draft(token, subject, html):
    """One live draft at a time: patch the previous one while it is still a
    draft, skip it if nothing changed, start a new one once it was sent or
    deleted."""
    import hashlib
    digest = hashlib.sha256((subject + html).encode()).hexdigest()
    st = db.load_state(SCHEDULE_STATE, {})
    old = st.get("message_id")
    if old:
        status, msg = graph_call(token, "GET", f"/users/{DRAFT_MAILBOX}/messages/{old}?$select=id,isDraft")
        if status != 404 and msg and msg.get("isDraft"):
            if st.get("hash") == digest:
                log("Schedule draft unchanged; left as is")
                return old
            graph_call(token, "PATCH", f"/users/{DRAFT_MAILBOX}/messages/{old}", {
                "subject": subject, "body": {"contentType": "HTML", "content": html},
                "toRecipients": _recips(SCHEDULE_TO), "ccRecipients": _recips(SCHEDULE_CC)})
            db.save_state(SCHEDULE_STATE, {"message_id": old, "hash": digest,
                                           "updated": datetime.now(timezone.utc).isoformat()})
            log("Schedule draft updated in place")
            return old
    new = create_draft(token, subject, html, SCHEDULE_TO, SCHEDULE_CC)
    db.save_state(SCHEDULE_STATE, {"message_id": new, "hash": digest,
                                   "updated": datetime.now(timezone.utc).isoformat()})
    log("Schedule draft created")
    return new


def seed_completion_state(rows, now):
    """Mark every repair worked so far as handled and start the window now,
    so the first live run does not re-draft jobs already emailed by hand."""
    state = {"watermark": now.isoformat(), "processed": all_worked_repair_ids(rows), "pending": {}}
    db.save_state(COMPLETION_STATE, state)
    log(f"Completion state seeded: {len(state['processed'])} repair tickets marked handled")
    return state


def run_drafts(api, rows, which, test=False):
    """which: subset of {"schedule", "completion", "emergency"}. Returns a list
    of errors; per-draft failures do not stop the tracker email."""
    now = datetime.now(timezone.utc)
    errors, token = [], None

    def tok():
        nonlocal token
        token = token or graph_token()
        return token

    if which & {"schedule", "emergency"}:
        try:
            assess, repairs, urgent = schedule_lists(api, rows)
            if not (assess or repairs):
                log("Schedule draft: nothing to schedule")
            elif "schedule" not in which and not urgent:
                log("Schedule draft: no EMERGENCY job waiting, left for the 3pm run")
            else:
                subject, html = schedule_draft(assess, repairs)
                if test:
                    print(f"\n=== DRAFT (not created) ===\nTo: {', '.join(SCHEDULE_TO)}\n"
                          f"Cc: {', '.join(SCHEDULE_CC)}\nSubject: {subject}\n\n{strip_tags(html)}")
                else:
                    upsert_schedule_draft(tok(), subject, html)
        except Exception as e:
            log(f"Schedule draft failed: {e}")
            errors.append(f"schedule draft: {e}")

    if "completion" in which:
        try:
            state = db.load_state(COMPLETION_STATE, {}) if db.is_configured() else {}
            # --draft-test-days=N previews an N-day window, ignoring what was drafted.
            test_days = next((int(a.split("=", 1)[1]) for a in sys.argv
                              if a.startswith("--draft-test-days=")), None)
            if test and test_days:
                state = {}
            if not state.get("watermark"):
                if test:
                    days = test_days or 7
                    state = {"watermark": (now - timedelta(days=days)).isoformat(), "processed": [], "pending": {}}
                    print(f"\n(completion preview over the last {days} days, ignoring drafted state)")
                else:
                    seed_completion_state(rows, now)
                    return errors
            jobs, flags = plan_completions(rows, state, now)
            for r, t, note in flags:
                r["Needs Attention"] = "; ".join(filter(None, [r["Needs Attention"], note]))
            if test:
                for _, _, note in flags:
                    print(f"NEEDS ATTENTION: {note}")
            if jobs:
                city_to, greeting = city_contacts()
                subject, html = completion_draft(jobs, greeting)
                if test:
                    print(f"\n=== DRAFT (not created) ===\nTo: {', '.join(city_to)}\n"
                          f"Cc: {', '.join(COMPLETION_CC)}\nSubject: {subject}\n\n{strip_tags(html)}")
                    for j in jobs:
                        print(f"  checklist {j['checklist_id']} \"{j['checklist_name']}\" "
                              f"-> \"{j['address']} - Work Order.pdf\" (WT {j['wt']})")
                    return errors
                draft_id = create_draft(tok(), subject, html, city_to, COMPLETION_CC)
                queue = db.load_state(PDF_QUEUE_STATE, {"jobs": []})
                queue.setdefault("jobs", [])
                queue["jobs"] += [{"draft_message_id": draft_id, "checklist_id": j["checklist_id"],
                                   "file_name": f"{j['address']} - Work Order.pdf",
                                   "address": j["address"], "work_ticket": j["wt"],
                                   "queued_at": now.isoformat()} for j in jobs]
                db.save_state(PDF_QUEUE_STATE, queue)
                log(f"Completion draft created for {len(jobs)} repairs; PDF queue updated")
            elif test:
                print("\nCompletion draft: no repairs to draft")
            if not test:
                flagged = {str(t["WorkTicketID"]) for _, t, _ in flags if t}
                today = now.astimezone(CENTRAL).date()
                first = state.get("pending", {})
                expired = {k for k, v in first.items()
                           if (today - datetime.fromisoformat(v).date()).days > PENDING_DAYS}
                for k in sorted(expired & flagged):
                    log(f"Completion: WT id {k} still has no usable checklist after {PENDING_DAYS} days; dropped")
                pending = {k: first.get(k, today.isoformat()) for k in flagged - expired}
                state = {"watermark": now.isoformat(),
                         "processed": sorted(set(state.get("processed", [])) | {j["wt_id"] for j in jobs}),
                         "pending": pending}
                db.save_state(COMPLETION_STATE, state)
        except Exception as e:
            log(f"Completion draft failed: {e}")
            errors.append(f"completion draft: {e}")
    return errors


def strip_tags(html):
    """Readable text of a draft for --draft-test output."""
    t = re.sub(r"</(p|tr|li|ol|table)>|<br>", "\n", html)
    t = re.sub(r"<li>", "  - ", t)
    t = re.sub(r"</t[dh]>", " | ", t)
    return re.sub(r"<[^>]+>", "", t).strip()


# --- Main ---

def main(run):
    log("FW Irrigation Tracker starting")
    generated = datetime.now(CENTRAL)
    if DRAFT_TEST or SEED_COMPLETIONS:
        api = Aspire(load_config())
        rows = build_rows(api)
        if SEED_COMPLETIONS:
            seed_completion_state(rows, datetime.now(timezone.utc))
        else:
            run_drafts(api, rows, {"schedule", "completion"}, test=True)
        return
    if not FORCE and not DRY_RUN and generated.weekday() >= 5:
        log("Skipping - weekend")
        return
    if not FORCE and not DRY_RUN and generated.hour not in SEND_HOURS:
        log(f"Skipping - Central hour is {generated.hour:02d}, only send at {SEND_HOURS}")
        return
    slot_key = f"{generated:%Y-%m-%d}-{generated.hour:02d}"
    if not FORCE and not DRY_RUN and db.is_processed("fw-irrigation-tracker", slot_key):
        log(f"Skipping - the {slot_key} slot was already sent")
        return

    api = Aspire(load_config())
    rows = build_rows(api)
    run.records = len(rows)
    if DRAFTS_ARG is not None:
        which = {w.strip() for w in DRAFTS_ARG.split(",") if w.strip() and w.strip() != "none"}
    elif FORCE or DRY_RUN:
        which = set()
    else:
        which = {15: {"schedule"}, 6: {"completion", "emergency"}}.get(generated.hour, set())
    draft_errors = run_drafts(api, rows, which) if which and not DRY_RUN else []
    xlsx_bytes, summary = build_xlsx(rows, generated)
    subject, html, plain = build_email(rows, summary, generated)
    xlsx_filename = f"FW_Irrigation_Tracker_{generated:%Y-%m-%d_%H%M}.xlsx"

    if DRY_RUN:
        out = os.environ.get("TRACKER_OUT_DIR", "/tmp")
        path = os.path.join(out, xlsx_filename)
        with open(path, "wb") as f:
            f.write(xlsx_bytes)
        print(f"\nSubject: {subject}\n\n{plain}")
        log(f"Dry run - wrote {path}")
        return

    send_email(subject, html, plain, xlsx_bytes, xlsx_filename)
    if not FORCE:
        db.mark_processed("fw-irrigation-tracker", slot_key, note=subject)
    # The tracker still went out; fail the run so notify-failure reports it.
    if draft_errors:
        raise RuntimeError("Draft step failed: " + "; ".join(draft_errors))


if __name__ == "__main__":
    with db.track("fw-irrigation-tracker") as run:
        main(run)
