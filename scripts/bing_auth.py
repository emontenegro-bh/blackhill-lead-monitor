#!/usr/bin/env python3
"""Shared Bing Ads OAuth with a single source of truth for the refresh token.

Microsoft rotates the refresh token on every redemption. Any copy that is not
written back immediately goes stale, which is why the old arrangement kept
breaking: the token lived in two places at once (~/.config/bing-ads/config.json
locally, the BING_ADS_REFRESH_TOKEN GitHub secret in CI) and whichever side
redeemed last silently invalidated the other. A local run left the next
scheduled CI run with a dead token, and vice versa.

This module keeps the token in ONE place, the Supabase `automation_state` row
named `bing-ads-oauth`, read and written by both local runs and CI. The rotated
token is persisted before the function returns, so a later failure cannot lose
it.

Usage, replacing the hand-rolled auth block in any Bing script:

    import bing_auth
    authorization_data = bing_auth.get_authorization_data()

Everything except the refresh token still comes from the usual dual source:
`~/.config/bing-ads/config.json` locally, env vars in CI.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

STATE_NAME = "bing-ads-oauth"
CONFIG_PATH = os.path.expanduser("~/.config/bing-ads/config.json")


def _config():
    """Non-secret Bing settings, local config file first then env (CI wins)."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fh:
            cfg = json.load(fh)
    for key, env in (
        ("developer_token", "BING_ADS_DEVELOPER_TOKEN"),
        ("client_id", "BING_ADS_CLIENT_ID"),
        ("client_secret", "BING_ADS_CLIENT_SECRET"),
        ("customer_id", "BING_ADS_CUSTOMER_ID"),
        ("account_id", "BING_ADS_ACCOUNT_ID"),
        ("redirect_uri", "BING_ADS_REDIRECT_URI"),
        ("refresh_token", "BING_ADS_REFRESH_TOKEN"),
    ):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def read_refresh_token():
    """Return the stored refresh token, or None when the row does not exist."""
    return (db.load_state(STATE_NAME) or {}).get("refresh_token")


def write_refresh_token(token):
    """Persist the refresh token. Called right after every redemption."""
    db.save_state(STATE_NAME, {"refresh_token": token})
    # Mirror locally so an interactive run still works if Supabase is briefly
    # unreachable. The Supabase copy stays authoritative.
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as fh:
                cfg = json.load(fh)
            if cfg.get("refresh_token") != token:
                cfg["refresh_token"] = token
                with open(CONFIG_PATH, "w") as fh:
                    json.dump(cfg, fh, indent=2)
        except (OSError, ValueError):
            pass


def get_authorization_data():
    """Authenticate and return AuthorizationData, persisting the rotated token.

    Falls back to the local config or BING_ADS_REFRESH_TOKEN the first time it
    runs, which seeds Supabase without a separate migration step.
    """
    from bingads.authorization import AuthorizationData, OAuthWebAuthCodeGrant

    cfg = _config()
    stored = read_refresh_token()
    token = stored or cfg.get("refresh_token")
    if not token:
        raise RuntimeError(
            "No Bing refresh token in Supabase or local config. Run "
            "scripts/get-bing-refresh-token.py to mint one."
        )

    auth = OAuthWebAuthCodeGrant(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirection_uri=cfg["redirect_uri"],
    )
    auth.request_oauth_tokens_by_refresh_token(token)

    rotated = auth.oauth_tokens.refresh_token
    if rotated and rotated != token:
        write_refresh_token(rotated)
    elif stored is None:
        write_refresh_token(token)

    return AuthorizationData(
        account_id=int(cfg["account_id"]),
        customer_id=int(cfg["customer_id"]),
        developer_token=cfg["developer_token"],
        authentication=auth,
    )


if __name__ == "__main__":
    data = get_authorization_data()
    print(f"authenticated: account {data.account_id}, customer {data.customer_id}")
    print(f"refresh token stored in Supabase automation_state '{STATE_NAME}'")
