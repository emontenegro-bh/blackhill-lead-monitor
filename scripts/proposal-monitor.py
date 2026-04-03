#!/usr/bin/env python3
"""
Proposal Monitor — Auto-generate Aspire proposal descriptions from CompanyCam projects.

Polls CompanyCam for new projects that have notes/comments added.
Downloads photos, calls Claude API with vision to generate Description1,
and emails the result to the configured recipient.

Usage:
    python3 proposal-monitor.py                  # Normal run
    python3 proposal-monitor.py --test           # Test API connections
    python3 proposal-monitor.py --dry-run        # Run without sending email or saving state
    python3 proposal-monitor.py --project <id>   # Process a specific project (skip polling)

Config: Environment variables (GitHub Actions) or ~/.config/ files (local).

Env vars:
    COMPANYCAM_TOKEN        CompanyCam API token
    ANTHROPIC_API_KEY       Claude API key
    GMAIL_EMAIL             Gmail sender address
    GMAIL_APP_PASSWORD      Gmail app password
    PROPOSAL_RECIPIENT      Email recipient (default: evelin@blackhilltx.com)
"""

import base64
import json
import math
import os
import signal
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- Timeout ---
def timeout_handler(signum, frame):
    print("ERROR: Script timed out after 120 seconds", file=sys.stderr)
    sys.exit(1)

if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(120)

DRY_RUN = "--dry-run" in sys.argv
STATE_FILE = Path(__file__).parent.parent / "data" / "proposal-state.json"
LOOKBACK_MINUTES = 130
MAX_PROCESSED_IDS = 5000
MAX_PHOTOS_TO_ANALYZE = 6

# Only process projects created by Evelin
CREATOR_ID = os.environ.get("COMPANYCAM_CREATOR_ID", "3069835")

# --- Config ---

def load_config():
    """Load config from env vars (cloud) or local files."""
    config = {
        "companycam_token": os.environ.get("COMPANYCAM_TOKEN", ""),
        "companycam_base_url": "https://api.companycam.com/v2",
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "gmail_email": os.environ.get("GMAIL_EMAIL", ""),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "recipient": os.environ.get("PROPOSAL_RECIPIENT", "evelin@blackhilltx.com"),
    }

    # Fallback to local config files
    if not config["companycam_token"]:
        cc_path = Path.home() / ".config" / "companycam" / "config.json"
        if cc_path.exists():
            cc = json.loads(cc_path.read_text())
            config["companycam_token"] = cc.get("access_token", "")
            config["companycam_base_url"] = cc.get("base_url", config["companycam_base_url"])

    if not config["anthropic_api_key"]:
        # Check common locations
        for p in [Path.home() / ".config" / "anthropic" / "config.json",
                   Path.home() / ".anthropic" / "config.json"]:
            if p.exists():
                config["anthropic_api_key"] = json.loads(p.read_text()).get("api_key", "")
                break
        # Check env file
        if not config["anthropic_api_key"]:
            config["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")

    if not config["gmail_email"]:
        gmail_path = Path.home() / ".config" / "gmail-sender" / "config.json"
        if gmail_path.exists():
            gm = json.loads(gmail_path.read_text())
            config["gmail_email"] = gm.get("email", "")
            config["gmail_app_password"] = gm.get("app_password", "")

    return config


# --- State ---

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": [], "stats": {"total_proposals": 0}, "last_run": None}


def save_state(state):
    if DRY_RUN:
        log("DRY RUN: Would save state")
        return
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    # FIFO cap
    if len(state["processed_ids"]) > MAX_PROCESSED_IDS:
        state["processed_ids"] = state["processed_ids"][-MAX_PROCESSED_IDS:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr)


# --- CompanyCam API ---

def cc_request(path, config):
    req = urllib.request.Request(
        f"{config['companycam_base_url']}{path}",
        headers={"Authorization": f"Bearer {config['companycam_token']}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log(f"CompanyCam API error: {e.code} {e.reason}")
        return None


def fetch_recent_projects(config, lookback_minutes=130):
    """Fetch projects created in the last N minutes.

    CompanyCam API only supports updated_after, so we fetch recently updated
    projects and filter client-side to only those created within the window.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    since_ts = int(since.timestamp())
    projects = cc_request(f"/projects?per_page=50&filter[updated_after]={since_ts}", config)
    if not projects:
        return []
    # Client-side filter: only projects created within the lookback window
    return [p for p in projects if p.get("created_at", 0) >= since_ts]


def get_project_details(project_id, config):
    """Get full project details including notepad."""
    project = cc_request(f"/projects/{project_id}", config)
    if not project:
        return None
    comments = cc_request(f"/projects/{project_id}/comments", config) or []
    labels = cc_request(f"/projects/{project_id}/labels", config) or []
    photos = cc_request(f"/projects/{project_id}/photos?per_page=50", config) or []
    return {
        "project": project,
        "notepad": project.get("notepad"),
        "comments": comments,
        "labels": labels,
        "photos": photos,
    }


def download_photo(url, output_path):
    """Download a photo to disk."""
    try:
        urllib.request.urlretrieve(url, output_path)
        return True
    except Exception as e:
        log(f"Failed to download photo: {e}")
        return False


def get_photo_url(photo, prefer_annotated=True):
    """Extract the best URL from a photo object."""
    uris = photo.get("uris", [])
    if prefer_annotated:
        for u in uris:
            if u.get("type") == "original_annotation":
                return u.get("uri") or u.get("url")
    for u in uris:
        if u.get("type") == "web":
            return u.get("uri") or u.get("url")
    for u in uris:
        if u.get("type") == "original":
            return u.get("uri") or u.get("url")
    return None


# --- Sun Exposure ---

def calculate_sun_exposure(photos):
    """Determine sun exposure from GPS coordinates of photos."""
    coords = []
    for p in photos:
        c = p.get("coordinates") or {}
        lat = c.get("lat")
        lon = c.get("lon")
        if lat and lon:
            coords.append((lat, lon))

    if len(coords) < 2:
        return "Unknown (insufficient GPS data)"

    # Use first and last photo coords to estimate house orientation
    first = coords[0]
    last = coords[-1]
    dlat = last[0] - first[0]
    dlon = last[1] - first[1]

    lat_m = dlat * 111320
    lon_m = dlon * 111320 * math.cos(math.radians(first[0]))

    bearing = math.degrees(math.atan2(lon_m, lat_m))
    if bearing < 0:
        bearing += 360

    if 225 <= bearing <= 315:
        return f"West-facing ({bearing:.0f} degrees). Afternoon sun, hottest exposure in North Texas."
    elif 135 <= bearing < 225:
        return f"South-facing ({bearing:.0f} degrees). Full sun exposure throughout the day."
    elif 45 <= bearing < 135:
        return f"East-facing ({bearing:.0f} degrees). Morning sun, shaded in afternoon."
    else:
        return f"North-facing ({bearing:.0f} degrees). Mostly shade, indirect light."


# --- Mulch Calculation ---

def calculate_mulch(length_ft, width_ft, depth_inches=2):
    """Calculate mulch needed. Returns bags (3 cuft) or yards."""
    volume_cuft = length_ft * width_ft * (depth_inches / 12)
    bags = math.ceil(volume_cuft / 3)
    yards = volume_cuft / 27

    if bags > 13:
        rounded_yards = math.ceil(yards * 2) / 2
        return f"{rounded_yards} cubic yards of black mulch"
    else:
        return f"{bags} bags of black mulch"


# --- Claude API ---

def call_claude(config, system_prompt, user_content):
    """Call Claude API with text and optional images."""
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": config["anthropic_api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=90)
        result = json.loads(resp.read())
        return result["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log(f"Claude API error: {e.code} {body[:200]}")
        return None
    except Exception as e:
        log(f"Claude API error: {e}")
        return None


def build_proposal_prompt(project_data, sun_exposure):
    """Build the system prompt and user content for Claude."""
    system_prompt = """You are an AI proposal writer for Black Hill Landscaping, a landscaping company in Fort Worth, Texas.

Generate a ProposalDescription1 (scope of work only) for an Aspire CRM opportunity.

RULES:
- Start with "Scope of Work" as the first h3 header. No opening paragraph.
- Every bullet ends with a period.
- Use common plant names only. No botanical/scientific names.
- Format plants as: qty - size CommonName on first line, description as sub-bullet.
- Always specify edging as "steel black edging."
- Always include planting soil quantities.
- Mulch is always black. State bags (3 cuft each) for small jobs, cubic yards for large.
- Mulch depth is 2 inches maximum.
- No bold, no em dashes, no costs/pricing, no payment terms.
- No Description2. No terms, warranty, or exclusions.
- Include sun exposure context when recommending plants.
- Plants must be suited to North Texas Zone 8a climate and the specific sun exposure.
- Recommend plant quantities based on mature spread with grow-in spacing (not packed at install).
- Note access constraints (gate width, equipment limitations) when present.
- Include drip irrigation if mentioned in notes.

OUTPUT FORMAT:
Return ONLY the HTML content inside this wrapper (do not include the wrapper itself):
<div style="font-size: 10pt;" id="fontFamilySizeSetting">
<div style="font-family: Arial,sans-serif;" id="fontFamilySetting">
  <!-- your content here using only p, ul, li, h3 tags -->
</div>
</div>

Use only: <h3>, <p>, <ul>, <li> tags. No <strong>, <em>, or inline styles."""

    # Build user content with text and images
    project = project_data["project"]
    addr = project.get("address", {})
    address_str = f"{addr.get('street_address_1', '')}, {addr.get('city', '')}, {addr.get('state', '')} {addr.get('postal_code', '')}"

    text_parts = [
        f"Property: {project.get('name', 'Unknown')}",
        f"Address: {address_str}",
        f"Sun Exposure: {sun_exposure}",
    ]

    if project_data.get("notepad"):
        text_parts.append(f"Field Notes: {project_data['notepad']}")

    if project_data.get("comments"):
        for c in project_data["comments"]:
            text_parts.append(f"Comment: {c.get('content', '')}")

    if project_data.get("labels"):
        label_names = [l.get("name", "") for l in project_data["labels"]]
        text_parts.append(f"Labels: {', '.join(label_names)}")

    text_parts.append(f"Photos: {len(project_data.get('photos', []))} site visit photos attached below.")
    text_parts.append("Generate the ProposalDescription1 HTML based on the notes and photos.")

    user_content = [{"type": "text", "text": "\n".join(text_parts)}]

    return system_prompt, user_content


def add_photos_to_content(user_content, photo_paths):
    """Add base64-encoded photos to the user content."""
    for path in photo_paths:
        try:
            with open(path, "rb") as f:
                img_data = base64.b64encode(f.read()).decode()
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img_data,
                },
            })
        except Exception as e:
            log(f"Failed to encode photo {path}: {e}")
    return user_content


# --- Email ---

def send_email(config, subject, html_body):
    """Send email via Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if DRY_RUN:
        log(f"DRY RUN: Would send email: {subject}")
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Black Hill Assistant <{config['gmail_email']}>"
    msg["To"] = config["recipient"]

    msg.attach(MIMEText("See HTML version for formatted proposal.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(config["gmail_email"], config["gmail_app_password"])
            server.sendmail(config["gmail_email"], config["recipient"], msg.as_string())
        log(f"Email sent: {subject}")
        return True
    except Exception as e:
        log(f"Email failed: {e}")
        return False


def build_email_html(project_data, proposal_html, sun_exposure):
    """Build the full email HTML with header and proposal content."""
    project = project_data["project"]
    addr = project.get("address", {})
    address_str = f"{addr.get('street_address_1', '')}, {addr.get('city', '')}, {addr.get('state', '')} {addr.get('postal_code', '')}"
    name = project.get("name", "Unknown").strip()
    photo_count = len(project_data.get("photos", []))
    cc_url = project.get("project_url", "")

    return f"""\
<html>
<body style="font-family: Arial, sans-serif; font-size: 11pt; color: #333;">

<h2 style="color: #B08A3C;">New Proposal Ready: {name}</h2>

<table style="font-size: 10pt; margin-bottom: 16px;">
<tr><td style="padding-right: 12px;"><strong>Property:</strong></td><td>{name}</td></tr>
<tr><td style="padding-right: 12px;"><strong>Address:</strong></td><td>{address_str}</td></tr>
<tr><td style="padding-right: 12px;"><strong>Source:</strong></td><td><a href="{cc_url}">CompanyCam Project</a></td></tr>
<tr><td style="padding-right: 12px;"><strong>Photos analyzed:</strong></td><td>{photo_count}</td></tr>
<tr><td style="padding-right: 12px;"><strong>Sun exposure:</strong></td><td>{sun_exposure}</td></tr>
</table>

<hr style="border: 1px solid #C9A24D; margin: 16px 0;">

<h3 style="color: #0B0B0B;">Description 1 (Scope of Work)</h3>
<p style="font-size: 9pt; color: #888;">Copy the content below and paste into Aspire ProposalDescription1</p>

<div style="background: #f9f8f5; border: 1px solid #ddd; padding: 16px; border-radius: 4px;">
<div style="font-size: 10pt;" id="fontFamilySizeSetting">
<div style="font-family: Arial,sans-serif;" id="fontFamilySetting">
{proposal_html}
</div>
</div>
</div>

<hr style="border: 1px solid #C9A24D; margin: 16px 0;">
<p style="font-size: 9pt; color: #888;">Generated by Black Hill Assistant from CompanyCam site visit data.</p>

</body>
</html>"""


# --- Service Type Detection ---

def detect_service_type(project_data):
    """Detect service type from notes, labels, and project name."""
    text = " ".join([
        project_data.get("notepad", "") or "",
        project_data["project"].get("name", "") or "",
        " ".join(c.get("content", "") for c in project_data.get("comments", [])),
        " ".join(l.get("name", "") for l in project_data.get("labels", [])),
    ]).lower()

    irrigation_kw = {"sprinkler", "irrigation", "zone", "head", "rotor", "drip", "controller", "valve", "leak", "drain"}
    tree_kw = {"tree", "trim", "removal", "stump", "limb", "prune", "brush", "clear"}
    landscape_kw = {"sod", "plant", "mulch", "bed", "design", "install", "clean up", "landscape"}

    scores = {
        "Irrigation": sum(1 for w in irrigation_kw if w in text),
        "Tree Care": sum(1 for w in tree_kw if w in text),
        "Landscape Install": sum(1 for w in landscape_kw if w in text),
    }

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Landscape Install"


# --- Main Processing ---

def has_notes(project_data):
    """Check if project has any notes or comments."""
    notepad = project_data.get("notepad", "")
    comments = project_data.get("comments", [])
    return bool(notepad and notepad.strip()) or bool(comments)


def process_project(project_id, config):
    """Process a single CompanyCam project into a proposal."""
    log(f"Processing project {project_id}...")

    # 1. Get full project data
    project_data = get_project_details(project_id, config)
    if not project_data:
        log(f"Failed to fetch project {project_id}")
        return False

    if not has_notes(project_data):
        log(f"Project {project_id} has no notes, skipping")
        return False

    # 2. Calculate sun exposure
    sun_exposure = calculate_sun_exposure(project_data["photos"])

    # 3. Download photos for vision analysis
    photos = project_data["photos"][:MAX_PHOTOS_TO_ANALYZE]
    photo_paths = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, photo in enumerate(photos):
            url = get_photo_url(photo, prefer_annotated=True)
            if url:
                path = os.path.join(tmpdir, f"photo_{i}.jpg")
                if download_photo(url, path):
                    photo_paths.append(path)

        log(f"Downloaded {len(photo_paths)} photos for analysis")

        # 4. Build prompt and call Claude
        system_prompt, user_content = build_proposal_prompt(project_data, sun_exposure)
        user_content = add_photos_to_content(user_content, photo_paths)

        proposal_html = call_claude(config, system_prompt, user_content)

    if not proposal_html:
        log(f"Claude API failed for project {project_id}")
        return False

    # 5. Detect service type
    service_type = detect_service_type(project_data)
    name = project_data["project"].get("name", "Unknown").strip()

    # 6. Send email
    email_html = build_email_html(project_data, proposal_html, sun_exposure)
    subject = f"New Proposal Ready: {name} - {service_type}"
    success = send_email(config, subject, email_html)

    if success:
        log(f"Proposal sent for: {name}")
    return success


def process_projects(config, state):
    """Poll for new projects and process them."""
    projects = fetch_recent_projects(config, LOOKBACK_MINUTES)
    log(f"Found {len(projects)} new projects created in last {LOOKBACK_MINUTES} minutes")

    new_count = 0
    for project in projects:
        project_id = str(project.get("id", ""))
        if not project_id:
            continue

        if project_id in state["processed_ids"]:
            continue

        # Only process projects created by the configured creator (Evelin)
        if CREATOR_ID and str(project.get("creator_id", "")) != CREATOR_ID:
            continue

        # Get details to check for notes
        details = get_project_details(project_id, config)
        if not details or not has_notes(details):
            continue

        log(f"New project with notes: {project.get('name', 'Unknown')} ({project_id})")

        if process_project(project_id, config):
            state["processed_ids"].append(project_id)
            state["stats"]["total_proposals"] = state["stats"].get("total_proposals", 0) + 1
            new_count += 1

    log(f"Processed {new_count} new proposals")


# --- CLI ---

def test_connections(config):
    """Test all API connections."""
    print("Testing CompanyCam...", end=" ")
    result = cc_request("/projects?per_page=1", config)
    print("OK" if result is not None else "FAILED")

    print("Testing Claude API...", end=" ")
    result = call_claude(config, "Say OK", [{"type": "text", "text": "Say OK"}])
    print("OK" if result else "FAILED")

    print("Testing Gmail SMTP...", end=" ")
    try:
        import smtplib
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(config["gmail_email"], config["gmail_app_password"])
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")


def main():
    config = load_config()

    if "--test" in sys.argv:
        test_connections(config)
        return

    # Single project mode
    if "--project" in sys.argv:
        idx = sys.argv.index("--project") + 1
        if idx < len(sys.argv):
            success = process_project(sys.argv[idx], config)
            sys.exit(0 if success else 1)

    # Normal polling mode
    state = load_state()
    try:
        process_projects(config, state)
    except Exception:
        traceback.print_exc()
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
