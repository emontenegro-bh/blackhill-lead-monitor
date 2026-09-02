#!/usr/bin/env python3
"""DMARC aggregate report monitor - stays silent unless something is wrong.

Mail providers send a DMARC aggregate report every day for every domain with
a rua= tag. They are compressed XML and unreadable by eye, and there is one
per provider per day, so they bury the inbox while hiding the one thing worth
knowing: is anything sending mail as blackhilltx.com that fails authentication.

This reads those reports out of the mailbox, parses them, files them away, and
emails ONLY when there is a real finding. A clean day produces no email.

Two kinds of finding, and the distinction matters:

  BREAKS-ON-ENFORCEMENT  A source that fails DMARC but passes SPF or DKIM for
                         a domain we recognise. Almost always our own mail
                         misconfigured. This is the one to fix, because it
                         starts bouncing the moment p= moves off none.

  UNAUTHENTICATED        A source failing everything. Usually spoofing or
                         someone forwarding our mail. Informational at p=none.

  FORWARDED              A recipient's mail system re-sent our message and
                         signed it with its own DKIM key. Nothing to fix and
                         nothing to alert on. Only the forwarders named in
                         KNOWN_FORWARDERS are treated this way, because an
                         unidentified third-party signer could be a spoofer
                         signing with a domain of their own.

Usage:
  python3 scripts/dmarc-monitor.py             # normal run
  python3 scripts/dmarc-monitor.py --dry-run   # parse and print, send nothing
  python3 scripts/dmarc-monitor.py --summary   # print findings even if clean
"""

import base64
import gzip
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

DRY_RUN = "--dry-run" in sys.argv
SUMMARY = "--summary" in sys.argv

STATE_NAME = "dmarc-monitor"
GRAPH = "https://graph.microsoft.com/v1.0"
REPORT_FOLDER = "DMARC Reports"
ALERT_TO = "evelin@blackhilltx.com"

# Days a failing source stays quiet after it has been alerted on once. Long
# enough that a daily forwarder does not mail every morning, short enough that
# a real misconfiguration nobody fixed comes back and says so again.
SUPPRESS_DAYS = 7

# Senders we expect to send as blackhilltx.com. A failure from one of these is
# our own misconfiguration and needs fixing before p= moves off none.
#
# Kept deliberately tight, matching the live SPF record:
#   v=spf1 include:spf.protection.outlook.com include:servers.mcsv.net -all
# Nothing else is authorised, so nothing else belongs here. SendGrid is
# configured as a secret but no script actually sends through it, and adding it
# would misfile a spoofer as "our own mail". Confirmed 2026-08-12.
#
# Note blackhillassistant@gmail.com sends via Gmail SMTP, but as gmail.com, so
# it never appears in a blackhilltx.com report. If a script ever sends FROM an
# @blackhilltx.com address through Gmail, it will show up here as a genuine
# finding, which is exactly what we want.
KNOWN_SENDING_DOMAINS = {
    "blackhilltx.com",
    "meangreenlawncare.com",
    "protection.outlook.com", "outlook.com",       # Microsoft 365
    "mcsv.net", "mcdlv.net", "rsgsv.net",          # Mailchimp
}

# Recipients whose mail system re-signs and re-sends what we send them.
#
# tbrown131@lamar.edu is a fertilization customer. Lamar University runs on
# Microsoft, so when Carlos emails Tom the message leaves again from Lamar's
# outbound host, still carrying blackhilltx.com in the From header. SPF fails
# (Lamar's IP is not in our record and never can be) and Lamar's own DKIM
# signature replaces ours, so DMARC reports a failure for a message that is
# working exactly as intended.
#
# Before 2026-09-02 this landed in BREAKS-ON-ENFORCEMENT and told Evelin her
# own sender needed fixing, because the SPF auth domain still reads
# blackhilltx.com. One customer's mail forwarding generated that alarm on
# 08-20 and again on 09-02. Nothing here is fixable from our side.
KNOWN_FORWARDERS = {
    "lamar.edu",
}


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _iso_ts(value):
    """Epoch seconds for a stored ISO timestamp, or 0 if it is unparseable.

    Returning 0 rather than raising means a corrupt entry is pruned on the next
    run instead of crashing the monitor, which would take the alerting down
    with it.
    """
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def graph_token():
    """App-only token, same client-credentials flow the lead monitor uses."""
    tenant = os.environ.get("MS_TENANT_ID", "")
    client = os.environ.get("MS_CLIENT_ID", "")
    secret = os.environ.get("MS_CLIENT_SECRET", "")
    if not (tenant and client and secret):
        raise SystemExit("Missing MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET")

    data = urllib.parse.urlencode({
        "client_id": client,
        "client_secret": secret,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())["access_token"]


def graph(token, method, path, body=None):
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from e


def ensure_report_folder(token, mailbox):
    """Find or create the folder processed reports get filed into."""
    res = graph(token, "GET",
                f"/users/{mailbox}/mailFolders?$top=100&$select=id,displayName")
    for f in res.get("value", []):
        if f["displayName"].lower() == REPORT_FOLDER.lower():
            return f["id"]
    if DRY_RUN:
        return None
    created = graph(token, "POST", f"/users/{mailbox}/mailFolders",
                    {"displayName": REPORT_FOLDER})
    log(f"Created folder '{REPORT_FOLDER}'")
    return created["id"]


# Providers vary the subject wildly, but every aggregate report carries one of
# these. Matched client-side because $filter has no substring operator for
# subject, and $search is not an option here (see find_reports).
SUBJECT_HINTS = ("report domain", "report-id", "dmarc")


def looks_like_report(msg):
    subject = (msg.get("subject") or "").lower()
    return any(hint in subject for hint in SUBJECT_HINTS)


def find_reports(token, mailbox, folder_id=None):
    """Unread DMARC report messages, from the Inbox and the report folder.

    Both locations are scanned deliberately. If an Outlook rule files reports
    on arrival they never touch the Inbox, and a monitor that only looked there
    would silently find nothing and report all-clear forever. Scanning both
    means the rule is an optimisation rather than a dependency.

    Unread is the work queue, selected with a folder-scoped $filter. $search
    cannot be used: Graph ignores the mailFolders segment whenever $search is
    present and searches the entire mailbox, so both "locations" above returned
    the same mailbox-wide hits and the monitor kept re-reading its own archive.
    The 2026-08-17 14:47 run shows it exactly -- 13 already-filed reports found,
    every one matched against the seen-list, "Parsed 0 new", then all 13 re-filed.
    The quiet half was worse: a growing archive crowds genuinely new reports out
    of the $top window, so this would go permanently silent while still logging
    a clean run.
    """
    query = "?" + urllib.parse.urlencode({
        "$top": "50",
        "$filter": "isRead eq false",
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,receivedDateTime,hasAttachments,parentFolderId",
    }, quote_via=urllib.parse.quote)
    seen, out = set(), []
    locations = [f"/users/{mailbox}/mailFolders/Inbox/messages"]
    if folder_id:
        locations.append(f"/users/{mailbox}/mailFolders/{folder_id}/messages")
    for loc in locations:
        try:
            res = graph(token, "GET", loc + query)
        except RuntimeError as e:
            log(f"  Could not read {loc.rsplit('/', 2)[1]}: {e}")
            continue
        for m in res.get("value", []):
            if not (m.get("hasAttachments") and looks_like_report(m)):
                continue
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
    return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def extract_xml(raw, filename):
    """DMARC reports arrive gzipped or zipped. Return the XML bytes."""
    name = (filename or "").lower()
    if name.endswith(".gz"):
        return gzip.decompress(raw)
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            inner = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if inner:
                return z.read(inner[0])
        return None
    if name.endswith(".xml"):
        return raw
    return None


def parse_report(xml_bytes):
    """Return (org, domain, policy, [record...]) from one aggregate report."""
    root = ElementTree.fromstring(xml_bytes)
    meta = root.find("report_metadata")
    pub = root.find("policy_published")
    org = (meta.findtext("org_name") if meta is not None else "") or "unknown"
    domain = (pub.findtext("domain") if pub is not None else "") or "unknown"
    policy = (pub.findtext("p") if pub is not None else "") or "none"

    records = []
    for rec in root.findall("record"):
        row = rec.find("row")
        if row is None:
            continue
        ev = row.find("policy_evaluated")
        auth = rec.find("auth_results")

        def auth_domains(kind):
            out = []
            if auth is not None:
                for node in auth.findall(kind):
                    d, r = node.findtext("domain"), node.findtext("result")
                    if d:
                        out.append((d.lower(), (r or "").lower()))
            return out

        records.append({
            "source_ip": row.findtext("source_ip") or "?",
            "count": int(row.findtext("count") or 0),
            "dkim": (ev.findtext("dkim") if ev is not None else "") or "fail",
            "spf": (ev.findtext("spf") if ev is not None else "") or "fail",
            "header_from": (rec.findtext("identifiers/header_from") or "").lower(),
            "dkim_auth": auth_domains("dkim"),
            "spf_auth": auth_domains("spf"),
        })
    return org, domain, policy, records


def classify(records):
    """Split failing volume into the buckets that need different responses."""
    breaks, unauth, forwarded = defaultdict(int), defaultdict(int), defaultdict(int)
    passed = 0
    for r in records:
        if r["dkim"] == "pass" or r["spf"] == "pass":
            passed += r["count"]
            continue
        # Failed DMARC. Does any underlying check recognise a domain of ours?
        seen = {d for d, _ in r["dkim_auth"] + r["spf_auth"]}

        # A DKIM signature that PASSES for a domain that is not ours means some
        # third party signed this message on its way through, which is what
        # forwarding looks like from the report side.
        #
        # Only the forwarders we have actually identified are silenced. An
        # unknown third-party signer could just as easily be a spoofer signing
        # with a domain of their own, and that has to keep alerting -- so
        # anything not on the list falls through to the checks below and
        # behaves exactly as it did before.
        relay = sorted(
            d for d, res in r["dkim_auth"]
            if res == "pass" and any(d == k or d.endswith("." + k)
                                     for k in KNOWN_FORWARDERS)
        )
        if relay:
            forwarded[(r["source_ip"], ", ".join(relay))] += r["count"]
            continue

        recognised = any(
            d == k or d.endswith("." + k)
            for d in seen for k in KNOWN_SENDING_DOMAINS
        )
        key = (r["source_ip"], ", ".join(sorted(seen)) or "no auth domain")
        (breaks if recognised else unauth)[key] += r["count"]
    return passed, breaks, unauth, forwarded


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def send_alert(token, mailbox, subject, body):
    graph(token, "POST", f"/users/{mailbox}/sendMail", {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": ALERT_TO}}],
        },
        "saveToSentItems": True,
    })


def build_body(window, passed, breaks, unauth, policy, domain, forwarded=None):
    lines = [
        f"DMARC findings for {domain} ({window})",
        f"Current policy: p={policy}",
        "",
        f"Messages that authenticated correctly: {passed:,}",
        "",
    ]
    if breaks:
        # The consequence depends on the policy that is actually published, so
        # read it rather than assuming. This line used to say "will start
        # bouncing if the policy moves off p=none" unconditionally, which was
        # wrong in both directions once the domains moved to p=quarantine on
        # 2026-08-20: the move had already happened, and quarantine sends mail
        # to junk rather than bouncing it.
        consequence = {
            "none": "Nothing is being blocked yet. These are what would break "
                    "if the policy moves to quarantine or reject.",
            "quarantine": "THIS IS LIVE. Mail from these sources is being "
                          "delivered to the recipient's junk folder right now.",
            "reject": "THIS IS LIVE. Mail from these sources is being rejected "
                      "outright right now.",
        }.get(policy, f"Published policy is p={policy}.")
        lines += [
            "MAIL USING OUR DOMAIN IS FAILING AUTHENTICATION",
            consequence,
            "",
            "This is EITHER our own misconfigured sender OR someone forging",
            "the domain. The check below cannot tell them apart -- it asks",
            "whether one of our domains is involved, which is true in both",
            "cases -- so read the source IP before assuming it is ours:",
            "",
            "  Microsoft or Mailchimp address  -> usually our own mail, but see",
            "                                     below before assuming it",
            "  anything else                   -> a forgery, and the policy",
            "                                     above is already handling it",
            "",
            "A Microsoft address is not proof the mail is ours. Microsoft's",
            "outbound hosts are shared, so a customer whose employer runs on",
            "Microsoft will relay our mail from one of those same addresses.",
            "That is how lamar.edu read as our own broken sender twice.",
            "",
            "Between 2026-08-21 and 08-26 three different ColoCrossing hosts",
            "forged meangreenlawncare.com. None of them were ours and none",
            "needed a fix.",
            "",
        ]
        for (ip, doms), n in sorted(breaks.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>6,} messages   {ip}   ({doms})")
        lines.append("")
    if unauth:
        lines += [
            "UNRECOGNISED SENDERS - failing everything",
            "Usually spoofing or a forwarder. No action needed at p=none.",
            "",
        ]
        for (ip, doms), n in sorted(unauth.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"  {n:>6,} messages   {ip}   ({doms})")
        lines.append("")
    if forwarded:
        lines += [
            "FORWARDED - no action, listed so the count adds up",
            "A recipient's mail system re-sent our message and signed it with",
            "its own DKIM key. SPF cannot pass across a forward and never will.",
            "This is our mail reaching a real customer.",
            "",
        ]
        for (ip, doms), n in sorted(forwarded.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>6,} messages   {ip}   ({doms})")
        lines.append("")
    lines.append("Reports were filed into the '%s' folder." % REPORT_FOLDER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main():
    mailbox = os.environ.get("MS_USER_EMAIL") or ALERT_TO
    token = graph_token()
    state = db.load_state(STATE_NAME, default={"seen_report_ids": [], "stats": {}})
    seen = set(state.get("seen_report_ids") or [])

    folder_id = ensure_report_folder(token, mailbox)
    messages = find_reports(token, mailbox, folder_id)
    # Split the count by location: an Inbox hit means the Outlook rule missed
    # one, which is worth noticing rather than burying in a single total.
    in_inbox = sum(1 for m in messages if m.get("parentFolderId") != folder_id)
    log(f"Found {len(messages)} unread report message(s): {in_inbox} in Inbox, "
        f"{len(messages) - in_inbox} in '{REPORT_FOLDER}'")

    passed_total = 0
    breaks, unauth, forwarded = defaultdict(int), defaultdict(int), defaultdict(int)
    policy = domain = "?"
    processed, windows = [], []

    for msg in messages:
        atts = graph(token, "GET",
                     f"/users/{mailbox}/messages/{msg['id']}/attachments").get("value", [])
        got_one = False
        for att in atts:
            content = att.get("contentBytes")
            if not content:
                continue
            try:
                xml = extract_xml(base64.b64decode(content), att.get("name"))
                if not xml:
                    continue
                org, domain, policy, records = parse_report(xml)
            except Exception as e:
                # Flag and continue: one malformed report must not stop the run.
                log(f"  Could not parse {att.get('name')}: {e}")
                continue

            rid = f"{org}:{msg['id']}"
            if rid in seen:
                got_one = True
                continue
            p, b, u, f = classify(records)
            passed_total += p
            for k, v in b.items():
                breaks[k] += v
            for k, v in u.items():
                unauth[k] += v
            for k, v in f.items():
                forwarded[k] += v
            windows.append(org)
            seen.add(rid)
            got_one = True

        if got_one:
            processed.append(msg["id"])

    # Clear the queue, and file away anything the Outlook rule did not catch.
    if not DRY_RUN:
        # Only move what is not already filed. Reports found in the report
        # folder (put there by the Outlook rule) still get parsed, but moving
        # them into the folder they already occupy is a pointless API call.
        already = {m["id"] for m in messages if m.get("parentFolderId") == folder_id}
        filed = 0
        for mid in processed:
            try:
                # Marking read is what takes a report off the queue, and it has
                # to happen before any move: a move rewrites the message id, so
                # doing it the other way round would patch an id that no longer
                # exists. Leaving filed reports in place is also what keeps the
                # seen-list meaningful, since ids are stable while a message
                # stays put.
                graph(token, "PATCH", f"/users/{mailbox}/messages/{mid}",
                      {"isRead": True})
                if folder_id and mid not in already:
                    graph(token, "POST", f"/users/{mailbox}/messages/{mid}/move",
                          {"destinationId": folder_id})
                    filed += 1
            except Exception as e:
                log(f"  Could not clear message {mid[:12]}: {e}")
        log(f"Marked {len(processed)} report(s) processed, "
            f"filed {filed} into '{REPORT_FOLDER}'")

    total_fail = sum(breaks.values()) + sum(unauth.values())
    log(f"Parsed {len(windows)} new report(s): {passed_total:,} passed, "
        f"{sum(breaks.values()):,} failing (ours), {sum(unauth.values()):,} failing (unknown), "
        f"{sum(forwarded.values()):,} forwarded (no action)")

    # Suppress repeats of a source already reported recently.
    #
    # Most recurring "failures" are forwarding: a recipient auto-forwards our
    # mail, the forwarder re-sends it from its own IP, and SPF alignment breaks
    # on the way through. That is not fixable from our side and it reports
    # every single day. An alert that arrives daily for a source already known
    # stops being read, which costs us the one that matters. A source goes
    # quiet for SUPPRESS_DAYS and then speaks up again, so nothing is lost
    # permanently, only repeated less.
    alerted = dict(state.get("alerted_sources") or {})
    now = datetime.now(timezone.utc)

    def _age(fp):
        prev = alerted.get(fp)
        if not prev:
            return None
        try:
            return (now - datetime.fromisoformat(prev)).days
        except ValueError:
            return None

    fresh = {}
    for key, n in breaks.items():
        ip, domains = key
        # TWO keys, and the second one is the point.
        #
        # Keying only on the exact IP is defeated entirely by a spoofer that
        # rotates hosts. Between 2026-08-21 and 08-26 three different
        # ColoCrossing addresses forged meangreenlawncare.com --
        # 104.168.101.10, 198.46.243.200, 198.23.177.22 -- and each one read
        # as a brand new source and alerted. The domain-level key means
        # "someone is forging this domain, you already know" and holds across
        # the rotation.
        #
        # The exact-IP key is kept as well, so a genuine misconfiguration on
        # one of our own hosts still alerts on its own merits rather than
        # being hidden by an unrelated spoof of the same domain.
        ip_age = _age(f"{ip}|{domains}")
        dom_age = _age(f"domain:{domains}")
        if ip_age is not None and ip_age < SUPPRESS_DAYS:
            log(f"  Suppressing known source {ip} "
                f"({n} msg, last alerted {ip_age}d ago)")
            continue
        if dom_age is not None and dom_age < SUPPRESS_DAYS:
            log(f"  Suppressing {ip} - {domains} already reported "
                f"{dom_age}d ago from a different address (rotating source)")
            continue
        fresh[key] = n

    if fresh or (SUMMARY and total_fail):
        window = ", ".join(sorted(set(windows))) or "latest"
        subject = ("DMARC: mail using our domain is failing authentication"
                   if fresh else "DMARC summary")
        body = build_body(window, passed_total, fresh or breaks, unauth,
                          policy, domain, forwarded)
        if DRY_RUN:
            log("DRY RUN - would have sent:\n" + body)
        else:
            send_alert(token, mailbox, subject, body)
            log(f"Alert sent to {ALERT_TO}")
    else:
        log("Nothing worth reporting. Staying silent.")

    if not DRY_RUN:
        state["seen_report_ids"] = sorted(seen)[-500:]
        # Stamp only what was actually alerted on. Stamping a suppressed source
        # would keep pushing its window forward and silence it forever.
        for key in fresh:
            alerted[f"{key[0]}|{key[1]}"] = now.isoformat()
            # Also stamp the domain, so the next rotation of a spoofing
            # source is suppressed rather than treated as news.
            alerted[f"domain:{key[1]}"] = now.isoformat()
        cutoff = now.timestamp() - SUPPRESS_DAYS * 2 * 86400
        state["alerted_sources"] = {
            k: v for k, v in alerted.items()
            if _iso_ts(v) >= cutoff
        }
        state["stats"] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_passed": passed_total,
            "last_failing_ours": sum(breaks.values()),
            "last_failing_unknown": sum(unauth.values()),
        }
        db.save_state(STATE_NAME, state)


if __name__ == "__main__":
    with db.track("dmarc-monitor"):
        main()
