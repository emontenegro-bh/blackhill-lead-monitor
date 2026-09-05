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

# Hosts of ours. When one of these turns up as the "source" of a lead, the site
# has been recorded as referring itself: the visitor's session broke somewhere
# mid-visit and WhatConverts read the previous page as the referrer. It is a
# tracking artefact, not a channel.
#
# 79 leads carried it between 2026-03-01 and 09-05, making it the fifth-largest
# apparent source. Because the medium is literally "referral" it used to fall
# through to REFERRAL below and get written to Aspire as a word-of-mouth
# referral -- so calls that came from the Google Business Profile listing were
# being credited to the referral channel, which both overstates referrals and
# robs GMB. Anything matching here must never reach that rule.
SELF_REFERRAL_HOSTS = (
    "blackhilllandscaping.com",
    "meangreenlawncare.com",
    "blackhilltx.com",
)

# Tracking-number name -> the source that number represents.
#
# For a phone call this outranks everything else WhatConverts reports. The
# number a caller dialled is a fact: each one is published in exactly one place,
# so if the GMB number rang, the call came from the GMB listing, whatever the
# session's referrer happened to say.
#
# "All Traffic" is deliberately absent. It is the catch-all number shown when
# nothing more specific applies, so it identifies no source and must stay
# unattributed rather than be guessed at. 4 of the 11 self-referral calls in the
# sample rang it and they stay unresolved.
TRACKING_NUMBER_SOURCE = {
    "new google my business": GOOGLE_BUSINESS_PROFILE,
    "google mybusiness": GOOGLE_BUSINESS_PROFILE,
    "google cpc": GOOGLE_ADS,
    "google lsa": GOOGLE_ADS,
}


def is_self_referral(lead_source):
    """True if WhatConverts recorded one of our own hosts as the lead's source."""
    src = (lead_source or "").lower().strip()
    return any(host in src for host in SELF_REFERRAL_HOSTS)


def from_tracking_number(phone_name):
    """Map a WhatConverts tracking-number name to a picklist value, or None."""
    return TRACKING_NUMBER_SOURCE.get((phone_name or "").lower().strip())


def from_whatconverts(lead_source, lead_medium, phone_name=None):
    """Map a WhatConverts (lead_source, lead_medium) pair to a picklist value.

    WhatConverts knows which tracking number the caller dialled, which makes it the
    only system that can say where a phone call actually came from. Pass
    phone_name whenever the lead is a call so a self-referral can be recovered
    from the number; it is optional so existing callers keep working.

    Returns None for direct/unknown traffic. That is not a source so much as the
    absence of one, and the right fallback depends on the channel: a phone call
    becomes PHONE_CALL, anything else came in through the site. The caller decides,
    because only the caller knows which it was.
    """
    src = (lead_source or "").lower().strip()
    med = (lead_medium or "").lower().strip()

    # Self-referral first, because the medium is "referral" and the rule at the
    # bottom would otherwise claim it. The tracking number is the one piece of
    # evidence the broken session cannot corrupt, so try it; if it says nothing
    # useful return None and let the caller fall back to WEBSITE / PHONE_CALL.
    # Never REFERRAL -- our own site referring itself is not word of mouth.
    if is_self_referral(src):
        return from_tracking_number(phone_name)

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
