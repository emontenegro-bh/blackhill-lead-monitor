#!/usr/bin/env python3
"""City bid monitor — daily scan of DFW municipal procurement sources for
landscaping / grounds-maintenance bid opportunities.

Polls 25+ sources (Bonfire JSON, Ionwave/CivicPlus/Public Purchase HTML,
PlanetBids/Beacon/DemandStar JSON, BidNet Direct keyword search), diffs
against data/bid-monitor-state.json, and emails a digest of NEW postings
that match service keywords. No email on days with nothing new.

Source research verified 2026-06-11. Per-source notes inline.

Usage:
  python scripts/bid-monitor.py            # normal run (email + state save)
  python scripts/bid-monitor.py --dry-run  # print digest, no email, no state
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import urljoin, quote, urlparse

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
STATE_FILE = os.path.join(REPO_ROOT, "data", "bid-monitor-state.json")

RECIPIENTS = [e.strip() for e in os.environ.get("BID_RECIPIENTS", "evelin@blackhilltx.com").split(",") if e.strip()]
FIRECRAWL_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
DRY_RUN = "--dry-run" in sys.argv

MAX_SEEN_IDS = 6000          # FIFO cap on remembered postings
CONSECUTIVE_FAIL_ALERT = 3   # exit nonzero if a source fails this many runs in a row

# CivicPlus 404s bot UAs; Euless (Akamai) needs the complete Chrome header set,
# not just a UA — silent zero results on WAF'd sites mean blocked, not empty.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Substring match (lowercased) against title/description decides relevance.
SERVICE_KEYWORDS = [
    "landscap", "lawn", "mow", "grounds maintenance", "ground maintenance",
    "groundskeeping", "grounds keeping", "irrigation", "sprinkler", "turf",
    "sod ", "sodding", "tree trim", "tree removal", "tree maintenance", "tree planting",
    "vegetation", "median", "right-of-way", "right of way", "rights-of-way",
    "hydromulch", "hydroseed", "erosion", "weed", "brush removal", "brush control",
    "park maintenance", "parks maintenance", "beautification", "xeriscap",
    "planting", "shrub", "fertiliz", "herbicide", "mulch", "bed maintenance",
]

# Veto list: a posting that hits a service keyword only incidentally (e.g.
# "Right-of-Way Acquisition" inside an A/E services contract) is not relevant.
# These mark design/professional-services and other-trade work, not field work.
EXCLUDE_KEYWORDS = [
    "architectural", "engineering", "surveying", "appraisal", "acquisition",
    "janitorial", "electrical", "plumbing", "roofing", "asbestos", "paving",
    "auditing", "software", "insurance", "legal services", "demolition",
    # Construction-delivery / prime-contractor procurements that merely contain
    # "landscape" — Black Hill would be a sub, not the bidder (Evelin, Jun 21).
    "construction manager at risk", "cmar", "design-build", "design build",
    "state let construction", "state-let construction",
]

# BidNet returns statewide results; keep only DFW-area agencies.
REGION_TERMS = [
    "fort worth", "arlington", "dallas", "tarrant", "parker county", "weatherford",
    "watauga", "north richland hills", "richland hills", "haltom", "keller",
    "saginaw", "hurst", "euless", "bedford", "mansfield", "burleson", "benbrook",
    "white settlement", "grand prairie", "azle", "aledo", "willow park", "crowley",
    "forest hill", "kennedale", "colleyville", "southlake", "grapevine", "roanoke",
    "trophy club", "westlake", "lake worth", "river oaks", "sansom park", "blue mound",
    "everman", "pantego", "dalworthington", "johnson county", "joshua", "cleburne",
    "springtown", "hudson oaks", "annetta", "irving", "duncanville", "cedar hill",
    "desoto", "grand prairie", "nctcog", "north central texas",
]


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[{now_utc().strftime('%H:%M:%S')}] {msg}", flush=True)


# Bonfire (bonfirehub.com) and Ionwave (ionwave.net) share backend infra per
# platform, so many rapid requests across their subdomains trip a shared 429
# rate limit on the runner IP. Pace requests to the same registrable domain and
# retry on 429 with backoff (honoring Retry-After when present).
_MIN_HOST_INTERVAL = 2.5   # seconds between requests to the same host family
_last_host_hit = {}


def _host_key(url):
    host = (urlparse(url).hostname or "").split(".")
    return ".".join(host[-2:]) if len(host) >= 2 else ".".join(host)


def fetch(url, **kwargs):
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", 30)
    key = _host_key(url)
    attempts = 5
    for attempt in range(attempts):
        wait = _MIN_HOST_INTERVAL - (time.monotonic() - _last_host_hit.get(key, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_host_hit[key] = time.monotonic()
        r = requests.get(url, **kwargs)
        if r.status_code == 429 and attempt < attempts - 1:
            ra = r.headers.get("Retry-After", "")
            time.sleep(float(ra) if ra.isdigit() else 8.0 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def make_item(source, agency, bid_id, title, close="", url="", ref=""):
    return {
        "id": f"{source}:{bid_id}",
        "agency": agency,
        "title": " ".join((title or "").split())[:300],
        "close": close,
        "url": url,
        "ref": ref,
    }


# ---------------------------------------------------------------- Bonfire ---
# Public list JSON, no auth. payload.projects is a dict keyed by ID, not a list.
BONFIRE_ORGS = [
    ("City of Fort Worth", "fortworthtexas"),
    ("City of Arlington", "arlingtontx"),
    ("City of Dallas", "dallascityhall"),
    ("Parker County", "parkercountytx"),
    ("City of Burleson", "burlesontx"),
    ("City of Lewisville", "cityoflewisville"),
    ("Denton County", "dentoncounty"),
    ("City of Southlake", "southlake"),
    ("DFW Airport", "dfwairport"),
    ("City of Frisco", "friscotexas"),  # friscotexas.gov/883 embeds this Bonfire portal
]


def scrape_bonfire(agency, slug):
    url = f"https://{slug}.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData"
    data = fetch(url).json()
    projects = (data.get("payload") or {}).get("projects") or {}
    items = []
    for pid, p in projects.items():
        items.append(make_item(
            f"bonfire-{slug}", agency, pid,
            p.get("ProjectName", ""),
            close=(p.get("DateClose") or "")[:10],
            url=f"https://{slug}.bonfirehub.com/opportunities/{pid}",
            ref=p.get("ReferenceID", ""),
        ))
    return items


# -------------------------------------------------------------- CivicPlus ---
# Shared Bids.aspx markup across all CivicPlus cities; one parser covers all.
CIVICPLUS_SITES = [
    ("City of Weatherford", "https://weatherfordtx.gov/Bids.aspx"),
    ("City of Watauga", "https://www.cowtx.org/Bids.aspx"),
    ("City of Haltom City", "https://www.haltomcitytx.com/Bids.aspx"),
    ("City of Mansfield", "https://www.mansfieldtexas.gov/bids.aspx"),
    ("City of Benbrook", "https://www.benbrook-tx.gov/Bids.aspx"),
    ("City of White Settlement", "https://www.wstx.us/Bids.aspx"),
    ("City of Azle", "https://www.cityofazle.org/Bids.aspx"),
    ("City of Forest Hill", "https://www.foresthilltx.org/Bids.aspx"),
    ("City of Haslet", "https://www.haslet.org/Bids.aspx"),
    ("City of Midlothian", "https://www.midlothian.tx.us/Bids.aspx"),
    ("City of Kennedale", "https://www.cityofkennedale.com/Bids.aspx"),
    ("City of Richland Hills", "https://www.richlandhills.com/Bids.aspx"),
    ("City of Westworth Village", "https://www.cityofwestworth.com/Bids.aspx"),
    ("City of Cleburne", "https://www.cleburne.net/Bids.aspx"),  # /207/Bid-Opportunities maps to this CivicPlus module
]


def scrape_civicplus(agency, url):
    r = fetch(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    seen_ids = set()
    for a in soup.find_all("a", href=re.compile(r"bids\.aspx\?bidID=\d+", re.I)):
        m = re.search(r"bidID=(\d+)", a["href"], re.I)
        title = a.get_text(" ", strip=True)
        if not m or not title:
            continue
        if m.group(1) in seen_ids:
            continue
        seen_ids.add(m.group(1))
        items.append(make_item(
            f"civicplus-{agency.lower().replace(' ', '')}", agency, m.group(1),
            title, url=urljoin(url, a["href"]),
        ))
    if not items and "no open bid postings" not in r.text.lower():
        raise RuntimeError("no bid links and no empty-state marker — possible WAF block or markup change")
    return items


# ---------------------------------------------------------------- Ionwave ---
# Telerik RadGrid is server-rendered; parse the table by header names.
IONWAVE_SITES = [
    ("Tarrant County", "https://tarrantcountytx.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("City of Keller", "https://cityofkeller.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("Fort Worth ISD", "https://fwisd.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("City of Irving", "https://cityofirving.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("City of Grapevine", "https://gpvine.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("Town of Flower Mound", "https://flower-mound.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("City of Carrollton", "https://carrolltonbids.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("City of Plano", "https://planotx.ionwave.net/SourcingEvents.aspx?SourceType=1"),  # /CurrentSourcingEvents.aspx 302s; this is the live path
    ("White Settlement ISD", "https://wsisdebid.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("Grapevine-Colleyville ISD", "https://gcisdbid.ionwave.net/SourcingEvents.aspx?SourceType=1"),
    ("Aledo ISD", "https://aledoisd.ionwave.net/SourcingEvents.aspx?SourceType=1"),
]


def scrape_ionwave(agency, url):
    r = fetch(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if not any("bid title" in h or "title" == h for h in headers):
            continue
        try:
            num_i = next(i for i, h in enumerate(headers) if "number" in h)
            title_i = next(i for i, h in enumerate(headers) if "title" in h)
        except StopIteration:
            continue
        close_i = next((i for i, h in enumerate(headers) if "close" in h), None)
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) <= max(num_i, title_i):
                continue
            num = tds[num_i].get_text(" ", strip=True)
            title = tds[title_i].get_text(" ", strip=True)
            if not num or not title or "no records" in title.lower():
                continue
            close = tds[close_i].get_text(" ", strip=True) if close_i is not None and close_i < len(tds) else ""
            items.append(make_item(f"ionwave-{agency.lower().replace(' ', '')}", agency, num, title, close=close, url=url))
        break
    if not items and "no records to display" not in r.text.lower():
        raise RuntimeError("no rows and no empty-state marker — markup change?")
    return items


# -------------------------------------------------------- Public Purchase ---
# Bid titles are embedded in inline-JS tooltip strings padded with junk <span>
# elements; the junk span IDs are enumerated in action("id1","id2",...) calls.
PUBLICPURCHASE_SITES = [
    ("City of North Richland Hills", "https://www.publicpurchase.com/gems/northrichlandhills,tx/buyer/public/publicInfo"),
    ("City of Hurst", "https://www.publicpurchase.com/gems/hurst,tx/buyer/public/publicInfo"),
]


def _pp_decode(fragment, junk_ids):
    try:
        frag = BeautifulSoup(fragment.replace("\\'", "'").replace('\\"', '"'), "html.parser")
        for sp in frag.find_all("span"):
            if sp.get("id") in junk_ids:
                sp.decompose()
        return " ".join(frag.get_text(" ").split())
    except Exception:
        return ""


def scrape_publicpurchase(agency, url):
    r = fetch(url)
    text = r.text
    bid_ids = list(dict.fromkeys(re.findall(r"bidView\?bidId=(\d+)", text)))
    if not bid_ids:
        if "bidView" in text or "publicInfo" in text:
            return []  # page loaded, no open bids
        raise RuntimeError("page structure changed — bidView pattern absent")
    junk_ids = set()
    for grp in re.findall(r"action\(([^)]*)\)", text):
        junk_ids.update(re.findall(r'"([^"]+)"', grp))
    tooltips = re.findall(r"tooltip\s*=\s*'((?:[^'\\]|\\.)*)'", text)
    titles = [t for t in (_pp_decode(tt, junk_ids) for tt in tooltips) if t]
    items = []
    for i, bid_id in enumerate(bid_ids):
        title = titles[i] if i < len(titles) else f"Bid posting #{bid_id} (open title on site)"
        items.append(make_item(
            "publicpurchase", agency, bid_id, title,
            url=f"https://www.publicpurchase.com/gems/bid/bidView?bidId={bid_id}",
        ))
    return items


# ----------------------------------------------------------------- Saginaw ---
# Self-hosted Revize page; closed bids carry a literal "CLOSED--" prefix.
# Saginaw bids mowing/grounds contracts directly here (annual ~Feb-Mar cycle).
def scrape_saginaw():
    url = "https://www.ci.saginaw.tx.us/government/bid_opportunities.php"
    r = fetch(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if not title or len(title) < 12:
            continue
        if not re.search(r"(?i)bid|proposal|rfp|rfq|quote", title):
            continue
        if re.match(r"(?i)\s*closed", title):
            continue
        items.append(make_item("saginaw", "City of Saginaw", urljoin(url, href), title, url=urljoin(url, href)))
    return items


# ------------------------------------------------------------- Willow Park ---
# Posts RFPs (incl. a mowing/landscape RFP cycle) as document links.
def scrape_willow_park():
    url = "https://willowparktx.gov/344/Public-Notices-RFPs-RFQs"
    r = fetch(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.find_all("a", href=re.compile(r"ImageRepository/Document\?documentID=\d+", re.I)):
        title = a.get_text(" ", strip=True)
        m = re.search(r"documentID=(\d+)", a["href"], re.I)
        if not title or not m:
            continue
        items.append(make_item("willowpark", "City of Willow Park", m.group(1), title, url=urljoin(url, a["href"])))
    return items


# ----------------------------------------------------------------- Bedford ---
# Beacon Bid GraphQL; the agencyTag is literally "bedford-public-library" but
# returns city-wide solicitations (verified). bedfordtx.gov/Bids.aspx is stale.
def scrape_bedford():
    gql = {
        "query": "query ListSolicitations($agencyTag: String, $status: String, $start: Int, $pageSize: Int) { solicitations(agencyTag: $agencyTag, status: $status, start: $start, pageSize: $pageSize) { total data { id refnum title status issueDate dueDate } } }",
        "variables": {"agencyTag": "bedford-public-library", "status": "open", "start": 0, "pageSize": 50},
    }
    r = requests.post(
        "https://www.beaconbid.com/api/gql?operation=ListSolicitations",
        json=gql, headers={**HEADERS, "Content-Type": "application/json"}, timeout=30,
    )
    r.raise_for_status()
    sols = (((r.json().get("data") or {}).get("solicitations") or {}).get("data")) or []
    items = []
    for s in sols:
        due = s.get("dueDate") or {}
        close = (due.get("utcDate") or "")[:10] if isinstance(due, dict) else str(due)[:10]
        items.append(make_item(
            "bedford", "City of Bedford", s.get("id") or s.get("refnum"),
            s.get("title", ""), close=close,
            url="https://www.beaconbid.com/solicitations/bedford-public-library/open",
            ref=s.get("refnum", ""),
        ))
    return items


# ----------------------------------------------------------- Grand Prairie ---
# PlanetBids JSON API (gptx.org itself is Akamai-blocked — never poll it).
# Keyword queries server-side; keep only stage "Bidding".
GP_KEYWORDS = ["landscape", "mowing", "grounds", "irrigation", "tree", "median", "turf"]


def scrape_grand_prairie():
    items, seen = [], set()
    # API rejects calls without a portal Origin/Referer ("DIRECT_ACCESS" 400)
    headers = {**HEADERS, "Accept": "application/vnd.api+json", "company-id": "53284",
               "Origin": "https://vendors.planetbids.com", "Referer": "https://vendors.planetbids.com/"}
    for kw in GP_KEYWORDS:
        url = ("https://api-external.prod.planetbids.com/papi/bids?bid_type_id=0&cid=53284"
               f"&dept_id=0&due_date_from=&due_date_to=&keyword={quote(kw)}&page=1&per_page=30"
               "&sort_by=&sort_order=-1&stage_id=0")
        data = fetch(url, headers=headers).json()
        for d in data.get("data", []):
            attr = d.get("attributes", {})
            if attr.get("stageStr") != "Bidding":
                continue
            bid_id = str(attr.get("bidId") or d.get("id"))
            if bid_id in seen:
                continue
            seen.add(bid_id)
            items.append(make_item(
                "grandprairie", "City of Grand Prairie", bid_id, attr.get("title", ""),
                close=(attr.get("bidDueDate") or "")[:10],
                url=f"https://vendors.planetbids.com/portal/53284/bo/bo-detail/{bid_id}",
                ref=str(attr.get("invitationNum") or ""),
            ))
    return items


# --------------------------------------------------- Weatherford Bid Notices ---
# Purchasing also posts notices as DocumentCenter PDF links on /654.
def scrape_weatherford_notices():
    url = "https://weatherfordtx.gov/654/BidNotices"
    r = fetch(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.find_all("a", href=re.compile(r"/DocumentCenter/View/\d+", re.I)):
        title = a.get_text(" ", strip=True) or a["href"].rstrip("/").split("/")[-1].replace("-", " ")
        m = re.search(r"/DocumentCenter/View/(\d+)", a["href"], re.I)
        if not m:
            continue
        items.append(make_item("weatherford-notices", "City of Weatherford", m.group(1), title, url=urljoin(url, a["href"])))
    return items


# ---------------------------------------------------------- Weatherford ISD ---
def scrape_weatherford_isd():
    url = "https://www.weatherfordisd.com/apps/pages/index.jsp?uREC_ID=280707&type=d&pREC_ID=636801"
    r = fetch(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.find_all("a", href=re.compile(r"files\.edl\.io", re.I)):
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        items.append(make_item("weatherfordisd", "Weatherford ISD", a["href"], title, url=a["href"]))
    return items


# -------------------------------------------------------------------- ESBD ---
# Texas SmartBuy Electronic State Business Daily — the official PUBLIC source
# for state-agency solicitations (TxDOT mowing/landscape lettings appear here
# with a link to the agency's own posting, unlike BidNet which locks the
# agency behind registration). The backend only answers real browsers (plain
# requests and Firecrawl both hang), so this source drives headless Chromium
# and calls the JSON service from page context.
ESBD_KEYWORDS = ["mowing", "landscape", "landscaping", "grounds", "irrigation"]
ESBD_SERVICE = "/app/extensions/CPA/CPAMain/1.0.0/services/ESBD.Service.ss?c=852252&n=2"


def scrape_esbd():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed — ESBD skipped")
    items, seen = [], set()
    with sync_playwright() as p:
        # Must be the FULL chromium build in new-headless mode — the default
        # "headless shell" build gets fingerprinted and the service call hangs.
        browser = p.chromium.launch(headless=True, channel="chromium")
        try:
            # Present as regular Chrome and let the SPA make its own service
            # call, then intercept the response.
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            for kw in ESBD_KEYWORDS:
                # The NetSuite backend throttles ~66s per query before
                # responding; this is normal, not a hang.
                with page.expect_response(lambda r: "ESBD.Service.ss" in r.url, timeout=300000) as resp_info:
                    page.goto(f"https://www.txsmartbuy.gov/esbd?keyword={quote(kw)}",
                              wait_until="domcontentloaded", timeout=60000)
                data = resp_info.value.json()
                for l in (data or {}).get("lines", []):
                    if l.get("statusName") != "Posted":
                        continue
                    sid = str(l.get("internalid"))
                    if sid in seen:
                        continue
                    seen.add(sid)
                    hay = f"{l.get('title', '')} {l.get('agencyName', '')}".lower()
                    if not any(t in hay for t in REGION_TERMS):
                        continue
                    agency = re.sub(r"\s*-\s*\w+\s*$", "", l.get("agencyName", "")).strip()
                    sol_id = l.get("solicitationId", "")
                    # ESBD's own detail page is per-bid (verified: renders full
                    # solicitation + PDF); the record's `url` field is often
                    # just the agency's general lettings page — keep it as a
                    # secondary link only.
                    url = f"https://www.txsmartbuy.gov/esbd/{sol_id}" if sol_id else (l.get("url") or "").strip()
                    item = make_item(
                        "esbd", agency, sid, l.get("title", ""),
                        close=l.get("responseDue", ""), url=url, ref=sol_id,
                    )
                    agency_url = (l.get("url") or "").strip()
                    if agency_url and agency_url != url:
                        item["agency_url"] = agency_url
                    items.append(item)
        finally:
            browser.close()
    return items


# ---------------------------------------------------------- BidNet Direct ---
# Statewide Texas Purchasing Group; server-rendered HTML with ?keywords=
# search. Region-filter rows to DFW-area agencies. Catches member cities not
# polled directly.
BIDNET_KEYWORDS = ["landscaping", "mowing", "grounds maintenance", "irrigation", "vegetation", "tree trimming"]


def scrape_bidnet():
    items, seen, dropped = [], set(), 0
    for kw in BIDNET_KEYWORDS:
        url = f"https://www.bidnetdirect.com/public/solicitations/texas-277?keywords={quote(kw)}"
        r = fetch(url)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"/solicitations/", re.I)):
            href = a.get("href", "")
            m = re.search(r"/(\d{6,})/?$", href.split("?")[0])
            title = a.get_text(" ", strip=True)
            if not m or not title or len(title) < 10:
                continue
            sol_id = m.group(1)
            if sol_id in seen:
                continue
            seen.add(sol_id)
            row = a.find_parent("tr") or a.find_parent("div")
            row_text = row.get_text(" ", strip=True).lower() if row else title.lower()
            if not any(term in row_text for term in REGION_TERMS):
                dropped += 1
                continue
            close = ""
            cm = re.search(r"[Cc]los\w*\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", row_text)
            if cm:
                close = cm.group(1)
            # anchor text is the whole row: "TITLE Texas N day(s) left Published ... Closing ... ID"
            title = re.split(r"\s+\d+\s+day\(s\)\s+left", title)[0]
            title = re.sub(r"\s+Texas$", "", title).strip()
            items.append(make_item("bidnet", "BidNet (regional — agency name requires login)", sol_id, title, close=close,
                                   url=urljoin("https://www.bidnetdirect.com", href)))
    log(f"bidnet: kept {len(items)}, dropped {dropped} outside DFW region")
    return items


# ------------------------------------------------- Firecrawl-proxied sites ---
# Aledo and Crowley sit behind Cloudflare managed challenges; Euless is behind
# Akamai, which fingerprints the requests TLS stack (403 even with full Chrome
# headers). Firecrawl's proxy gets through all three. Skipped if no key.
FIRECRAWL_SITES = [
    ("City of Aledo", "https://www.aledotx.gov/finance-department/pages/bid-opportunities"),
    ("City of Crowley", "https://www.ci.crowley.tx.us/rfps"),
    ("City of Euless", "https://www.eulesstx.gov/departments/purchasing-office/bids-and-quotes"),
]


def scrape_firecrawl(agency, url):
    if not FIRECRAWL_KEY:
        raise RuntimeError("FIRECRAWL_API_KEY not set — source skipped")
    r = requests.post(
        "https://api.firecrawl.dev/v1/scrape",
        json={"url": url, "formats": ["markdown"]},
        headers={"Authorization": f"Bearer {FIRECRAWL_KEY}", "Content-Type": "application/json"},
        timeout=90,
    )
    r.raise_for_status()
    md = ((r.json().get("data") or {}).get("markdown")) or ""
    if not md:
        raise RuntimeError("firecrawl returned empty markdown")
    items = []
    for title, link in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", md):
        title = " ".join(title.split())
        if len(title) < 12:
            continue
        if not re.search(r"(?i)bid|proposal|rfp|rfq|quote|solicitation", title):
            continue
        items.append(make_item(f"firecrawl-{agency.lower().replace(' ', '')}", agency, link, title, url=link))
    return items


# ------------------------------------------------------------------ engine ---

def build_sources():
    sources = []
    for agency, slug in BONFIRE_ORGS:
        sources.append((agency, lambda a=agency, s=slug: scrape_bonfire(a, s)))
    for agency, url in CIVICPLUS_SITES:
        sources.append((agency, lambda a=agency, u=url: scrape_civicplus(a, u)))
    for agency, url in IONWAVE_SITES:
        sources.append((agency, lambda a=agency, u=url: scrape_ionwave(a, u)))
    for agency, url in PUBLICPURCHASE_SITES:
        sources.append((agency, lambda a=agency, u=url: scrape_publicpurchase(a, u)))
    sources.append(("City of Saginaw", scrape_saginaw))
    sources.append(("City of Willow Park", scrape_willow_park))
    sources.append(("City of Bedford", scrape_bedford))
    sources.append(("City of Grand Prairie", scrape_grand_prairie))
    sources.append(("Weatherford Bid Notices", scrape_weatherford_notices))
    sources.append(("Weatherford ISD", scrape_weatherford_isd))
    sources.append(("ESBD (state of Texas)", scrape_esbd))
    sources.append(("BidNet Direct (regional)", scrape_bidnet))
    for agency, url in FIRECRAWL_SITES:
        sources.append((agency, lambda a=agency, u=url: scrape_firecrawl(a, u)))
    return sources


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def is_relevant(item):
    text = item["title"].lower()
    if not any(kw in text for kw in SERVICE_KEYWORDS):
        return False
    return not any(kw in text for kw in EXCLUDE_KEYWORDS)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "source_failures": {}, "last_run": None}


def save_state(state):
    seen = state["seen"]
    if len(seen) > MAX_SEEN_IDS:
        oldest = sorted(seen.items(), key=lambda kv: kv[1].get("first_seen", ""))
        for k, _ in oldest[: len(seen) - MAX_SEEN_IDS]:
            del seen[k]
    state["last_run"] = now_utc().isoformat()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def esc(s):
    return html_mod.escape(s or "")


def build_email(relevant, errors, total_sources, ok_sources):
    today = now_utc().astimezone().strftime("%B %-d, %Y")
    rows = []
    for it in sorted(relevant, key=lambda x: (x["agency"], x["title"])):
        ref = f" &middot; {esc(it['ref'])}" if it["ref"] else ""
        close = f" &middot; closes {esc(it['close'])}" if it["close"] else ""
        agency_link = f' &middot; <a href="{esc(it["agency_url"])}" style="color:#666;">agency site</a>' if it.get("agency_url") else ""
        link = f'<a href="{esc(it["url"])}">{esc(it["title"])}</a>' if it["url"] else esc(it["title"])
        rows.append(
            f'<li style="margin-bottom:10px;"><strong>{esc(it["agency"])}</strong> &mdash; {link}'
            f'<br><span style="color:#666;font-size:13px;">{esc(it["id"].split(":")[0])}{ref}{close}{agency_link}</span></li>'
        )
    error_html = ""
    if errors:
        lis = "".join(f"<li>{esc(name)}: {esc(err)}</li>" for name, err in sorted(errors.items()))
        error_html = (
            '<p style="color:#a33;font-size:13px;margin-top:24px;"><strong>Sources with errors this run '
            f"({len(errors)} of {total_sources}):</strong></p><ul style=\"color:#a33;font-size:13px;\">{lis}</ul>"
        )
    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:680px;">
    <h2 style="color:#1a1a1a;border-bottom:3px solid #c9a227;padding-bottom:8px;">
      New Bid Opportunities &mdash; {today}</h2>
    <p style="color:#444;">{len(relevant)} new landscaping / grounds-maintenance posting(s) found across
      {ok_sources} sources scanned.</p>
    <ul style="padding-left:20px;">{''.join(rows)}</ul>
    {error_html}
    <p style="color:#999;font-size:12px;margin-top:32px;">Black Hill bid monitor &middot; runs daily &middot;
      emails only when new matching opportunities appear</p>
    </body></html>
    """
    plain_lines = [f"New bid opportunities — {today}", ""]
    for it in relevant:
        plain_lines.append(f"- {it['agency']}: {it['title']}" + (f" (closes {it['close']})" if it["close"] else ""))
        if it["url"]:
            plain_lines.append(f"  {it['url']}")
        if it.get("agency_url"):
            plain_lines.append(f"  agency site: {it['agency_url']}")
    subject = f"{len(relevant)} new bid opportunit{'y' if len(relevant) == 1 else 'ies'} — {today}"
    return subject, "\n".join(plain_lines), html_body


def send_email(subject, plain, html_body):
    gmail_email = os.environ.get("GMAIL_EMAIL", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not gmail_email or not gmail_password:
        raise RuntimeError("GMAIL_EMAIL / GMAIL_APP_PASSWORD not set")
    import smtplib

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Black Hill Assistant", gmail_email))
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(gmail_email, gmail_password)
        server.sendmail(gmail_email, RECIPIENTS, msg.as_string())


def main():
    state = load_state()
    seen = state.setdefault("seen", {})
    failures = state.setdefault("source_failures", {})
    today = now_utc().strftime("%Y-%m-%d")

    all_items, errors = [], {}
    sources = build_sources()
    for name, fn in sources:
        try:
            items = fn()
            all_items.extend(items)
            failures.pop(name, None)
            log(f"{name}: {len(items)} open posting(s)")
        except Exception as e:
            errors[name] = str(e)[:200]
            failures[name] = failures.get(name, 0) + 1
            log(f"{name}: ERROR {e}")

    # Prefer direct-source links: drop BidNet rows that duplicate a posting
    # another source already provides publicly (BidNet locks details behind
    # registration). Both stay open in their sources until close, so a
    # same-run title match is sufficient.
    direct_titles = {_norm_title(it["title"]) for it in all_items if not it["id"].startswith("bidnet:")}
    bidnet_dupes = [it for it in all_items if it["id"].startswith("bidnet:") and _norm_title(it["title"]) in direct_titles]
    if bidnet_dupes:
        all_items = [it for it in all_items if it not in bidnet_dupes]
        log(f"dropped {len(bidnet_dupes)} BidNet duplicate(s) of direct-source postings")

    new_items = [it for it in all_items if it["id"] not in seen]
    for it in all_items:
        if it["id"] not in seen:
            seen[it["id"]] = {"title": it["title"][:120], "first_seen": today}

    relevant = [it for it in new_items if is_relevant(it)]
    log(f"total open: {len(all_items)} | new: {len(new_items)} | relevant new: {len(relevant)} | errors: {len(errors)}")

    if relevant:
        subject, plain, html_body = build_email(relevant, errors, len(sources), len(sources) - len(errors))
        if DRY_RUN:
            log("DRY RUN — would send:")
            print(subject)
            print(plain)
        else:
            send_email(subject, plain, html_body)
            log(f"email sent to {RECIPIENTS}: {subject}")
    else:
        log("no new relevant postings — no email sent")

    if not DRY_RUN:
        save_state(state)
        log(f"state saved ({len(seen)} ids tracked)")

    # Hard-fail (triggers the notify-failure email) only on persistent or
    # widespread source failures, not one-off flakiness.
    persistent = [n for n, c in failures.items() if c >= CONSECUTIVE_FAIL_ALERT]
    if persistent:
        log(f"FAILING RUN: sources broken {CONSECUTIVE_FAIL_ALERT}+ consecutive runs: {persistent}")
        sys.exit(1)
    if len(errors) > len(sources) // 3:
        log(f"FAILING RUN: {len(errors)}/{len(sources)} sources errored this run")
        sys.exit(1)


if __name__ == "__main__":
    main()
