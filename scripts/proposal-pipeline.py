#!/usr/bin/env python3
"""
Proposal Pipeline — Autonomous CompanyCam → Claude → Email proposal system.

Three-stage pipeline:
  Stage 1 (Detect):  Poll CompanyCam for new projects with notes
  Stage 2 (Draft):   Pull photos, select template, generate proposal via Claude
  Stage 3 (Send):    Deliver email and log to state

Usage:
    python3 proposal-pipeline.py                    # Full pipeline run
    python3 proposal-pipeline.py --dry-run          # Run without email/state
    python3 proposal-pipeline.py --project <id>     # Process specific project
    python3 proposal-pipeline.py --test             # Test all API connections
    python3 proposal-pipeline.py --validate <id>    # Draft + print, no email

Config: Environment variables (GitHub Actions) or ~/.config/ files (local).

Env vars:
    COMPANYCAM_TOKEN        CompanyCam API bearer token
    ANTHROPIC_API_KEY       Claude API key
    GMAIL_EMAIL             Gmail sender address
    GMAIL_APP_PASSWORD      Gmail app password (NOT account password)
    PROPOSAL_RECIPIENT      Email recipient (default: evelin@blackhilltx.com)
    COMPANYCAM_CREATOR_ID   Filter by creator (default: 3069835)
"""

import base64
import json
import math
import os
import signal
import smtplib
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR.parent / "data" / "proposal-state.json"
LOOKBACK_MINUTES = 130
MAX_PROCESSED_IDS = 5000
MAX_PHOTOS = 6
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_TIMEOUT = 90
COMPANYCAM_TIMEOUT = 30
SCRIPT_TIMEOUT = 150  # total script timeout
DRY_RUN = "--dry-run" in sys.argv
VALIDATE_MODE = "--validate" in sys.argv

CREATOR_ID = os.environ.get("COMPANYCAM_CREATOR_ID", "3069835")


# ---------------------------------------------------------------------------
# Timeout guard
# ---------------------------------------------------------------------------

def _timeout_handler(signum, frame):
    log("FATAL: Script timed out after %d seconds" % SCRIPT_TIMEOUT)
    sys.exit(1)

if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(SCRIPT_TIMEOUT)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config():
    """Load config from env vars (GitHub Actions) or local ~/.config/ files."""
    config = {
        "companycam_token": os.environ.get("COMPANYCAM_TOKEN", ""),
        "companycam_base_url": "https://api.companycam.com/v2",
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "gmail_email": os.environ.get("GMAIL_EMAIL", ""),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "recipient": os.environ.get("PROPOSAL_RECIPIENT", "evelin@blackhilltx.com"),
    }

    # Fallback: local config files
    if not config["companycam_token"]:
        p = Path.home() / ".config" / "companycam" / "config.json"
        if p.exists():
            cc = json.loads(p.read_text())
            config["companycam_token"] = cc.get("access_token", "")
            config["companycam_base_url"] = cc.get("base_url", config["companycam_base_url"])

    if not config["anthropic_api_key"]:
        for p in [Path.home() / ".config" / "anthropic" / "config.json",
                   Path.home() / ".anthropic" / "config.json"]:
            if p.exists():
                config["anthropic_api_key"] = json.loads(p.read_text()).get("api_key", "")
                break

    if not config["gmail_email"]:
        p = Path.home() / ".config" / "gmail-sender" / "config.json"
        if p.exists():
            gm = json.loads(p.read_text())
            config["gmail_email"] = gm.get("email", "")
            config["gmail_app_password"] = gm.get("app_password", "")

    return config


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("WARNING: Corrupt state file, starting fresh")
    return {"processed_ids": [], "stats": {"total_proposals": 0}, "last_run": None}


def save_state(state):
    if DRY_RUN or VALIDATE_MODE:
        log("SKIP: State not saved (dry-run/validate mode)")
        return
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if len(state["processed_ids"]) > MAX_PROCESSED_IDS:
        state["processed_ids"] = state["processed_ids"][-MAX_PROCESSED_IDS:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ===================================================================
# STAGE 1: DETECT — CompanyCam polling
# ===================================================================

class CompanyCamClient:
    """CompanyCam API v2 client with error handling."""

    def __init__(self, config):
        self.base_url = config["companycam_base_url"].rstrip("/")
        self.token = config["companycam_token"]

    def _request(self, path, timeout=COMPANYCAM_TIMEOUT):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            if e.code == 401:
                log("ERROR: CompanyCam 401 Unauthorized — check COMPANYCAM_TOKEN")
            elif e.code == 403:
                log(f"ERROR: CompanyCam 403 Forbidden on {path} — endpoint may require different permissions")
            elif e.code == 429:
                log("ERROR: CompanyCam 429 Rate Limited — backing off")
            else:
                log(f"ERROR: CompanyCam {e.code} on {path}: {body}")
            return None
        except urllib.error.URLError as e:
            log(f"ERROR: CompanyCam connection failed: {e.reason}")
            return None

    def fetch_recent_projects(self, lookback_minutes=LOOKBACK_MINUTES):
        """Fetch projects created in the last N minutes.

        CompanyCam API updated_after behaves like created_after is unreliable,
        so we fetch by updated_after and filter client-side by created_at.
        """
        since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        since_ts = int(since.timestamp())
        projects = self._request(
            f"/projects?per_page=50&filter[updated_after]={since_ts}"
        )
        if not projects:
            return []
        # Client-side filter: only projects actually created within window
        return [p for p in projects if p.get("created_at", 0) >= since_ts]

    def get_project(self, project_id):
        return self._request(f"/projects/{project_id}")

    def get_comments(self, project_id):
        return self._request(f"/projects/{project_id}/comments") or []

    def get_labels(self, project_id):
        return self._request(f"/projects/{project_id}/labels") or []

    def get_photos(self, project_id):
        return self._request(f"/projects/{project_id}/photos?per_page=50") or []

    def get_full_project(self, project_id):
        """Fetch all data for a project in one call."""
        project = self.get_project(project_id)
        if not project:
            return None
        return {
            "project": project,
            "notepad": project.get("notepad"),
            "comments": self.get_comments(project_id),
            "labels": self.get_labels(project_id),
            "photos": self.get_photos(project_id),
        }


def has_notes(project_data):
    """Check if project has notes or comments worth processing."""
    notepad = project_data.get("notepad", "")
    comments = project_data.get("comments", [])
    return bool(notepad and notepad.strip()) or bool(comments)


def detect_new_projects(cc_client, state):
    """Stage 1: Find new CompanyCam projects with notes."""
    projects = cc_client.fetch_recent_projects(LOOKBACK_MINUTES)
    log(f"DETECT: Found {len(projects)} projects in last {LOOKBACK_MINUTES} min")

    candidates = []
    for project in projects:
        pid = str(project.get("id", ""))
        if not pid or pid in state["processed_ids"]:
            continue
        if CREATOR_ID and str(project.get("creator_id", "")) != CREATOR_ID:
            continue

        details = cc_client.get_full_project(pid)
        if not details or not has_notes(details):
            continue

        name = details["project"].get("name", "Unknown")
        log(f"DETECT: New project with notes: {name} ({pid})")
        candidates.append(details)

    return candidates


# ===================================================================
# STAGE 2: DRAFT — Proposal generation with template selection
# ===================================================================

# --- Service type detection ---

SERVICE_KEYWORDS = {
    "Irrigation": {"sprinkler", "irrigation", "zone", "head", "rotor", "drip",
                   "controller", "valve", "leak", "drain", "nozzle", "pipe"},
    "Tree Care": {"tree", "trim", "removal", "stump", "limb", "prune",
                  "brush", "clear", "canopy", "deadwood"},
    "Landscape Install": {"sod", "plant", "mulch", "bed", "design", "install",
                          "clean up", "landscape", "flower", "shrub", "edging"},
    "Hardscape": {"patio", "walkway", "retaining wall", "stone", "paver",
                  "flagstone", "concrete", "gravel", "fire pit"},
    "Maintenance": {"mow", "weekly", "bi-weekly", "maintenance", "cleanup",
                    "seasonal", "leaf", "weed"},
}


def detect_service_type(project_data):
    """Detect primary service type from notes, labels, and project name."""
    text = " ".join([
        project_data.get("notepad", "") or "",
        project_data["project"].get("name", "") or "",
        " ".join(c.get("content", "") for c in project_data.get("comments", [])),
        " ".join(l.get("name", "") for l in project_data.get("labels", [])),
    ]).lower()

    scores = {}
    for stype, keywords in SERVICE_KEYWORDS.items():
        scores[stype] = sum(1 for w in keywords if w in text)

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Landscape Install"


# --- Sun exposure calculation ---

def calculate_sun_exposure(photos):
    """Estimate sun exposure from GPS coords of first and last photos."""
    coords = []
    for p in photos:
        c = p.get("coordinates") or {}
        lat, lon = c.get("lat"), c.get("lon")
        if lat and lon:
            coords.append((lat, lon))

    if len(coords) < 2:
        return "Unknown (insufficient GPS data)"

    first, last = coords[0], coords[-1]
    dlat = last[0] - first[0]
    dlon = last[1] - first[1]

    lat_m = dlat * 111320
    lon_m = dlon * 111320 * math.cos(math.radians(first[0]))
    bearing = math.degrees(math.atan2(lon_m, lat_m))
    if bearing < 0:
        bearing += 360

    if 225 <= bearing <= 315:
        return f"West-facing ({bearing:.0f} deg). Afternoon sun, hottest exposure in North Texas."
    elif 135 <= bearing < 225:
        return f"South-facing ({bearing:.0f} deg). Full sun throughout the day."
    elif 45 <= bearing < 135:
        return f"East-facing ({bearing:.0f} deg). Morning sun, shaded in afternoon."
    else:
        return f"North-facing ({bearing:.0f} deg). Mostly shade, indirect light."


# --- Photo handling ---

def get_photo_url(photo, prefer_annotated=True):
    """Extract best URL from a photo object. Priority: annotated > web > original."""
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


def download_photos(photos, tmpdir):
    """Download up to MAX_PHOTOS photos and return file paths."""
    paths = []
    for i, photo in enumerate(photos[:MAX_PHOTOS]):
        url = get_photo_url(photo, prefer_annotated=True)
        if not url:
            continue
        path = os.path.join(tmpdir, f"photo_{i}.jpg")
        try:
            urllib.request.urlretrieve(url, path)
            paths.append(path)
        except Exception as e:
            log(f"WARNING: Photo download failed: {e}")
    return paths


def encode_photos_to_content(photo_paths):
    """Encode photos as base64 content blocks for Claude API."""
    blocks = []
    for path in photo_paths:
        try:
            with open(path, "rb") as f:
                img_data = base64.b64encode(f.read()).decode()
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img_data,
                },
            })
        except Exception as e:
            log(f"WARNING: Photo encode failed ({path}): {e}")
    return blocks


# --- Template selection ---

TEMPLATES = {
    "Landscape Install": {
        "focus": "planting design, bed preparation, mulch, edging, and soil amendments",
        "extras": """- Plant quantities based on mature spread with grow-in spacing.
- For planting jobs, use Planters Mix soil at 2-3 inches depth max. Quote cubic yards rounded up to 0.5.
- Default mulch is Hardwood Native Mulch (NOT black). Only use Colored/Black when notes specify "black."
- Mulch depth is 2 inches max. Never quote 3 inches. Only 3CF bags, never 2CF.
- Pallet = 45 bags (3CF). If total bags > 45 (more than 1 pallet), quote cubic yards. If 45 or fewer, quote bags/pallets.
- Edging sections are 10 feet each. Formula: linear feet / 10 = pieces, round up. No cutting.
- Common plant names only. No botanical/scientific names.
- List plants simply: "3 - 3 gallon Texas Sage" — no sub-bullet explanations.""",
    },
    "Irrigation": {
        "focus": "irrigation system installation, repair, or modification",
        "extras": """- Specify head types (rotors, sprays, drip) and zone counts.
- Include controller model or note existing controller.
- Note pipe sizes and materials where visible.
- Specify drip emitter spacing and GPH for beds.
- Include rain sensor or smart controller if discussed.
- Note water pressure or meter size if mentioned.""",
    },
    "Tree Care": {
        "focus": "tree trimming, removal, or stump grinding",
        "extras": """- Identify tree species from photos if possible.
- Note approximate height and canopy diameter.
- Specify trimming type: crown thin, crown raise, deadwood, or full trim.
- For removals, include stump grinding depth (typically 6-8 inches below grade).
- Note haul-off or on-site chip disposal.
- Flag proximity to structures, power lines, or fences.""",
    },
    "Hardscape": {
        "focus": "patio, walkway, retaining wall, or outdoor living installation",
        "extras": """- Specify material (pavers, flagstone, concrete, etc.).
- Include estimated square footage for paved areas.
- Note base preparation (compacted gravel, sand setting bed).
- Include polymeric sand or mortar joints as applicable.
- Specify any drainage considerations (slope, french drain).
- Note border or edge restraint material.
- For decomposed granite: quote total cubic yards. No single default vendor.""",
    },
    "Maintenance": {
        "focus": "recurring lawn care, seasonal cleanup, or property maintenance",
        "extras": """- Specify visit frequency (weekly, bi-weekly, monthly).
- List included services per visit (mow, edge, blow, trim).
- Note any seasonal add-ons (leaf removal, pre-emergent, aeration).
- Include estimated turf area for mowing.
- Note gate access or special access requirements.""",
    },
}

# ---------------------------------------------------------------------------
# Material calculation rules (embedded in system prompt for Claude)
# ---------------------------------------------------------------------------

MATERIAL_RULES = """
MATERIAL CALCULATION RULES:

You MUST parse all LxW measurements from the field notes, calculate square footage for each area,
sum by section (front yard, backyard, etc.), and compute material quantities. Show your math.

CRITICAL: Only include materials that are EXPLICITLY mentioned in the field notes, photo annotations,
or photo descriptions. Read the section headings carefully — they often specify the material
(e.g., "Front yard - Sod St. Augustine" means sod, "Rock bed with edging" means rock and edging).
- Do NOT add materials that are not mentioned. If notes say sod, include sod. If notes say rock, include rock.
- Do NOT add mulch, plants, or other materials unless the notes explicitly call for them.
- If an area has dimensions but truly no material specified anywhere in its heading or bullets,
  flag it in internal notes under "AREAS REQUIRING CLARIFICATION."

MULCH:
- Default: Hardwood Native Mulch. Only use Colored/Black if notes say "black."
- Depth: 2 inches max. Never 3 inches.
- Only 3CF bags. Never 2CF.
- Pallet = 45 bags (3CF). If total bags > 45, quote cubic yards. If 45 or fewer, quote bags/pallets.
- Formula: area(sqft) x (2/12) / 27 = cubic yards, or x (2/12) / 3 = bags.
- Client proposal: state total pallets or cubic yards being installed.

SOD:
- St. Augustine, Celebration Bermuda, Zoysia: 450 sqft per pallet (vendor: King Ranch).
- TifTuf Bermuda: 600 sqft per pallet (full) or 450 sqft (half) (vendor: Prime Sod).
- Waste factor: 5% (fixed). Formula: area(sqft) x 1.05 / pallet_size = exact pallets (show decimal).
- Do NOT round up pallets. Show the exact calculation (e.g., "5.14 pallets").
- Calculate ONE grand total for all sod areas combined. Do NOT break into front/back separately.
- Always use Comanche Compost 1/4" for sod prep soil.
- Client proposal: state total sqft, exact pallet calculation, and variety.

EDGING:
- Steel black edging. 10-foot sections. Formula: linear feet / 10 = pieces, round up.
- Client proposal: say "steel black edging" only. No gauge, no pieces, no feet, no price.

PLANTING SOIL:
- For planting jobs: Planters Mix at 2-3 inches depth max.
- For sod jobs: Comanche Compost 1/4" (depth per project needs).
- Quote cubic yards, rounded up to nearest 0.5.
- Formula: area(sqft) x (depth_inches / 12) / 27 = cubic yards.

DECOMPOSED GRANITE:
- Quote total cubic yards. Client sees yards installed.

ROCK/STONE:
- 1 ton covers ~80-100 sqft at 2-3 inch depth. Round up to 0.5 ton.

PLANTS:
- Calculate qty from mature spread of each species (grow-in spacing, not full coverage).
- Format: qty - size CommonName, with sub-bullet describing why it fits the location.
- Common names only. No botanical/scientific names.
- SINGLE VENDOR PREFERENCE: Source all plants from one vendor when possible. Do not mix vendors
  unless there is a very large, substantial cost difference. Default vendor: Southwest.

OUTPUT MUST INCLUDE TWO SECTIONS:

SECTION 1 — CLIENT-FACING PROPOSAL:
- Conversational, direct, first-person voice. No corporate stiffness.
- State material quantities plainly: "Supply and install 6 pallets of St. Augustine sod."
- No pricing, no dollar amounts, no hourly rates in this section.
- No em dashes. No <strong>, <em>, <b>, or bold. No inline styles.
- Use <ul>/<li> tags for ALL work items. Every work item is a bullet point.
- Use a single <p> tag only for the opening "Scope of Work" line. Everything else is <ul>/<li> bullets.
- Group related bullets under a short label using a <p> tag (e.g., "Cleanup:", "Prep:", "Installation:").
- No <h3>, no wrapper divs.
- MATH ACCURACY: You MUST write Section 2 (internal notes with all calculations) FIRST,
  then write Section 1 (client proposal) using ONLY the numbers from Section 2.
  Output them in the correct order (Section 1 first, then Section 2), but do the math before writing
  the client section. Never guess or estimate numbers in the client section.

SECTION 2 — INTERNAL NOTES (for project manager only):
Start this section with the marker: <!-- INTERNAL NOTES -->
Format ALL internal notes as bullet points using <ul>/<li> tags. No long paragraphs.
Each material gets its own bullet with sub-bullets for details:
- Vendor name
- Unit pricing and total material cost
- Mulch: semi vs dump truck (semi=75CY, dump=40CY), delivery cost
  Delivery: >59CY=$185, >15CY=$235, other bulk=$285, pallets=$335
- Sod: vendor, pallet count, variety
- Edging: total pieces, $19.58/piece (Ewing), total cost
- Soil: vendor (Mayer), product, $/CY
- DG: vendor/source, $/CY or $/TN, delivery
- Plants: vendor, size, unit price (use Southwest as default vendor reference)
- Any flags or verification notes
"""


def build_system_prompt(service_type):
    """Build service-type-specific system prompt for Claude."""
    template = TEMPLATES.get(service_type, TEMPLATES["Landscape Install"])

    return f"""You are writing a proposal for Black Hill Landscaping in Fort Worth, Texas.
This is a {service_type} project focused on {template['focus']}.

WRITING STYLE — match this voice exactly:
- Start EXACTLY with this line every time:
  "Scope of Work - PLEASE SEE ATTACHED REPORT ALONG WITH THIS PROPOSAL"
  Then go straight into the work items. No other opening text.
- Do NOT write in first person. No "I calculated", "I measured", "I've factored in", "my recommendation."
  Exception: very rare cases adding context, but never as standard.
- State what WILL BE DONE, not what you did or thought: "Supply and install 6 pallets of St. Augustine sod."
- Include quantities inline: "Supply and install 5.14 pallets of St. Augustine sod" — not as a separate
  bullet point. Never mention waste factor, calculation method, or "based on measured areas" to the client.
- Do NOT use diagnostic language ("assessment revealed", "inspection identified").
- Do NOT reference "Zone 8a", "North Texas clay soil", or regional context.
- Do NOT add sub-bullet descriptions explaining why a plant fits. Just list it simply.
- Plants format: "3 - 3 gallon Cast Iron plants" or "15 - 1 gallon Autumn Ferns" (simple, no explanation).
- Say "edging" or "black steel edging" naturally.

WORK SEQUENCE — organize ALL proposals in construction order, not by area:
1. Cleanup/Demolition: Remove existing sod, dead bushes, weeds, debris. Always start with a clean base.
2. Prep: Install soil amendments, edging, landscape fabric, grading. Prepare the area for installation.
3. Installation: Install sod, plants, rock, pea gravel, etc. This comes AFTER prep is complete.
4. Finishing: For sod jobs, always end with "Roll sod to ensure proper soil contact for establishment."
Follow the actual order a crew would complete the work on site.

- Include practical notes about site conditions, access, or limitations.
- Include an exclusions or notes section when relevant.
- Every bullet ends with a period.

{template['extras']}

MATERIAL SCOPE RULE:
Only include materials EXPLICITLY mentioned in the field notes or photo annotations.
Read section headings carefully — they specify materials (e.g., "Front yard - Sod St. Augustine").
Do NOT add materials the notes do not call for.

{MATERIAL_RULES}

OUTPUT FORMAT:
Return two sections separated by <!-- INTERNAL NOTES --> marker.

Section 1 (before the marker): Client-facing proposal.
Use <ul>/<li> for ALL work items as bullet points. Only use <p> for the opening scope line and section labels.
Do NOT write long paragraphs. Every work item must be its own <li> bullet.
Do NOT use <h3> tags, <strong>, <em>, inline styles, or the 10pt Arial wrapper divs.
Start directly with the opening "Scope of Work" line in a <p> tag, then bullets.
Keep it clean and simple — Aspire handles the formatting.

Section 2 (after the marker): Internal notes with vendor, pricing, delivery, and material breakdown.
Put all calculations and math here, not in Section 1.
Format as <ul>/<li> bullet points, NOT paragraphs. Each material is a bullet with sub-bullets for details.
This section is for the project manager only.
Use these vendor defaults: Mulch=Organic Recycler, Soil=Mayer, Edging=Ewing ($19.58/pc), Rock/Stone=Clear Fork Materials (CFM), DG=SiteOne, Plants=Southwest."""


def build_user_content(project_data, sun_exposure, photo_paths):
    """Build the user message with text context and photos."""
    project = project_data["project"]
    addr = project.get("address", {})
    address_str = (
        f"{addr.get('street_address_1', '')}, "
        f"{addr.get('city', '')}, "
        f"{addr.get('state', '')} {addr.get('postal_code', '')}"
    )

    text_parts = [
        f"Property: {project.get('name', 'Unknown')}",
        f"Address: {address_str}",
        f"Sun Exposure: {sun_exposure}",
    ]

    if project_data.get("notepad"):
        text_parts.append(f"Field Notes:\n{project_data['notepad']}")

    for c in project_data.get("comments", []):
        content = c.get("content", "")
        if content:
            text_parts.append(f"Comment: {content}")

    if project_data.get("labels"):
        label_names = [l.get("name", "") for l in project_data["labels"]]
        text_parts.append(f"Labels: {', '.join(label_names)}")

    # Collect photo descriptions from API metadata
    photo_descriptions = []
    for i, photo in enumerate(project_data.get("photos", [])[:MAX_PHOTOS]):
        desc = photo.get("description", "")
        if isinstance(desc, str) and desc.strip():
            photo_descriptions.append(f"Photo {i+1} description: {desc.strip()}")
    if photo_descriptions:
        text_parts.append("Photo Descriptions:\n" + "\n".join(photo_descriptions))

    photo_count = len(project_data.get("photos", []))
    text_parts.append(f"Photos: {photo_count} site visit photos attached below.")
    text_parts.append(
        "IMPORTANT: Each photo may contain text annotations, handwritten notes, arrows, "
        "labels, dimensions, or material callouts written directly on the image. "
        "Read every photo carefully for these visual annotations. "
        "The photo descriptions above also contain notes from the project manager. "
        "Do not rely solely on the notepad — the photos and their descriptions are primary data sources."
    )
    text_parts.append(
        "Parse ALL measurements from the notes and photos. Calculate sqft for each area. "
        "Compute material quantities using the rules in your system prompt. "
        "Only include materials explicitly mentioned in the notes. Do not add materials the notes do not call for. "
        "Generate the client-facing proposal HTML followed by <!-- INTERNAL NOTES --> and internal notes."
    )

    content = [{"type": "text", "text": "\n\n".join(text_parts)}]
    content.extend(encode_photos_to_content(photo_paths))
    return content


# --- Claude API client ---

class ClaudeClient:
    """Claude API client with robust error handling."""

    # Known failure modes
    KNOWN_ERRORS = {
        400: "Bad request — check payload structure",
        401: "Invalid ANTHROPIC_API_KEY",
        403: "API key lacks permission for this model",
        404: "Model not found — verify CLAUDE_MODEL is correct",
        429: "Rate limited — too many requests",
        500: "Anthropic server error — transient, will retry next cycle",
        529: "Anthropic overloaded — transient, will retry next cycle",
    }

    def __init__(self, config):
        self.api_key = config["anthropic_api_key"]

    def call(self, system_prompt, user_content, max_tokens=4096):
        """Call Claude API. Returns text or None on failure."""
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT)
            result = json.loads(resp.read())

            # Validate response structure
            if "content" not in result or not result["content"]:
                log("ERROR: Claude returned empty content")
                return None

            text = result["content"][0].get("text", "")
            if not text.strip():
                log("ERROR: Claude returned blank text (silent failure)")
                return None

            # Check for stop reason
            stop = result.get("stop_reason", "")
            if stop == "max_tokens":
                log("WARNING: Claude hit max_tokens — proposal may be truncated")

            return text

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass

            hint = self.KNOWN_ERRORS.get(e.code, "Unknown error")
            log(f"ERROR: Claude API {e.code}: {hint}")

            # Surface model ID errors specifically
            if e.code == 404 or (e.code == 400 and "model" in body.lower()):
                log(f"  Model ID used: {CLAUDE_MODEL}")
                log(f"  Response: {body}")

            return None

        except urllib.error.URLError as e:
            log(f"ERROR: Claude API connection failed: {e.reason}")
            return None
        except Exception as e:
            log(f"ERROR: Claude API unexpected: {e}")
            return None


def draft_proposal(project_data, claude_client):
    """Stage 2: Generate a proposal from project data."""
    project_id = project_data["project"].get("id", "?")
    name = project_data["project"].get("name", "Unknown").strip()
    log(f"DRAFT: Processing {name} ({project_id})")

    # Detect service type and select template
    service_type = detect_service_type(project_data)
    log(f"DRAFT: Service type = {service_type}")

    # Calculate sun exposure
    sun_exposure = calculate_sun_exposure(project_data["photos"])

    # Download and encode photos
    with tempfile.TemporaryDirectory() as tmpdir:
        photo_paths = download_photos(project_data["photos"], tmpdir)
        log(f"DRAFT: Downloaded {len(photo_paths)}/{len(project_data['photos'])} photos")

        # Build prompt with selected template
        system_prompt = build_system_prompt(service_type)
        user_content = build_user_content(project_data, sun_exposure, photo_paths)

        # Call Claude
        proposal_html = claude_client.call(system_prompt, user_content)

    if not proposal_html:
        log(f"DRAFT: Failed for {name}")
        return None

    # Verify the proposal contains expected HTML structure
    if "<h3>" not in proposal_html and "<li>" not in proposal_html:
        log("WARNING: Proposal may not contain valid HTML structure")

    log(f"DRAFT: Generated {len(proposal_html)} chars for {name}")

    return {
        "project_data": project_data,
        "proposal_html": proposal_html,
        "service_type": service_type,
        "sun_exposure": sun_exposure,
    }


# ===================================================================
# STAGE 3: SEND — Email delivery and logging
# ===================================================================

def split_proposal_sections(proposal_html):
    """Split Claude's output into client-facing and internal sections."""
    marker = "<!-- INTERNAL NOTES -->"
    if marker in proposal_html:
        parts = proposal_html.split(marker, 1)
        return parts[0].strip(), parts[1].strip()
    return proposal_html.strip(), ""


def build_email_html(result):
    """Build the full email HTML with client proposal and internal notes."""
    project = result["project_data"]["project"]
    addr = project.get("address", {})
    address_str = (
        f"{addr.get('street_address_1', '')}, "
        f"{addr.get('city', '')}, "
        f"{addr.get('state', '')} {addr.get('postal_code', '')}"
    )
    name = project.get("name", "Unknown").strip()
    photo_count = len(result["project_data"].get("photos", []))
    cc_url = project.get("project_url", "")
    sun = result["sun_exposure"]
    stype = result["service_type"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Split into client and internal sections
    client_html, internal_notes = split_proposal_sections(result["proposal_html"])

    # Build internal notes section HTML (only if present)
    internal_section = ""
    if internal_notes:
        internal_section = f"""
<hr style="border: 2px solid #D32F2F; margin: 24px 0;">

<h3 style="color: #D32F2F;">Internal Notes (DO NOT share with client)</h3>
<div style="background: #FFF3E0; border: 2px solid #FF9800; padding: 16px; border-radius: 4px; font-size: 10pt;">
{internal_notes}
</div>
"""

    return f"""\
<html>
<body style="font-family: Arial, sans-serif; font-size: 11pt; color: #333;">

<h2 style="color: #B08A3C;">Proposal Ready: {name}</h2>

<table style="font-size: 10pt; margin-bottom: 16px;">
<tr><td style="padding-right: 12px;"><strong>Property:</strong></td><td>{name}</td></tr>
<tr><td style="padding-right: 12px;"><strong>Address:</strong></td><td>{address_str}</td></tr>
<tr><td style="padding-right: 12px;"><strong>Service:</strong></td><td>{stype}</td></tr>
<tr><td style="padding-right: 12px;"><strong>Source:</strong></td><td><a href="{cc_url}">CompanyCam Project</a></td></tr>
<tr><td style="padding-right: 12px;"><strong>Photos analyzed:</strong></td><td>{photo_count}</td></tr>
<tr><td style="padding-right: 12px;"><strong>Sun exposure:</strong></td><td>{sun}</td></tr>
</table>

<hr style="border: 1px solid #C9A24D; margin: 16px 0;">

<h3 style="color: #0B0B0B;">Description 1 (Scope of Work)</h3>
<p style="font-size: 9pt; color: #888;">Copy the content below and paste into Aspire ProposalDescription1.</p>

<div style="background: #f9f8f5; border: 1px solid #ddd; padding: 16px; border-radius: 4px;">
<div style="font-size: 10pt;" id="fontFamilySizeSetting">
<div style="font-family: Arial,sans-serif;" id="fontFamilySetting">
{client_html}
</div>
</div>
</div>
{internal_section}
<hr style="border: 1px solid #C9A24D; margin: 16px 0;">
<p style="font-size: 9pt; color: #888;">
  Generated by Black Hill Proposal Pipeline at {ts}.<br>
  Reply to this email to request changes.
</p>

</body>
</html>"""


def send_proposal_email(config, result):
    """Stage 3: Send the proposal email."""
    name = result["project_data"]["project"].get("name", "Unknown").strip()
    stype = result["service_type"]
    subject = f"Proposal Ready: {name} - {stype}"

    if DRY_RUN or VALIDATE_MODE:
        log(f"SEND: [skip] {subject}")
        return True

    html_body = build_email_html(result)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Black Hill Assistant <{config['gmail_email']}>"
    msg["To"] = config["recipient"]
    msg.attach(MIMEText("See HTML version for formatted proposal.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(config["gmail_email"], config["gmail_app_password"])
            server.sendmail(config["gmail_email"], config["recipient"], msg.as_string())
        log(f"SEND: Email delivered — {subject}")
        return True
    except smtplib.SMTPAuthenticationError:
        log("ERROR: Gmail auth failed — check GMAIL_EMAIL and GMAIL_APP_PASSWORD (must be app password)")
        return False
    except smtplib.SMTPRecipientsRefused:
        log(f"ERROR: Recipient refused — {config['recipient']}")
        return False
    except Exception as e:
        log(f"ERROR: Email send failed: {e}")
        return False


def push_to_slack(result):
    """Push proposal draft to Slack #proposals channel via incoming webhook."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    project = result["project_data"]["project"]
    address = project.get("address", {})
    addr_str = f"{address.get('street_address_1', '')}, {address.get('city', '')}"
    service_type = result.get("service_type", "Landscape")
    proposal_text = result.get("proposal_html", result.get("description", ""))

    # Strip HTML tags for Slack display
    import re as _re
    clean_text = _re.sub(r"<[^>]+>", "", proposal_text)[:3000]

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"New Proposal: {service_type}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{addr_str}*\n:sunny: {result.get('sun_exposure', 'Unknown')}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": clean_text[:3000]},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Reply to this thread and tag @Claude to refine this proposal."}],
            },
        ],
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
        log(f"SLACK: Proposal pushed to #proposals — {addr_str}")
    except Exception as e:
        log(f"SLACK: Push failed (non-fatal): {e}")


# ===================================================================
# Orchestration
# ===================================================================

def process_single_project(project_id, config, cc_client, claude_client, state):
    """Run full pipeline for a single project."""
    project_data = cc_client.get_full_project(project_id)
    if not project_data:
        log(f"ERROR: Could not fetch project {project_id}")
        return False

    if not has_notes(project_data):
        log(f"Project {project_id} has no notes — skipping")
        return False

    result = draft_proposal(project_data, claude_client)
    if not result:
        return False

    if VALIDATE_MODE:
        # Print proposal to stdout for review, split into sections
        client_html, internal_notes = split_proposal_sections(result["proposal_html"])
        print(f"\n{'='*60}")
        print(f"PROJECT: {result['project_data']['project'].get('name', '?')}")
        print(f"SERVICE: {result['service_type']}")
        print(f"SUN:     {result['sun_exposure']}")
        print(f"{'='*60}")
        print("CLIENT-FACING PROPOSAL:")
        print(f"{'='*60}")
        print(client_html)
        if internal_notes:
            print(f"\n{'='*60}")
            print("INTERNAL NOTES:")
            print(f"{'='*60}")
            print(internal_notes)
        print(f"{'='*60}\n")
        return True

    if send_proposal_email(config, result):
        push_to_slack(result)
        state["processed_ids"].append(str(project_id))
        state["stats"]["total_proposals"] = state["stats"].get("total_proposals", 0) + 1
        return True

    return False


def run_pipeline(config, state):
    """Full pipeline: detect → draft → send for all new projects."""
    cc_client = CompanyCamClient(config)
    claude_client = ClaudeClient(config)

    candidates = detect_new_projects(cc_client, state)
    if not candidates:
        log("PIPELINE: No new projects to process")
        return

    success_count = 0
    for project_data in candidates:
        pid = str(project_data["project"].get("id", ""))
        result = draft_proposal(project_data, claude_client)
        if not result:
            continue

        if send_proposal_email(config, result):
            push_to_slack(result)
            state["processed_ids"].append(pid)
            state["stats"]["total_proposals"] = state["stats"].get("total_proposals", 0) + 1
            success_count += 1

    log(f"PIPELINE: {success_count}/{len(candidates)} proposals sent")


# ===================================================================
# CLI
# ===================================================================

def test_connections(config):
    """Test all API connections."""
    errors = []

    # CompanyCam
    print("Testing CompanyCam API...", end=" ", flush=True)
    cc = CompanyCamClient(config)
    result = cc._request("/projects?per_page=1")
    if result is not None:
        print("OK")
    else:
        print("FAILED")
        errors.append("CompanyCam")

    # Claude API
    print(f"Testing Claude API ({CLAUDE_MODEL})...", end=" ", flush=True)
    claude = ClaudeClient(config)
    result = claude.call("Say OK", [{"type": "text", "text": "Say OK"}], max_tokens=10)
    if result:
        print("OK")
    else:
        print("FAILED")
        errors.append("Claude")

    # Gmail SMTP
    print("Testing Gmail SMTP...", end=" ", flush=True)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as s:
            s.starttls()
            s.login(config["gmail_email"], config["gmail_app_password"])
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")
        errors.append("Gmail")

    if errors:
        print(f"\nFailed: {', '.join(errors)}")
        sys.exit(1)
    else:
        print("\nAll connections OK")


def main():
    config = load_config()

    if "--test" in sys.argv:
        test_connections(config)
        return

    # Single project mode (--project <id> or --validate <id>)
    for flag in ("--project", "--validate"):
        if flag in sys.argv:
            idx = sys.argv.index(flag) + 1
            if idx < len(sys.argv):
                state = load_state()
                cc_client = CompanyCamClient(config)
                claude_client = ClaudeClient(config)
                success = process_single_project(
                    sys.argv[idx], config, cc_client, claude_client, state
                )
                if not VALIDATE_MODE:
                    save_state(state)
                sys.exit(0 if success else 1)
            else:
                log(f"ERROR: {flag} requires a project ID")
                sys.exit(1)

    # Normal polling mode
    state = load_state()
    try:
        run_pipeline(config, state)
    except Exception:
        traceback.print_exc()
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
