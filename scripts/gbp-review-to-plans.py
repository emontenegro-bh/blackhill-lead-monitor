#!/usr/bin/env python3
"""Route new 5-star Google reviews into the irrigation maintenance-plans journey.

Watches Google Business Profile reviews. For each new 5-star review, matches the
reviewer's display name to an Aspire contact (which supplies a verified email),
then tags that contact in Mailchimp to trigger the plans Customer Journey.

Google never exposes a reviewer email address, only a display name, so the match
is probabilistic. Confident matches are tagged automatically. Anything ambiguous
is queued for Evelin instead of guessed at. Measured 2026-09-01 against the eight
real Jon reviews: 6 unique-with-email, 0 ambiguous, 2 correctly refused (one was a
spouse-named account, one had no Aspire record at all).

Also tallies 5-star reviews mentioning Jon, who is bonused per mention. Customers
spell it both "Jon" and "John".

Usage:
  python3 gbp-review-to-plans.py --dry-run     # default; prints, writes nothing
  python3 gbp-review-to-plans.py --live        # actually tags in Mailchimp
  python3 gbp-review-to-plans.py --backfill    # consider all reviews, not just new
"""

import json, os, re, sys, hashlib, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
sys.path.insert(0, os.path.join(REPO, "scripts"))

DRY_RUN = "--live" not in sys.argv
BACKFILL = "--backfill" in sys.argv
SINCE_ARG = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--since=")), None)
MAX_LOOKBACK_DAYS = 14   # a long outage should not fire a mass send
ALL_FIVE_STAR = "--all-five-star" in sys.argv

STATE_NAME = "gbp-review-to-plans"
QUEUE_FILE = os.path.join(REPO, "data", "plans-review-queue.json")

PLANS_TAG = "plans-prospect"          # triggers the Mailchimp journey
NAME_MATCH_THRESHOLD = 0.80
JON_RE = re.compile(r"\b(jon|john)\b", re.I)

# Never mail our own people. Reviews from staff surface as real Aspire contacts.
INTERNAL_DOMAINS = {"meangreenlawncare.com", "blackhilltx.com", "blackhilllandscaping.com"}

# Short forms a prefix test cannot catch (Nicholas does not start with Nick).
# Conservative on purpose: a wrong expansion mails the wrong person.
NICKNAMES = {
    "nick": "nicholas", "bob": "robert", "rob": "robert", "bill": "william",
    "will": "william", "dick": "richard", "rick": "richard", "jim": "james",
    "jimmy": "james", "joe": "joseph", "tom": "thomas", "tony": "anthony",
    "dave": "david", "dan": "daniel", "danny": "daniel", "mike": "michael",
    "steve": "steven", "chuck": "charles", "chris": "christopher",
    "sue": "susan", "susie": "susan", "suzie": "susan", "beth": "elizabeth",
    "liz": "elizabeth", "betty": "elizabeth", "kathy": "katherine",
    "kate": "katherine", "katie": "katherine", "peggy": "margaret",
    "meg": "margaret", "maggie": "margaret", "patty": "patricia",
    "pat": "patricia", "trish": "patricia", "jen": "jennifer",
    "jenny": "jennifer", "becky": "rebecca", "cathy": "catherine",
}


def canon(n):
    n = (n or "").strip().lower()
    return NICKNAMES.get(n, n)


# --- credentials (dual: ~/.config locally, env vars in CI) ---

def mailchimp_creds():
    key = (os.environ.get("MAILCHIMP_API_KEY") or "").strip()
    server = (os.environ.get("MAILCHIMP_SERVER") or "").strip()
    list_id = (os.environ.get("MAILCHIMP_LIST_ID") or "").strip()
    if not key:
        path = os.path.expanduser("~/.config/mailchimp/config.json")
        if os.path.exists(path):
            with open(path) as f:
                c = json.load(f)
            key = (c.get("api_key") or "").strip()
            server = (c.get("server_prefix") or "").strip()
            list_id = list_id or (c.get("list_id") or "").strip()
    # Trailing whitespace in the CI secret has silently broken Mailchimp writes
    # twice. Every one of these MUST stay stripped.
    if not list_id:
        raise SystemExit(
            "No Mailchimp audience id. Set MAILCHIMP_LIST_ID, or add a 'list_id' "
            "key to ~/.config/mailchimp/config.json. This repo is public, so the "
            "audience id is deliberately not hardcoded here.")
    return key, (server or "us20"), list_id


# --- Google Business Profile ---

def load_reviews():
    import importlib.util
    p = os.path.join(REPO, "scripts", "gbp-auth.py")
    spec = importlib.util.spec_from_file_location("gbp_auth", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    out, token = [], None
    while True:
        params = {"pageSize": 50, "orderBy": "updateTime desc"}
        if token:
            params["pageToken"] = token
        r = m.v4_get("reviews", params=params)
        out += r.get("reviews", [])
        token = r.get("nextPageToken")
        if not token:
            break
    return out


# --- Aspire ---

def aspire_client():
    import importlib.util
    # REPO is already the repo root (two dirnames up from scripts/), so the
    # "../scripts" that used to be here climbed out of the checkout entirely.
    # It resolved locally only because the parent directory happens to hold a
    # sibling copy; in CI there is no sibling and it failed every day from
    # 2026-09-01 with FileNotFoundError on
    # /home/runner/work/blackhill-lead-monitor/scripts/aspire-api-sync.py --
    # note the missing second repo-name segment.
    p = os.path.join(REPO, "scripts", "aspire-api-sync.py")
    spec = importlib.util.spec_from_file_location("asy", p)
    asy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(asy)
    cfg = asy.load_config()
    tok = asy.authenticate(cfg)

    def get(path, **params):
        url = cfg["api_base_url"].rstrip("/") + "/" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    return get


def split_name(display):
    parts = [p for p in re.split(r"[\s.]+", (display or "").strip()) if p]
    if not parts:
        return None, None
    return parts[0], parts[-1]


def first_name_score(review_first, contact_first):
    a = canon(review_first)
    b = canon(contact_first)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # "Matt" vs "Matthew", "Chris" vs "Christopher"
    if b.startswith(a) or a.startswith(b):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def match_contact(get, display_name):
    """Return (status, contact, note). status in: unique | ambiguous | none."""
    first, last = split_name(display_name)
    if not first or not last:
        return "none", None, "unparseable display name"
    esc = lambda s: s.replace("'", "''")
    if len(last) == 1:
        # "Sam M" -- surname is an initial, cannot resolve safely
        flt = (f"startswith(FirstName,'{esc(first)}') and "
               f"startswith(LastName,'{esc(last)}')")
    else:
        flt = f"LastName eq '{esc(last)}'"
    try:
        r = get("Contacts", **{"$filter": flt, "$top": "50"})
    except Exception as e:
        return "none", None, f"aspire error: {e}"
    rows = r if isinstance(r, list) else r.get("value", [])
    strong = [c for c in rows
              if first_name_score(first, c.get("FirstName")) >= NAME_MATCH_THRESHOLD]
    if len(strong) == 1:
        c = strong[0]
        em = (c.get("Email") or "").strip()
        if not em:
            return "none", c, "matched contact has no email in Aspire"
        if em.rsplit("@", 1)[-1].lower() in INTERNAL_DOMAINS:
            return "none", c, f"internal address, not a customer ({em})"
        return "unique", c, ""
    if len(strong) > 1:
        names = ", ".join(f"{c.get('FirstName')} {c.get('LastName')}" for c in strong[:5])
        return "ambiguous", None, f"{len(strong)} candidates: {names}"
    if rows:
        names = ", ".join(f"{c.get('FirstName')} {c.get('LastName')}" for c in rows[:5])
        return "none", None, f"surname matched but first name did not: {names}"
    return "none", None, "no Aspire contact with that surname"


# --- Mailchimp ---

def mc_request(method, path, payload=None):
    key, server, _ = mailchimp_creds()
    url = f"https://{server}.api.mailchimp.com/3.0/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    auth = __import__("base64").b64encode(f"bh:{key}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=45) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


def tag_contact(email, first, last):
    """Upsert the contact and apply PLANS_TAG. Verified by read-back."""
    _, _, list_id = mailchimp_creds()
    h = hashlib.md5(email.strip().lower().encode()).hexdigest()
    mc_request("PUT", f"lists/{list_id}/members/{h}", {
        "email_address": email.strip(),
        "status_if_new": "subscribed",
        "merge_fields": {"FNAME": first or "", "LNAME": last or ""},
    })
    mc_request("POST", f"lists/{list_id}/members/{h}/tags", {
        "tags": [{"name": PLANS_TAG, "status": "active"}]
    })
    # Mailchimp writes can report success without persisting. Always read back.
    back = mc_request("GET", f"lists/{list_id}/members/{h}/tags")
    return any(t.get("name") == PLANS_TAG for t in back.get("tags", []))


# --- state ---

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def review_window(state):
    """Reviews to consider this run.

    Normal daily run at 08:00 picks up everything posted since the last
    successful run, which is yesterday's reviews plus anything from this
    morning. If a run is missed the window stretches back to the last
    success so nothing is skipped, capped at MAX_LOOKBACK_DAYS so a long
    outage cannot trigger a mass send.
    """
    now = datetime.now(timezone.utc)
    if SINCE_ARG:
        start = datetime.fromisoformat(SINCE_ARG).replace(tzinfo=timezone.utc)
        return start, now, f"--since {SINCE_ARG}"
    last = state.get("last_run")
    if last:
        start = datetime.fromisoformat(last)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        floor = now - timedelta(days=MAX_LOOKBACK_DAYS)
        if start < floor:
            return floor, now, f"last run was over {MAX_LOOKBACK_DAYS} days ago, capped"
        return start, now, "since last successful run"
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now, "first run, defaulting to yesterday"


def in_window(rv, start, end):
    ct = rv.get("createTime")
    if not ct:
        return False
    try:
        t = datetime.fromisoformat(ct.replace("Z", "+00:00"))
    except ValueError:
        return False
    return start <= t <= end


def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] gbp-review-to-plans ({mode})")

    # Supabase, not a local file. The workflow used to cache a path the
    # script never wrote, so dedup state silently reset on every CI run.
    state = db.load_state(STATE_NAME, default={"routed": [], "jon_mentions": []})
    routed = set(state.get("routed", []))

    reviews = load_reviews()
    print(f"  {len(reviews)} reviews on profile")

    candidates = [r for r in reviews if r.get("starRating") == "FIVE"]
    if not ALL_FIVE_STAR:
        # Maintenance plans are irrigation-specific. Jon runs the irrigation
        # service calls, so a review naming him is the reliable signal that this
        # reviewer is an irrigation customer. --all-five-star overrides.
        candidates = [r for r in candidates if JON_RE.search(r.get("comment") or "")]
    if not BACKFILL:
        candidates = [r for r in candidates if r.get("reviewId") not in routed]
        w_start, w_end, why = review_window(state)
        before = len(candidates)
        candidates = [r for r in candidates if in_window(r, w_start, w_end)]
        print(f"  window {w_start.date()} to {w_end.date()} ({why})")
        print(f"  {before} unrouted -> {len(candidates)} inside the window")
    print(f"  {len(candidates)} five-star review(s) to consider")

    queue = load_json(QUEUE_FILE, [])
    tagged = jon_count = queued = 0

    for rv in candidates:
        rid = rv.get("reviewId")
        name = (rv.get("reviewer") or {}).get("displayName") or "(anonymous)"
        comment = rv.get("comment") or ""
        mentions_jon = bool(JON_RE.search(comment))
        if mentions_jon:
            jon_count += 1
            if rid not in {m.get("reviewId") for m in state.get("jon_mentions", [])}:
                state.setdefault("jon_mentions", []).append({
                    "reviewId": rid, "reviewer": name,
                    "createTime": rv.get("createTime"),
                    "spelling": (JON_RE.search(comment).group(0)),
                })

        get = main.aspire_get
        status, contact, note = match_contact(get, name)
        flag = " [mentions Jon]" if mentions_jon else ""

        if status == "unique":
            email = contact["Email"].strip()
            print(f"  MATCH   {name:22} -> {contact.get('FirstName')} {contact.get('LastName')} <{email}>{flag}")
            if DRY_RUN:
                print(f"          would tag '{PLANS_TAG}'")
            else:
                ok = tag_contact(email, contact.get("FirstName"), contact.get("LastName"))
                print(f"          tagged '{PLANS_TAG}': {'verified' if ok else 'FAILED READ-BACK'}")
                if ok:
                    tagged += 1
                    routed.add(rid)
        else:
            print(f"  QUEUE   {name:22} -> {status}: {note}{flag}")
            if rid not in {q.get("reviewId") for q in queue}:
                queue.append({
                    "reviewId": rid, "reviewer": name,
                    "createTime": rv.get("createTime"),
                    "comment": comment[:400], "reason": note,
                    "mentions_jon": mentions_jon,
                })
                queued += 1

    print(f"\n  tagged={tagged}  queued_for_review={queued}  jon_mentions_seen={jon_count}")
    if DRY_RUN:
        print("  DRY RUN: no Mailchimp writes, no state saved. Re-run with --live to apply.")
    else:
        state["routed"] = sorted(routed)
        if not DRY_RUN:
            state["last_run"] = datetime.now(timezone.utc).isoformat()
        db.save_state(STATE_NAME, state)
        save_json(QUEUE_FILE, queue)
        print(f"  state -> Supabase ({STATE_NAME})")
        print(f"  queue -> {QUEUE_FILE}")


if __name__ == "__main__":
    main.aspire_get = aspire_client()
    main()
