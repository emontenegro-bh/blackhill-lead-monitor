#!/usr/bin/env python3
"""Add keywords to existing ad groups in Google Ads.

Reads a JSON array from KEYWORDS_JSON env var. Each object must have:
  - keyword_text:   The keyword phrase (e.g. "commercial landscaping haslet")
  - match_type:     PHRASE, EXACT, or BROAD
  - campaign_name:  Name of the target campaign
  - ad_group_name:  Name of the target ad group within that campaign
  - final_url:      Landing page URL (must start with https://)

Set DRY_RUN=true to preview changes without mutating.

Auth: prefers GOOGLE_ADS_* env vars, falls back to ~/.config/google-ads/config.json.
"""

import json, os, sys

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
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
elif os.path.exists(config_file):
    with open(config_file) as f:
        ads_config = json.load(f)
else:
    print("ERROR: No Google Ads credentials found. Set GOOGLE_ADS_* env vars "
          "or create ~/.config/google-ads/config.json", file=sys.stderr)
    sys.exit(1)

credentials = {
    "developer_token": ads_config["developer_token"],
    "client_id": ads_config["client_id"],
    "client_secret": ads_config["client_secret"],
    "refresh_token": ads_config["refresh_token"],
    "login_customer_id": ads_config["login_customer_id"],
    "use_proto_plus": True,
    "timeout": 60,
}
client = GoogleAdsClient.load_from_dict(credentials)
customer_id = ads_config["customer_id"]

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
keywords_json = os.environ.get("KEYWORDS_JSON", "")
if not keywords_json:
    print("ERROR: KEYWORDS_JSON env var is empty or not set.", file=sys.stderr)
    sys.exit(1)

try:
    keywords = json.loads(keywords_json)
except json.JSONDecodeError as e:
    print(f"ERROR: Failed to parse KEYWORDS_JSON: {e}", file=sys.stderr)
    sys.exit(1)

if not isinstance(keywords, list) or len(keywords) == 0:
    print("ERROR: KEYWORDS_JSON must be a non-empty JSON array.", file=sys.stderr)
    sys.exit(1)

dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"

VALID_MATCH_TYPES = {"PHRASE", "EXACT", "BROAD"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ga_service = client.get_service("GoogleAdsService")


def find_campaign(name: str) -> str | None:
    """Return campaign resource name by exact name, or None."""
    query = (
        "SELECT campaign.resource_name, campaign.name "
        "FROM campaign "
        f"WHERE campaign.name = '{name}' "
        "AND campaign.status != 'REMOVED' "
        "LIMIT 1"
    )
    rows = ga_service.search(customer_id=customer_id, query=query)
    for row in rows:
        return row.campaign.resource_name
    return None


def find_ad_group(campaign_resource: str, name: str) -> str | None:
    """Return ad group resource name by name within a campaign, or None."""
    query = (
        "SELECT ad_group.resource_name, ad_group.name "
        "FROM ad_group "
        f"WHERE ad_group.campaign = '{campaign_resource}' "
        f"AND ad_group.name = '{name}' "
        "AND ad_group.status != 'REMOVED' "
        "LIMIT 1"
    )
    rows = ga_service.search(customer_id=customer_id, query=query)
    for row in rows:
        return row.ad_group.resource_name
    return None


def get_existing_keywords(ad_group_resource: str) -> set[tuple[str, str]]:
    """Return set of (keyword_text, match_type) already in the ad group."""
    query = (
        "SELECT ad_group_criterion.keyword.text, "
        "       ad_group_criterion.keyword.match_type "
        "FROM ad_group_criterion "
        f"WHERE ad_group_criterion.ad_group = '{ad_group_resource}' "
        "AND ad_group_criterion.type = 'KEYWORD' "
        "AND ad_group_criterion.status != 'REMOVED'"
    )
    existing = set()
    rows = ga_service.search(customer_id=customer_id, query=query)
    for row in rows:
        kw = row.ad_group_criterion.keyword
        existing.add((kw.text.lower(), kw.match_type.name))
    return existing


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
added = 0
skipped = 0
errors = 0

print(f"{'[DRY RUN] ' if dry_run else ''}Processing {len(keywords)} keyword(s)...\n")

for i, entry in enumerate(keywords, 1):
    keyword_text = entry.get("keyword_text", "").strip()
    match_type = entry.get("match_type", "").strip().upper()
    campaign_name = entry.get("campaign_name", "").strip()
    ad_group_name = entry.get("ad_group_name", "").strip()
    final_url = entry.get("final_url", "").strip()

    label = f"[{i}/{len(keywords)}] \"{keyword_text}\" ({match_type}) -> {campaign_name} / {ad_group_name}"

    # --- Validate ---
    if not keyword_text:
        print(f"  ERROR {label}: keyword_text is empty")
        errors += 1
        continue
    if match_type not in VALID_MATCH_TYPES:
        print(f"  ERROR {label}: match_type must be one of {VALID_MATCH_TYPES}, got '{match_type}'")
        errors += 1
        continue
    if not campaign_name:
        print(f"  ERROR {label}: campaign_name is empty")
        errors += 1
        continue
    if not ad_group_name:
        print(f"  ERROR {label}: ad_group_name is empty")
        errors += 1
        continue
    if not final_url.startswith("https://"):
        print(f"  ERROR {label}: final_url must start with https://, got '{final_url}'")
        errors += 1
        continue

    # --- Resolve campaign ---
    try:
        campaign_resource = find_campaign(campaign_name)
    except GoogleAdsException as e:
        print(f"  ERROR {label}: Google Ads API error finding campaign: {e.failure.errors[0].message}")
        errors += 1
        continue

    if not campaign_resource:
        print(f"  WARNING {label}: Campaign '{campaign_name}' not found - skipping")
        errors += 1
        continue

    # --- Resolve ad group ---
    try:
        ad_group_resource = find_ad_group(campaign_resource, ad_group_name)
    except GoogleAdsException as e:
        print(f"  ERROR {label}: Google Ads API error finding ad group: {e.failure.errors[0].message}")
        errors += 1
        continue

    if not ad_group_resource:
        print(f"  WARNING {label}: Ad group '{ad_group_name}' not found in campaign '{campaign_name}' - skipping")
        errors += 1
        continue

    # --- Check for duplicates ---
    try:
        existing = get_existing_keywords(ad_group_resource)
    except GoogleAdsException as e:
        print(f"  ERROR {label}: Google Ads API error checking existing keywords: {e.failure.errors[0].message}")
        errors += 1
        continue

    if (keyword_text.lower(), match_type) in existing:
        print(f"  SKIP {label}: keyword already exists in ad group")
        skipped += 1
        continue

    # --- Add keyword ---
    if dry_run:
        print(f"  DRY RUN {label}: would add with final_url={final_url}")
        added += 1
        continue

    try:
        ad_group_criterion_service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create

        criterion.ad_group = ad_group_resource
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.keyword.text = keyword_text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type].value
        criterion.final_urls.append(final_url)

        response = ad_group_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        resource_name = response.results[0].resource_name
        print(f"  ADDED {label}: {resource_name}")
        added += 1

    except GoogleAdsException as e:
        err_msg = e.failure.errors[0].message if e.failure.errors else str(e)
        print(f"  ERROR {label}: {err_msg}")
        errors += 1

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*60}")
print(f"Summary {'(DRY RUN)' if dry_run else ''}")
print(f"  Added:           {added}")
print(f"  Already existed: {skipped}")
print(f"  Errors:          {errors}")
print(f"{'='*60}")

if errors > 0 and added == 0 and skipped == 0:
    print("\nAll keywords failed. Check campaign/ad group names and credentials.")
    sys.exit(1)
