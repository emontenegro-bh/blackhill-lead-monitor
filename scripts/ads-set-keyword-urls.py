#!/usr/bin/env python3
"""Set keyword-level final URLs in Google Ads.

Reads a JSON array from the KEYWORD_URLS env var. Each entry specifies a
keyword by text, campaign name, and ad group name, plus the final URL to set.

Supports DRY_RUN mode (default) which prints planned changes without mutating.

Usage:
  # Via GitHub Actions workflow_dispatch (preferred)
  # Or locally:
  export KEYWORD_URLS='[{"keyword_text":"french drain keller tx","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Drainage Solutions","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/standing-water/"}]'
  export DRY_RUN=true
  python scripts/ads-set-keyword-urls.py
"""

import json, os, sys, io, warnings

warnings.filterwarnings("ignore")

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_client_and_customer():
    """Load Google Ads client. Prefer env vars, fall back to local config."""
    config_file = os.path.expanduser("~/.config/google-ads/config.json")

    if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        ads_config = {
            "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
            "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            "customer_id": os.environ["GOOGLE_ADS_CUSTOMER_ID"],
        }
    else:
        with open(config_file) as f:
            ads_config = json.load(f)

    credentials = {
        "developer_token": ads_config["developer_token"],
        "client_id": ads_config["client_id"],
        "client_secret": ads_config["client_secret"],
        "refresh_token": ads_config["refresh_token"],
        "login_customer_id": ads_config["login_customer_id"],
        "use_proto_plus": True,
        "timeout": 60,
    }

    # Suppress noisy proto-plus warnings on stderr
    stderr_backup = sys.stderr
    sys.stderr = io.StringIO()
    client = GoogleAdsClient.load_from_dict(credentials)
    sys.stderr = stderr_backup

    customer_id = ads_config["customer_id"]
    return client, customer_id


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_keyword(ga_service, customer_id, keyword_text, campaign_name, ad_group_name):
    """Find a keyword criterion by text, campaign name, and ad group name.

    Returns a dict with resource_name, current_urls, status, match_type
    or None if not found.
    """
    # Escape single quotes in names for GAQL
    safe_campaign = campaign_name.replace("'", "\\'")
    safe_ad_group = ad_group_name.replace("'", "\\'")
    safe_keyword = keyword_text.replace("'", "\\'")

    query = f"""
        SELECT
            ad_group_criterion.resource_name,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.final_urls,
            campaign.name,
            ad_group.name
        FROM ad_group_criterion
        WHERE campaign.name = '{safe_campaign}'
            AND ad_group.name = '{safe_ad_group}'
            AND ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status != 'REMOVED'
    """

    response = ga_service.search(customer_id=customer_id, query=query)

    for row in response:
        crit = row.ad_group_criterion
        if crit.keyword.text.lower() == keyword_text.lower():
            return {
                "resource_name": crit.resource_name,
                "text": crit.keyword.text,
                "match_type": crit.keyword.match_type.name,
                "status": crit.status.name,
                "current_urls": list(crit.final_urls) if crit.final_urls else [],
            }

    return None


def set_keyword_url(client, ad_group_criterion_service, customer_id, resource_name, final_url):
    """Mutate a keyword criterion to set its final_url."""
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.final_urls.append(final_url)

    field_mask = field_mask_pb2.FieldMask(paths=["final_urls"])
    client.copy_from(operation.update_mask, field_mask)

    response = ad_group_criterion_service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=[operation]
    )
    return response


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_entry(entry, index):
    """Validate a single JSON entry. Returns list of error strings."""
    errors = []
    required_keys = ["keyword_text", "campaign_name", "ad_group_name", "final_url"]

    for key in required_keys:
        if key not in entry or not entry[key]:
            errors.append(f"  Entry {index}: missing or empty '{key}'")

    if "final_url" in entry and entry["final_url"]:
        url = entry["final_url"]
        if not url.startswith("https://"):
            errors.append(f"  Entry {index}: final_url must start with https:// (got: {url})")

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Parse inputs
    keyword_urls_raw = os.environ.get("KEYWORD_URLS", "")
    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"

    if not keyword_urls_raw:
        print("ERROR: KEYWORD_URLS env var is empty or not set.", file=sys.stderr)
        sys.exit(1)

    try:
        entries = json.loads(keyword_urls_raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: KEYWORD_URLS is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(entries, list) or len(entries) == 0:
        print("ERROR: KEYWORD_URLS must be a non-empty JSON array.", file=sys.stderr)
        sys.exit(1)

    # Validate all entries up front
    all_errors = []
    for i, entry in enumerate(entries):
        all_errors.extend(validate_entry(entry, i))

    if all_errors:
        print("ERROR: Validation failed:", file=sys.stderr)
        for err in all_errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    # Header
    mode = "DRY RUN" if dry_run else "LIVE"
    print("=" * 80)
    print(f"KEYWORD URL UPDATE  [{mode}]")
    print("=" * 80)
    print(f"Entries to process: {len(entries)}")
    print()

    # Authenticate
    client, customer_id = load_client_and_customer()
    ga_service = client.get_service("GoogleAdsService")
    ad_group_criterion_service = client.get_service("AdGroupCriterionService")

    # Process each entry
    updated = 0
    not_found = 0
    errors = 0
    skipped = 0

    for i, entry in enumerate(entries):
        kw_text = entry["keyword_text"]
        campaign = entry["campaign_name"]
        ad_group = entry["ad_group_name"]
        final_url = entry["final_url"]

        label = f"'{kw_text}' in {campaign} > {ad_group}"
        print(f"[{i + 1}/{len(entries)}] {label}")

        # Find keyword
        try:
            result = find_keyword(ga_service, customer_id, kw_text, campaign, ad_group)
        except GoogleAdsException as ex:
            error_msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"  ERROR looking up keyword: {error_msg}")
            errors += 1
            continue

        if result is None:
            print(f"  WARNING: Keyword not found — skipping")
            not_found += 1
            continue

        current_url = result["current_urls"][0] if result["current_urls"] else "(inherits from ad)"
        print(f"  Found: {result['text']} ({result['match_type']}, {result['status']})")
        print(f"  Current URL: {current_url}")
        print(f"  Target URL:  {final_url}")

        # Skip if already set
        if result["current_urls"] and result["current_urls"][0] == final_url:
            print(f"  SKIP: Already set to target URL")
            skipped += 1
            continue

        # Skip non-enabled keywords
        if result["status"] != "ENABLED":
            print(f"  SKIP: Keyword status is {result['status']}")
            skipped += 1
            continue

        if dry_run:
            print(f"  DRY RUN: Would update {current_url} -> {final_url}")
            updated += 1
            continue

        # Mutate
        try:
            set_keyword_url(client, ad_group_criterion_service, customer_id,
                            result["resource_name"], final_url)
            print(f"  UPDATED: {current_url} -> {final_url}")
            updated += 1
        except GoogleAdsException as ex:
            error_msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"  ERROR: {error_msg}")
            errors += 1

        print()

    # Summary
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    action_word = "Would update" if dry_run else "Updated"
    print(f"  {action_word}: {updated}")
    print(f"  Not found:  {not_found}")
    print(f"  Skipped:    {skipped}")
    print(f"  Errors:     {errors}")
    print("=" * 80)

    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
