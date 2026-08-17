#!/usr/bin/env python3
"""Create the server-side Outlook rule that files DMARC reports on arrival.

Run once. Idempotent: re-running updates the existing rule rather than
stacking duplicates.

Providers deliver aggregate reports around the clock. dmarc-monitor.py sweeps
every 2 hours, so without this rule a report sits visible in the Inbox for up
to 2 hours. The rule files it in the moment it lands, so the Inbox never shows
one at all.

The rule is an optimisation, NOT a dependency. dmarc-monitor.py scans the Inbox
as well as the report folder, so removing this rule degrades tidiness and
nothing else.

CRITICAL: the rule must not mark messages read. Unread is the monitor's work
queue -- a rule that marked them read would file every report straight past the
parser and the monitor would report all-clear forever while analysing nothing.
That is why markAsRead is absent below rather than merely set false.

Usage:
    python3 scripts/setup-dmarc-inbox-rule.py            # create or update
    python3 scripts/setup-dmarc-inbox-rule.py --dry-run  # show, change nothing
    python3 scripts/setup-dmarc-inbox-rule.py --remove   # delete the rule
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

GRAPH = "https://graph.microsoft.com/v1.0"
RULE_NAME = "File DMARC aggregate reports"
REPORT_FOLDER = "DMARC Reports"

# Matches every aggregate-report subject seen in this mailbox: Microsoft's
# "Report Domain: ...", Google's lowercase "Report domain: ...", Yahoo and
# Mimecast variants, and Microsoft's "[Preview] Report Domain: ...".
# subjectContains is case-insensitive, so one spelling covers the casings.
SUBJECT_MATCHES = ["Report Domain", "Report-ID", "DMARC"]

DRY_RUN = "--dry-run" in sys.argv
REMOVE = "--remove" in sys.argv


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def graph_token():
    """App-only token, same client-credentials flow dmarc-monitor.py uses."""
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
    url = f"{GRAPH}{path}"
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
        detail = e.read().decode(errors="replace")[:400]
        # Creating rules needs MailboxSettings.ReadWrite, which is a different
        # grant from the Mail.* scopes the monitor uses. Say so plainly rather
        # than leaving a bare 403 for someone to decode later.
        if e.code == 403:
            raise SystemExit(
                "403 from Graph. The app registration needs the application "
                "permission MailboxSettings.ReadWrite (admin-consented) to "
                f"manage inbox rules.\nDetail: {detail}")
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from e


def find_folder(token, mailbox, name):
    res = graph(token, "GET",
                f"/users/{mailbox}/mailFolders?$top=100&$select=id,displayName")
    for f in res.get("value", []):
        if f["displayName"].lower() == name.lower():
            return f["id"]
    return None


def main():
    mailbox = os.environ.get("MS_USER_EMAIL", "")
    if not mailbox:
        raise SystemExit("Missing MS_USER_EMAIL")

    token = graph_token()

    folder_id = find_folder(token, mailbox, REPORT_FOLDER)
    if not folder_id:
        if DRY_RUN:
            log(f"'{REPORT_FOLDER}' does not exist yet (dry run, not creating)")
            return
        # dmarc-monitor.py creates this on its first run; create it here too so
        # the rule can be set up before the monitor has ever run.
        folder_id = graph(token, "POST", f"/users/{mailbox}/mailFolders",
                          {"displayName": REPORT_FOLDER})["id"]
        log(f"Created folder '{REPORT_FOLDER}'")

    existing = graph(token, "GET",
                     f"/users/{mailbox}/mailFolders/inbox/messageRules")
    mine = next((r for r in existing.get("value", [])
                 if r.get("displayName") == RULE_NAME), None)

    if REMOVE:
        if not mine:
            log("Rule not present, nothing to remove")
            return
        if DRY_RUN:
            log(f"Would delete rule '{RULE_NAME}'")
            return
        graph(token, "DELETE",
              f"/users/{mailbox}/mailFolders/inbox/messageRules/{mine['id']}")
        log(f"Deleted rule '{RULE_NAME}'")
        return

    rule = {
        "displayName": RULE_NAME,
        "sequence": 1,
        "isEnabled": True,
        "conditions": {"subjectContains": SUBJECT_MATCHES},
        # No markAsRead. See the module docstring: unread is the monitor's queue.
        # stopProcessingRules stays false so any other inbox rule still runs.
        "actions": {"moveToFolder": folder_id, "stopProcessingRules": False},
    }

    if DRY_RUN:
        log(("Would update" if mine else "Would create") + f" rule '{RULE_NAME}':")
        print(json.dumps(rule, indent=2))
        return

    if mine:
        graph(token, "PATCH",
              f"/users/{mailbox}/mailFolders/inbox/messageRules/{mine['id']}", rule)
        log(f"Updated existing rule '{RULE_NAME}'")
    else:
        graph(token, "POST",
              f"/users/{mailbox}/mailFolders/inbox/messageRules", rule)
        log(f"Created rule '{RULE_NAME}'")

    # Read it back. Graph accepts a rule and then reports isEnabled false if the
    # mailbox rejected it, so a write that "succeeded" is not proof of anything.
    check = graph(token, "GET",
                  f"/users/{mailbox}/mailFolders/inbox/messageRules")
    live = next((r for r in check.get("value", [])
                 if r.get("displayName") == RULE_NAME), None)
    if not live:
        raise SystemExit("Rule is not present on read-back")
    log(f"Verified: enabled={live.get('isEnabled')}, "
        f"subjectContains={live.get('conditions', {}).get('subjectContains')}, "
        f"moveToFolder set={bool(live.get('actions', {}).get('moveToFolder'))}")


if __name__ == "__main__":
    main()
