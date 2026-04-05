#!/usr/bin/env python3
"""
Aspire Lead Watcher - Local launchd job.

Watches ~/.config/lead-monitor/leads/ for new lead JSON files and creates
Aspire contacts for any that haven't been processed yet. Runs alongside
the cloud-based lead monitor (GitHub Actions) which handles email parsing,
spam filtering, auto-replies, and notifications.

Usage:
    python3 aspire-lead-watcher.py          # Process new leads
    python3 aspire-lead-watcher.py --status # Show processing stats
    python3 aspire-lead-watcher.py --reprocess <filename>  # Re-run a specific lead
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from glob import glob

LEADS_DIR = os.path.expanduser("~/.config/lead-monitor/leads")
STATE_FILE = os.path.expanduser("~/.config/aspire/watcher-state.json")
LOG_PATH = os.path.expanduser("~/.config/aspire/aspire-watcher.log")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREATE_SCRIPT = os.path.join(SCRIPT_DIR, "aspire-create-contact.py")
ASPIRE_CONFIG = os.path.expanduser("~/.config/aspire/config.json")
HUBSPOT_SYNC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hubspot-sync.py")
HEARTBEAT_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "data", "aspire-watcher-heartbeat.json")
MAX_RETRIES = 5  # Stop retrying after this many failures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("aspire-watcher")


def load_state():
    """Load the watcher state (tracks processed leads)."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "processed": {},  # filename -> {action, timestamp, contact_url}
        "stats": {"created": 0, "exists": 0, "errors": 0, "total_runs": 0},
    }


def save_state(state):
    """Save the watcher state."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_aspire_enabled():
    """Check if Aspire automation is enabled in config."""
    if not os.path.exists(ASPIRE_CONFIG):
        return False
    with open(ASPIRE_CONFIG) as f:
        cfg = json.load(f)
    return cfg.get("enabled", False)


def get_unprocessed_leads(state):
    """Find lead files that haven't been processed yet, including retryable errors."""
    if not os.path.isdir(LEADS_DIR):
        return []

    lead_files = sorted(glob(os.path.join(LEADS_DIR, "*.json")))
    unprocessed = []
    for filepath in lead_files:
        filename = os.path.basename(filepath)
        if filename not in state["processed"]:
            unprocessed.append(filepath)
        elif state["processed"][filename].get("action") == "error":
            retries = state["processed"][filename].get("retries", 0)
            if retries < MAX_RETRIES:
                unprocessed.append(filepath)
    return unprocessed


def update_hubspot_aspire_note(email, aspire_url):
    """Update HubSpot deal with Aspire contact creation status."""
    if not os.path.exists(HUBSPOT_SYNC):
        return
    try:
        args = ["python3", HUBSPOT_SYNC, "--update-aspire", "--email", email]
        if aspire_url:
            args.extend(["--aspire-url", aspire_url])
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.stdout.strip():
            resp = json.loads(result.stdout.strip())
            if resp.get("success"):
                log.info(f"  HubSpot note updated with Aspire status")
            else:
                log.debug(f"  HubSpot update skipped: {resp.get('action', 'unknown')}")
    except Exception as e:
        log.debug(f"  HubSpot note update failed: {e}")


def process_lead(filepath):
    """Call aspire-create-contact.py for a single lead. Returns result dict."""
    with open(filepath) as f:
        lead = json.load(f)

    lead_json = json.dumps(lead)
    try:
        result = subprocess.run(
            ["python3", CREATE_SCRIPT, "--lead-json", lead_json],
            capture_output=True, text=True, timeout=120,
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
        else:
            stderr_tail = (result.stderr or "")[-300:]
            return {"success": False, "action": "error",
                    "message": f"No output. stderr: {stderr_tail}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "action": "error",
                "message": "Timed out (120s)"}
    except Exception as e:
        return {"success": False, "action": "error",
                "message": str(e)[:200]}


def run_watcher():
    """Main watcher loop - process all unprocessed leads."""
    if not is_aspire_enabled():
        log.info("Aspire automation disabled. Exiting.")
        return

    if not os.path.exists(CREATE_SCRIPT):
        log.error(f"Create script not found: {CREATE_SCRIPT}")
        return

    state = load_state()
    state["stats"]["total_runs"] = state["stats"].get("total_runs", 0) + 1

    unprocessed = get_unprocessed_leads(state)
    if not unprocessed:
        log.info("No new leads to process.")
        save_state(state)
        return

    log.info(f"Found {len(unprocessed)} new lead(s) to process.")

    for filepath in unprocessed:
        filename = os.path.basename(filepath)
        try:
            with open(filepath) as f:
                lead = json.load(f)
            name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
            email = lead.get("email", "(no email)")
        except Exception:
            name = filename
            email = ""

        log.info(f"Processing: {name} ({email}) [{filename}]")
        result = process_lead(filepath)

        action = result.get("action", "unknown")
        success = result.get("success", False)
        message = result.get("message", "")

        state["processed"][filename] = {
            "action": action,
            "success": success,
            "message": message,
            "contact_url": result.get("contact_url", ""),
            "timestamp": datetime.now().isoformat(),
        }

        if action == "created":
            state["stats"]["created"] = state["stats"].get("created", 0) + 1
            log.info(f"  Created: {name} ({email})")
            # Update HubSpot deal note with Aspire status
            update_hubspot_aspire_note(email, result.get("contact_url", ""))
        elif action == "exists":
            state["stats"]["exists"] = state["stats"].get("exists", 0) + 1
            log.info(f"  Already exists: {name} ({email})")
            update_hubspot_aspire_note(email, result.get("contact_url", ""))
        else:
            state["stats"]["errors"] = state["stats"].get("errors", 0) + 1
            log.warning(f"  Error: {message}")

    save_state(state)
    log.info(f"Done. Stats: {state['stats']['created']} created, "
             f"{state['stats']['exists']} existed, "
             f"{state['stats']['errors']} errors")


def show_status():
    """Show current watcher stats."""
    state = load_state()
    stats = state.get("stats", {})
    processed = state.get("processed", {})

    print(f"Aspire Lead Watcher Status")
    print(f"{'=' * 40}")
    print(f"Total runs:     {stats.get('total_runs', 0)}")
    print(f"Leads created:  {stats.get('created', 0)}")
    print(f"Already existed: {stats.get('exists', 0)}")
    print(f"Errors:         {stats.get('errors', 0)}")
    print(f"Total processed: {len(processed)}")

    # Count unprocessed
    unprocessed = get_unprocessed_leads(state)
    print(f"Pending:        {len(unprocessed)}")

    if unprocessed:
        print(f"\nPending leads:")
        for fp in unprocessed[:10]:
            print(f"  - {os.path.basename(fp)}")
        if len(unprocessed) > 10:
            print(f"  ... and {len(unprocessed) - 10} more")

    # Recent activity
    recent = sorted(processed.items(), key=lambda x: x[1].get("timestamp", ""),
                    reverse=True)[:5]
    if recent:
        print(f"\nRecent activity:")
        for filename, info in recent:
            ts = info.get("timestamp", "")[:19]
            action = info.get("action", "?")
            msg = info.get("message", "")[:60]
            print(f"  [{ts}] {action}: {msg}")


def reprocess_lead(filename):
    """Re-run a specific lead file."""
    state = load_state()

    # Find the file
    filepath = os.path.join(LEADS_DIR, filename)
    if not os.path.exists(filepath):
        # Try matching partial filename
        matches = glob(os.path.join(LEADS_DIR, f"*{filename}*"))
        if matches:
            filepath = matches[0]
            filename = os.path.basename(filepath)
        else:
            print(f"Lead file not found: {filename}")
            return

    # Remove from processed so it gets re-run
    if filename in state["processed"]:
        del state["processed"][filename]
        save_state(state)

    print(f"Reprocessing: {filename}")
    result = process_lead(filepath)
    print(json.dumps(result, indent=2))

    state = load_state()
    state["processed"][filename] = {
        "action": result.get("action", "unknown"),
        "success": result.get("success", False),
        "message": result.get("message", ""),
        "contact_url": result.get("contact_url", ""),
        "timestamp": datetime.now().isoformat(),
    }
    save_state(state)


def write_heartbeat():
    """Write heartbeat timestamp for remote monitoring."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump({
                "last_run": datetime.now().isoformat(),
                "hostname": os.uname().nodename,
            }, f, indent=2)
    except Exception:
        pass


if __name__ == "__main__":
    if "--status" in sys.argv:
        show_status()
    elif "--reprocess" in sys.argv:
        idx = sys.argv.index("--reprocess")
        if idx + 1 < len(sys.argv):
            reprocess_lead(sys.argv[idx + 1])
        else:
            print("Usage: --reprocess <filename>")
    else:
        run_watcher()
        write_heartbeat()
