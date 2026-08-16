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


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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


def find_reports(token, mailbox, folder_id=None):
    """Unparsed DMARC report messages, from the Inbox and the report folder.

    Both locations are scanned deliberately. If an Outlook rule files reports
    on arrival they never touch the Inbox, and a monitor that only looked there
    would silently find nothing and report all-clear forever. Scanning both
    means the rule is an optimisation rather than a dependency.
    """
    # Providers vary the subject, but every report carries "DMARC" somewhere.
    # Filtering server-side keeps this cheap.
    query = ("?$top=50&$select=id,subject,receivedDateTime,hasAttachments,parentFolderId"
             "&$search=%22dmarc%22")
    seen, out = set(), []
    locations = [f"/users/{mailbox}/mailFolders/Inbox/messages"]
    if folder_id:
        locations.append(f"/users/{mailbox}/mailFolders/{folder_id}/messages")
    for loc in locations:
        try:
            res = graph(token, "GET", loc + query)
        except RuntimeError as e:
            log(f"  Could not search {loc.rsplit('/', 2)[1]}: {e}")
            continue
        for m in res.get("value", []):
            if m.get("hasAttachments") and m["id"] not in seen:
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
    """Split failing volume into the two buckets that need different responses."""
    breaks, unauth = defaultdict(int), defaultdict(int)
    passed = 0
    for r in records:
        if r["dkim"] == "pass" or r["spf"] == "pass":
            passed += r["count"]
            continue
        # Failed DMARC. Does any underlying check recognise a domain of ours?
        seen = {d for d, _ in r["dkim_auth"] + r["spf_auth"]}
        recognised = any(
            d == k or d.endswith("." + k)
            for d in seen for k in KNOWN_SENDING_DOMAINS
        )
        key = (r["source_ip"], ", ".join(sorted(seen)) or "no auth domain")
        (breaks if recognised else unauth)[key] += r["count"]
    return passed, breaks, unauth


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


def build_body(window, passed, breaks, unauth, policy, domain):
    lines = [
        f"DMARC findings for {domain} ({window})",
        f"Current policy: p={policy}",
        "",
        f"Messages that authenticated correctly: {passed:,}",
        "",
    ]
    if breaks:
        lines += [
            "NEEDS FIXING - our own mail failing authentication",
            "These will start bouncing if the policy moves off p=none.",
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
    log(f"Found {len(messages)} candidate report message(s) in Inbox")

    passed_total = 0
    breaks, unauth = defaultdict(int), defaultdict(int)
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
            p, b, u = classify(records)
            passed_total += p
            for k, v in b.items():
                breaks[k] += v
            for k, v in u.items():
                unauth[k] += v
            windows.append(org)
            seen.add(rid)
            got_one = True

        if got_one:
            processed.append(msg["id"])

    # File the reports away so the inbox stays clean whether or not we alert.
    if not DRY_RUN and folder_id:
        # Only move what is not already filed. Reports found in the report
        # folder (put there by the Outlook rule) still get parsed, but moving
        # them into the folder they already occupy is a pointless API call.
        already = {m["id"] for m in messages if m.get("parentFolderId") == folder_id}
        for mid in [m for m in processed if m not in already]:
            try:
                graph(token, "POST", f"/users/{mailbox}/messages/{mid}/move",
                      {"destinationId": folder_id})
            except Exception as e:
                log(f"  Could not file message {mid[:12]}: {e}")
        log(f"Filed {len(processed)} report(s) into '{REPORT_FOLDER}'")

    total_fail = sum(breaks.values()) + sum(unauth.values())
    log(f"Parsed {len(windows)} new report(s): {passed_total:,} passed, "
        f"{sum(breaks.values()):,} failing (ours), {sum(unauth.values()):,} failing (unknown)")

    if breaks or (SUMMARY and total_fail):
        window = ", ".join(sorted(set(windows))) or "latest"
        subject = ("DMARC: our mail is failing authentication"
                   if breaks else "DMARC summary")
        body = build_body(window, passed_total, breaks, unauth, policy, domain)
        if DRY_RUN:
            log("DRY RUN - would have sent:\n" + body)
        else:
            send_alert(token, mailbox, subject, body)
            log(f"Alert sent to {ALERT_TO}")
    else:
        log("Nothing worth reporting. Staying silent.")

    if not DRY_RUN:
        state["seen_report_ids"] = sorted(seen)[-500:]
        state["stats"] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_passed": passed_total,
            "last_failing_ours": sum(breaks.values()),
            "last_failing_unknown": sum(unauth.values()),
        }
        db.save_state(STATE_NAME, state)


if __name__ == "__main__":
    main()
