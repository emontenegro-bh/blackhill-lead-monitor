#!/usr/bin/env python3
"""
Tag Mailchimp contacts who have bought an irrigation maintenance plan.

Watches Aspire for won opportunities named "* Irrigation Maintenance Plan" and tags the
matching Mailchimp contact `plan-signed`. The maintenance-plan customer journey branches on
that tag and drops the contact out, so nobody who has already bought is asked to buy again.

Plan opportunities in Aspire are named consistently, which is what makes this reliable:
    Essential Irrigation Maintenance Plan    $600/yr
    Preferred Irrigation Maintenance Plan    $900/yr
    Premier Irrigation Maintenance Plan      $1,800/yr

Dry run by default. Pass --live to actually write tags.

Credentials: ~/.config/{aspire,mailchimp}/config.json locally, env vars in CI.
"""
import json, os, sys, base64, hashlib, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

DRY_RUN = "--live" not in sys.argv
TAG = "plan-signed"
PLAN_NAME_MATCH = "Irrigation Maintenance Plan"
STATE_NAME = "plan-signed-tagger"
INTERNAL_DOMAINS = {"meangreenlawncare.com", "blackhilltx.com", "blackhilllandscaping.com"}


def _cfg(service):
    p = os.path.expanduser(f"~/.config/{service}/config.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def aspire_creds():
    cid = (os.environ.get("ASPIRE_CLIENT_ID") or "").strip()
    sec = (os.environ.get("ASPIRE_SECRET") or "").strip()
    base = (os.environ.get("ASPIRE_API_URL") or "").strip()
    if cid and sec:
        return cid, sec, base or "https://cloud-api.youraspire.com"
    c = _cfg("aspire")
    return c.get("api_client_id"), c.get("api_secret"), c.get("api_base_url")


def mailchimp_creds():
    key = (os.environ.get("MAILCHIMP_API_KEY") or "").strip()
    srv = (os.environ.get("MAILCHIMP_SERVER") or "").strip()
    lid = (os.environ.get("MAILCHIMP_LIST_ID") or "").strip()
    if not key:
        c = _cfg("mailchimp")
        key = (c.get("api_key") or "").strip()
        srv = srv or (c.get("server") or "").strip()
        lid = lid or (c.get("list_id") or "").strip()
    if not key or not lid:
        raise SystemExit("Mailchimp API key or list id missing")
    return key, (srv or "us20"), lid


def _json(req):
    return json.load(urllib.request.urlopen(req))


def aspire_token(cid, sec, base):
    body = json.dumps({"ClientId": cid, "Secret": sec}).encode()
    req = urllib.request.Request(f"{base}/Authorization", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return _json(req)["Token"]


def aspire_get(base, token, path, query):
    # spaces must be percent-encoded or http.client rejects the URL outright
    safe_chars = "=&$,'()"
    url = base + "/" + path + "?" + urllib.parse.quote(query, safe=safe_chars)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    d = _json(req)
    return d if isinstance(d, list) else d.get("value", d.get("Items", []))


def won_plan_opportunities(base, token):
    q = (f"$filter=contains(OpportunityName,'{PLAN_NAME_MATCH}') and "
         f"OpportunityStatusName eq 'Won'&$orderby=OpportunityID desc&$top=200")
    return aspire_get(base, token, "Opportunities", q)


def contact_email(base, token, contact_id):
    if not contact_id:
        return None, None
    rows = aspire_get(base, token, "Contacts",
                      f"$filter=ContactID eq {contact_id}&$select=ContactID,FirstName,LastName,Email")
    if not rows:
        return None, None
    r = rows[0]
    name = f"{r.get('FirstName') or ''} {r.get('LastName') or ''}".strip()
    return (r.get("Email") or "").strip() or None, name


def mc_call(srv, key, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://{srv}.api.mailchimp.com/3.0{path}",
                                 data=data, method=method)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"anystring:{key}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req)
    raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def tag_contact(srv, key, lid, email):
    """Apply the tag, then read it back. Mailchimp writes can report success and not persist."""
    h = hashlib.md5(email.lower().encode()).hexdigest()
    mc_call(srv, key, f"/lists/{lid}/members/{h}/tags", "POST",
            {"tags": [{"name": TAG, "status": "active"}]})
    got = mc_call(srv, key, f"/lists/{lid}/members/{h}/tags")
    return any(t.get("name") == TAG for t in got.get("tags", []))


def load_state():
    # Supabase, not the repo. Repo-path state caused the read-modify-write
    # race that double-emailed customers; a miss here would re-tag buyers.
    return db.load_state(STATE_NAME,
                         default={"tagged_opportunity_ids": [], "last_run": None})


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    db.save_state(STATE_NAME, state)


def main():
    acid, asec, abase = aspire_creds()
    if not acid or not asec:
        raise SystemExit("Aspire credentials missing")
    key, srv, lid = mailchimp_creds()

    state = load_state()
    seen = set(state.get("tagged_opportunity_ids", []))

    token = aspire_token(acid, asec, abase)
    opps = won_plan_opportunities(abase, token)
    print(f"{'DRY RUN' if DRY_RUN else 'LIVE'}  won plan opportunities in Aspire: {len(opps)}")

    newly, skipped, failed = [], 0, []
    for o in opps:
        oid = o.get("OpportunityID")
        if oid in seen:
            skipped += 1
            continue
        email, name = contact_email(abase, token, o.get("BillingContactID"))
        label = f"#{oid} {str(o.get('OpportunityName'))[:38]}"
        if not email:
            print(f"  SKIP  {label} -> no email on the billing contact")
            continue
        if email.split("@")[-1].lower() in INTERNAL_DOMAINS:
            print(f"  SKIP  {label} -> internal address {email}")
            continue
        if DRY_RUN:
            print(f"  WOULD TAG  {label} -> {name} <{email}>")
            newly.append(oid)
            continue
        try:
            if tag_contact(srv, key, lid, email):
                print(f"  TAGGED     {label} -> {name} <{email}>")
                newly.append(oid)
            else:
                print(f"  FAILED     {label} -> tag did not persist for {email}")
                failed.append(oid)
        except urllib.error.HTTPError as e:
            print(f"  FAILED     {label} -> HTTP {e.code} for {email}")
            failed.append(oid)

    print(f"\nalready handled: {skipped}   newly tagged: {len(newly)}   failed: {len(failed)}")
    if not DRY_RUN and newly:
        state["tagged_opportunity_ids"] = sorted(seen | set(newly))
        save_state(state)
        print(f"state written to Supabase ({STATE_NAME})")
    elif DRY_RUN:
        print("dry run, no tags written and no state saved. re-run with --live to apply.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
