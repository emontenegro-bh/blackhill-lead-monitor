#!/usr/bin/env python3
"""Shared marketing-suppression rules for Aspire -> Mailchimp syncs.

Aspire holds every contact the business touches, including the billing inboxes
and municipal AP desks that exist only to receive invoices. Those addresses are
valid, active and belong to real customers, so no Active/email check filters
them out -- they have to be named. Mailing them is how a landscaping newsletter
ends up in a city's shared accounts-payable queue.

Used by aspire-mailchimp-backfill.py (daily, forward-only) and
aspire-mailchimp-customer-backfill.py (one-time catch-up) so the two agree on
who is marketable.
"""

import re

# Local-parts that are a function, not a person. Matched against the whole
# local-part and against its first token, so "accounting", "accounting2" and
# "accounts.payable" are all caught.
ROLE_LOCAL_PARTS = {
    "accounting", "accounts", "accountspayable", "admin", "ap", "billing",
    "contact", "frontdesk", "hello", "help", "hoa", "hr", "info", "invoice",
    "invoices", "leasing", "maintenance", "management", "manager", "no-reply",
    "noreply", "office", "operations", "orders", "payables", "procurement",
    "property", "purchasing", "receipts", "reception", "sales", "service",
    "support", "team", "vendor", "vendors",
}

# Municipal accounts. Excluded at Evelin's direction 2026-10-02: these are
# procurement and AP desks on contract work, not an audience for seasonal
# marketing.
EXCLUDED_DOMAINS = {
    "arlingtontx.gov",
    "tarrantcountytx.gov",
}

_TOKEN_SPLIT = re.compile(r"[._\-+0-9]")


def normalize_email(raw):
    """Lowercased, stripped address, or None when there is nothing usable."""
    email = (raw or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return None
    return email


def suppression_reason(email):
    """Why this address must not be marketed to, or None when it is fine.

    Returns a short string so callers can log and count the reasons rather than
    silently dropping contacts.
    """
    email = normalize_email(email)
    if not email:
        return "no-email"

    local, _, domain = email.partition("@")
    if domain in EXCLUDED_DOMAINS:
        return "excluded-domain"

    first_token = _TOKEN_SPLIT.split(local)[0]
    if local in ROLE_LOCAL_PARTS or first_token in ROLE_LOCAL_PARTS:
        return "role-inbox"

    return None


def is_marketable(email):
    return suppression_reason(email) is None
