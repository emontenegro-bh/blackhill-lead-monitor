#!/usr/bin/env python3
"""Post a single lead to Teams and notify its owner, by hand.

For leads the automatic pipeline lost. Two ways that happens:

  * WhatConverts never captured the form (the tracking script did not run in
    the visitor's browser), so the API monitor has nothing to find and
    lead-monitor.py stands down on web forms by design.
  * The lead was captured but filtered, and the filter was wrong.

Both leave a real customer sitting in the sales@ mailbox with nobody told.
This replays one through the same routing, the same Teams card and the same
owner notification the pipeline would have used, so the result is
indistinguishable from a lead that worked.

Deliberately does NOT touch Aspire, HubSpot or Mailchimp. Those writes are
not idempotent here and a duplicate contact is worse than a manual one; the
assigned owner enters it. Run with --dry-run first, always.

Usage (normally via the post-lead-manual workflow, which holds the secrets):

    python3 scripts/post-lead-manual.py --lead-json '<json>' [--dry-run]

The JSON is a WhatConverts-shaped lead so the existing parser and owner
rules apply unchanged:

    {"lead_type": "Web Form",
     "contact_name": "...", "contact_email_address": "...",
     "contact_phone_number": "...", "city": "...",
     "additional_fields": {"Name": "...", "Email": "...", "Address": "...",
       "City": "...", "Contact No": "...",
       "What Type Of Service Do You Need?": "...",
       "Anything else you would like to share?": "..."}}
"""
import argparse, importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_monitor(dry_run):
    """Import whatconverts-lead-monitor.py as a module.

    Its DRY_RUN is read from sys.argv at import time, so argv has to be set
    before exec_module rather than after.
    """
    sys.argv = ["whatconverts-lead-monitor.py"] + (["--dry-run"] if dry_run else [])
    path = os.path.join(HERE, "whatconverts-lead-monitor.py")
    spec = importlib.util.spec_from_file_location("wc_monitor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead-json", required=True)
    ap.add_argument("--owner", choices=["evelin", "denisse"], default=None,
                    help="Override the routing rules. Needed for commercial bid "
                         "requests: assign_lead_owner only sees the service "
                         "dropdown, and the website form has no Commercial "
                         "Maintenance option, so an RFP round-robins instead of "
                         "going to Evelin.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        lead_data = json.loads(args.lead_json)
    except json.JSONDecodeError as e:
        sys.exit(f"--lead-json is not valid JSON: {e}")

    m = load_monitor(args.dry_run)
    config = m.load_config()

    lead = m._parse_form_lead(lead_data)
    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "(no name)"

    # Round-robin state lives in Supabase. Load and save it so a manual post
    # takes its turn in rotation instead of handing every replayed lead to
    # whoever happens to be next.
    state = m.load_state()
    if args.owner:
        owner_id = (m.OWNER_EVELIN_HUBSPOT_ID if args.owner == "evelin"
                    else m.OWNER_DENISSE_HUBSPOT_ID)
        routed_by = "override"
    else:
        owner_id = m.assign_lead_owner(lead, state)
        routed_by = "routing rules"
    owner_name, owner_email = m.get_owner_info(owner_id)

    print(f"Lead:     {name} | {lead.get('email')} | {lead.get('phone')}")
    print(f"Service:  {lead.get('service_interest')}")
    print(f"Assigned: {owner_name} <{owner_email}>  ({routed_by})")

    if not os.environ.get("TEAMS_WEBHOOK_URL") and not args.dry_run:
        sys.exit("TEAMS_WEBHOOK_URL is not set; refusing to run outside a dry run.")

    m.send_teams_notification(
        config, lead, owner_name=owner_name, owner_email=owner_email,
        hubspot_status="manual replay - not added",
    )
    m.send_owner_notification(
        config, lead, owner_name, owner_email,
        hubspot_status="manual replay - enter into Aspire by hand",
    )

    if not args.dry_run:
        m.save_state(state)
    print("Done." if not args.dry_run else "Dry run: nothing sent.")


if __name__ == "__main__":
    main()
