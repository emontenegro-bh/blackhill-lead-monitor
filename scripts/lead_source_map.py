#!/usr/bin/env python3
"""Aspire "Lead Source" picklist values and the WhatConverts mapping, in one place.

WHY THIS MODULE EXISTS

Aspire's Lead Source (contact custom field 34) is a List field. It accepts a value
that is NOT in the picklist, returns 200, and silently stores an empty string -- a
"successful" write that leaves the field blank. Every value sent must therefore
match a picklist option verbatim, and nothing will tell you when one does not.

The "Phone Call " option carries a TRAILING SPACE. That was found and fixed once,
in whatconverts-lead-monitor.py, and the fix was never carried across to
phone-lead-monitor.py -- which went on sending "Phone Call" and blanking the Lead
Source of every phone lead it created. Six contacts had to be repaired by hand on
2026-08-25.

One copy of a fragile string is a gotcha. Two copies is a recurring bug. Import
these names; do not paste the literals into a caller.
"""

# Verified against GET /ContactCustomFieldDefinitions (definition 34) on 2026-08-25.
PHONE_CALL = "Phone Call "          # trailing space is real -- do not strip it
WEBSITE = "Website"
REFERRAL = "Referral"
BING_ORGANIC = "Bing Organic"
BING_ADS = "Bing Ads"
GOOGLE_ORGANIC = "Google Organic"
GOOGLE_ADS = "Google Ads"
GOOGLE_BUSINESS_PROFILE = "Google Business Profile"
POSTCARD_MANIA = "Postcard Mania"

PICKLIST = (PHONE_CALL, WEBSITE, REFERRAL, BING_ORGANIC, BING_ADS, GOOGLE_ORGANIC,
            GOOGLE_ADS, GOOGLE_BUSINESS_PROFILE, POSTCARD_MANIA)

# ContactCustomFieldDefinitionID for the Lead Source picklist (looked up 2026-05-13).
DEFINITION_ID = 34


def from_whatconverts(lead_source, lead_medium):
    """Map a WhatConverts (lead_source, lead_medium) pair to a picklist value.

    WhatConverts knows which tracking number the caller dialled, which makes it the
    only system that can say where a phone call actually came from.

    Returns None for direct/unknown traffic. That is not a source so much as the
    absence of one, and the right fallback depends on the channel: a phone call
    becomes PHONE_CALL, anything else came in through the site. The caller decides,
    because only the caller knows which it was.
    """
    src = (lead_source or "").lower().strip()
    med = (lead_medium or "").lower().strip()
    if src == "gmb":                              # WC tags map-pack calls/clicks 'gmb'
        return GOOGLE_BUSINESS_PROFILE
    if src == "google" and med == "cpc":
        return GOOGLE_ADS
    if src == "google" and med == "organic":
        return GOOGLE_ORGANIC
    if src == "bing" and med == "cpc":
        return BING_ADS
    if src == "bing" and med == "organic":
        return BING_ORGANIC
    if med == "referral":
        return REFERRAL
    return None
