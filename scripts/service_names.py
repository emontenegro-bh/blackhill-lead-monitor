#!/usr/bin/env python3
"""Service-name handling shared by the lead pipeline.

WHY THIS MODULE EXISTS

The same two judgements are needed in several scripts: "is this string actually
a service?" and "are these two service names the same work?". Getting either
wrong is expensive and silent.

The web form's dropdown offers its own prompt, "Type Of Service You Need", as a
real option with a real value, so a visitor who never opens the dropdown submits
the prompt as their answer. Verified still live on /contact-us/ 2026-10-02.
Eight Mailchimp contacts carry it as a service tag, and it is why a Christmas
lights enquiry routed by round-robin instead of by service.

The web form and the Phone Lead Intake form also name the same work
differently -- "Sprinkler Services" against "Irrigation & Sprinkler Services",
"Drainage Solutions" against "Drainage & Erosion Solutions". Comparing raw
strings would read a returning customer as new business and open a duplicate
deal.

lead_source_map.py already learned this lesson the hard way: one copy of a
fragile string is a gotcha, two copies is a recurring bug. Import these; do not
paste the literals into a caller.
"""

import re

# Strings that occupy the service field without naming a service.
NON_SERVICES = {
    "",
    "general",
    "general inquiry",
    "n/a",
    "none",
    "other",
    "type of service you need",
    "what type of service do you need?",
}

# Words carrying no distinguishing meaning inside a service name.
_STOPWORDS = {"a", "and", "care", "of", "service", "services", "solution",
              "solutions", "the", "your"}


def is_real_service(service):
    """False when the field holds a placeholder, a catch-all, or nothing."""
    return (service or "").strip().lower() not in NON_SERVICES


def normalized_service(lead):
    """The lead's service, or "" when nothing meaningful was selected."""
    service = (lead.get("service_interest") or "").strip()
    return service if is_real_service(service) else ""


def service_key(service):
    """The significant words in a service name."""
    words = re.split(r"[^a-z0-9]+", (service or "").lower())
    return {w for w in words if w and w not in _STOPWORDS}


def same_service(a, b):
    """True when two service names refer to the same line of work."""
    ka, kb = service_key(a), service_key(b)
    return bool(ka and kb and ka & kb)


# One Mailchimp tag per service, whichever form the lead came through.
#
# The web form and the Phone Lead Intake form name the same work differently,
# and slugging each name produced two tags for one service with no contact in
# common: sprinkler-services (120) against irrigation-sprinkler-services (26),
# drainage-solutions (51) against drainage-erosion-solutions (9). Both pairs
# were merged by hand on 2026-10-02; this is what stops them growing back.
#
# Keyed on a significant word, so a new phrasing of the same service lands on
# the existing tag instead of minting another one. Evelin chose the surviving
# names: sprinkler over irrigation, because "sprinkler system" is the house
# wording for residential customers.
CANONICAL_TAG_BY_WORD = {
    "sprinkler": "sprinkler-services",
    "irrigation": "sprinkler-services",
    "drainage": "drainage-solutions",
    "erosion": "drainage-solutions",
    "christmas": "christmas-lights",
    "xmas": "christmas-lights",
    "holiday": "christmas-lights",
    "tree": "tree-care",
    "shrub": "tree-care",
    "commercial": "commercial-maintenance",
    "landscaping": "landscaping",
    "landscape": "landscaping",
    "lawn": "lawn-services",
}


def service_tag(service):
    """The Mailchimp tag for a service name, or "" when there is no service.

    Falls back to a plain slug for a service with no canonical mapping, so a
    genuinely new line of work still gets tagged rather than silently dropped.
    """
    if not is_real_service(service):
        return ""
    for word in service_key(service):
        if word in CANONICAL_TAG_BY_WORD:
            return CANONICAL_TAG_BY_WORD[word]
    slug = re.sub(r"[^a-z0-9]+", "-", (service or "").lower()).strip("-")
    return slug
