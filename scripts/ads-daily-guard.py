#!/usr/bin/env python3
"""Daily Google Ads guard: detect risky account changes within 24 hours.

Checks (all read-only, no AI):
1. Conversion goal changes  - any conversion action flipping primary/secondary
2. Budget / target CPA changes - vs yesterday's snapshot
3. Keyword hygiene - typos from known list, UNSPECIFIED match, enabled duplicates
4. Yesterday's keyword deletes+recreates (performance history loss)

Emails evelin only when something is found. State snapshot committed to repo.
"""
import json, os, sys, signal, smtplib, urllib.request
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timedelta

SCRIPT_TIMEOUT = 300
signal.signal(signal.SIGALRM, lambda s, f: sys.exit("ERROR: guard timed out"))
signal.alarm(SCRIPT_TIMEOUT)

from google.ads.googleads.client import GoogleAdsClient

TO_EMAIL = "evelin@blackhilltx.com"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
STATE_FILE = os.path.join(REPO_ROOT, ".claude", "states", "ads-guard-state.json")
KNOWN_TYPOS = ("istallation", "instalation", "sprinler", "irigation", "landscapping")

if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
    cfg = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
    }
    customer_id = os.environ["GOOGLE_ADS_CUSTOMER_ID"]
else:
    with open(os.path.expanduser("~/.config/google-ads/config.json")) as f:
        local = json.load(f)
    cfg = {k: local[k] for k in ("developer_token", "client_id", "client_secret",
                                 "refresh_token", "login_customer_id")}
    customer_id = local["customer_id"]
cfg["use_proto_plus"] = True
client = GoogleAdsClient.load_from_dict(cfg)
svc = client.get_service("GoogleAdsService")


def rows(q):
    out = []
    for batch in svc.search_stream(customer_id=customer_id, query=q):
        out.extend(batch.results)
    return out


findings = []

# --- Snapshot current settings ---
conv_goals = {}
for r in rows("""SELECT conversion_action.name, conversion_action.primary_for_goal
                 FROM conversion_action WHERE conversion_action.status = 'ENABLED'"""):
    conv_goals[r.conversion_action.name] = bool(r.conversion_action.primary_for_goal)

camp_settings = {}
for r in rows("""SELECT campaign.name, campaign.bidding_strategy_type,
                 campaign.maximize_conversions.target_cpa_micros, campaign_budget.amount_micros
                 FROM campaign WHERE campaign.status = 'ENABLED'"""):
    camp_settings[r.campaign.name] = {
        "bidding": r.campaign.bidding_strategy_type.name,
        "tcpa": round(r.campaign.maximize_conversions.target_cpa_micros / 1e6, 2),
        "budget": round(r.campaign_budget.amount_micros / 1e6, 2),
    }

# --- 1+2. Diff vs prior snapshot ---
prior = {}
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            prior = json.load(f)
    except Exception:
        prior = {}

for name, is_primary in conv_goals.items():
    old = prior.get("conv_goals", {}).get(name)
    if old is not None and old != is_primary:
        findings.append(
            f"CONVERSION GOAL CHANGED: '{name}' flipped from "
            f"{'PRIMARY' if old else 'secondary'} to {'PRIMARY' if is_primary else 'secondary'}. "
            f"This directly changes what smart bidding optimizes for."
        )
for name, cur in camp_settings.items():
    old = prior.get("camp_settings", {}).get(name)
    if not old:
        continue
    if old.get("budget") != cur["budget"]:
        findings.append(f"BUDGET CHANGED: {name}: ${old.get('budget')}/day -> ${cur['budget']}/day")
    if old.get("tcpa") != cur["tcpa"]:
        findings.append(f"TARGET CPA CHANGED: {name}: ${old.get('tcpa')} -> ${cur['tcpa']}")
    if old.get("bidding") != cur["bidding"]:
        findings.append(f"BIDDING STRATEGY CHANGED: {name}: {old.get('bidding')} -> {cur['bidding']}")

# --- 3. Keyword hygiene ---
seen_kw = {}
for r in rows("""SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
                 ad_group.id, ad_group.name, campaign.name
                 FROM ad_group_criterion
                 WHERE ad_group_criterion.type = 'KEYWORD'
                 AND ad_group_criterion.negative = FALSE
                 AND ad_group_criterion.status = 'ENABLED'
                 AND campaign.status = 'ENABLED'"""):
    kw = r.ad_group_criterion.keyword
    text = kw.text.lower()
    if any(t in text for t in KNOWN_TYPOS):
        findings.append(f"TYPO KEYWORD: '{kw.text}' in {r.campaign.name}")
    if kw.match_type.name == "UNSPECIFIED":
        findings.append(f"NO MATCH TYPE: '{kw.text}' in {r.campaign.name}")
    key = (text, kw.match_type.name, r.ad_group.id)
    if key in seen_kw:
        findings.append(f"DUPLICATE KEYWORD: '{kw.text}' ({kw.match_type.name}) twice in ad group "
                        f"'{r.ad_group.name}' ({r.campaign.name})")
    seen_kw[key] = True

# --- 4. Yesterday's delete+recreate pairs ---
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
today = datetime.now().strftime("%Y-%m-%d")
removed, created = set(), {}
for r in rows(f"""SELECT change_event.change_date_time, change_event.user_email,
                  change_event.resource_change_operation, change_event.new_resource, campaign.name
                  FROM change_event
                  WHERE change_event.change_date_time BETWEEN '{yesterday} 00:00:00' AND '{today} 23:59:59'
                  AND change_event.change_resource_type = 'AD_GROUP_CRITERION'
                  ORDER BY change_event.change_date_time LIMIT 500"""):
    ce = r.change_event
    try:
        text = ce.new_resource.ad_group_criterion.keyword.text.strip('"').lower()
    except Exception:
        continue
    if not text:
        continue
    op = ce.resource_change_operation.name
    if op == "REMOVE":
        removed.add(text)
    elif op == "CREATE":
        created[text] = ce.user_email
for text in sorted(removed & set(created)):
    findings.append(
        f"DELETE+RECREATE: keyword '{text}' was removed and re-created by {created[text]} "
        f"(performance history and Quality Score reset)"
    )

# --- Save snapshot (always) ---
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
with open(STATE_FILE, "w") as f:
    json.dump({"conv_goals": conv_goals, "camp_settings": camp_settings,
               "updated": datetime.now().isoformat()}, f, indent=2, sort_keys=True)

if not findings:
    print("Guard clean: no risky changes detected.")
    sys.exit(0)

print(f"{len(findings)} finding(s):")
for fnd in findings:
    print(f"  - {fnd}")

gmail_email = os.environ.get("GMAIL_EMAIL", "")
gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
if not gmail_email or not gmail_password:
    print("No Gmail credentials; findings printed only.")
    sys.exit(0)

body_lines = ["The daily Google Ads guard found account changes that need your attention:\n"]
body_lines += [f"- {fnd}" for fnd in findings]
body_lines.append("\nFull change history: Google Ads -> Tools -> Change history")
msg = MIMEText("\n".join(body_lines))
msg["Subject"] = f"Google Ads Guard: {len(findings)} change(s) need review - {today}"
msg["From"] = formataddr(("Black Hill Assistant", gmail_email))
msg["To"] = TO_EMAIL
with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
    server.starttls()
    server.login(gmail_email, gmail_password)
    server.sendmail(gmail_email, TO_EMAIL, msg.as_string())
print("Alert email sent.")
