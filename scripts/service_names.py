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
