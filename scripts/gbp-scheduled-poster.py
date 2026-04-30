#!/usr/bin/env python3
"""Post queued GBP posts on their target date.

Reads from data/gbp/scheduled-posts/queue.json, finds any post with
today's date and status 'queued', and posts it via the GBP API.

Usage:
  python3 scripts/gbp-scheduled-poster.py              # Post today's queued posts
  python3 scripts/gbp-scheduled-poster.py --dry-run     # Preview without posting
  python3 scripts/gbp-scheduled-poster.py --list        # Show all queued posts
  python3 scripts/gbp-scheduled-poster.py --date 2026-04-28  # Post for specific date
"""

import json, os, sys, argparse
from datetime import datetime

QUEUE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data", "gbp", "scheduled-posts", "queue.json"
)


def load_queue():
    with open(QUEUE_FILE) as f:
        return json.load(f)


def save_queue(queue):
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)


def list_queue(queue):
    print(f"\nScheduled GBP Posts ({len(queue)} total)")
    print("=" * 70)
    for post in queue:
        status_icon = {"queued": "[ ]", "posted": "[x]", "failed": "[!]"}.get(post["status"], "[?]")
        print(f"  {status_icon} {post['target_date']} ({post['day']}) - {post['theme']}")
        print(f"      {post['text'][:70]}...")
        print()


def post_to_gbp(post, dry_run=False):
    """Post a single entry via gbp-post.py logic."""
    if dry_run:
        print(f"\n--- DRY RUN: Would post on {post['target_date']} ---")
        print(f"  Theme: {post['theme']}")
        print(f"  Type: {post['type']}")
        print(f"  CTA: {post.get('cta', 'None')}")
        print(f"  Text ({len(post['text'])} chars):")
        print(f"  {post['text'][:200]}...")
        return True

    # Import the GBP auth module
    script_dir = os.path.dirname(__file__)
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("gbp_auth", os.path.join(script_dir, "gbp-auth.py"))
    gbp_auth = module_from_spec(spec)
    spec.loader.exec_module(gbp_auth)

    post_body = {
        "languageCode": "en-US",
        "summary": post["text"],
        "topicType": "STANDARD",
    }

    if post.get("cta"):
        cta = {"actionType": post["cta"].upper()}
        if post["cta"].upper() != "CALL":
            cta["url"] = post.get("url", "https://blackhilllandscaping.com")
        post_body["callToAction"] = cta

    if post.get("photo"):
        post_body["media"] = [{
            "mediaFormat": "PHOTO",
            "sourceUrl": post["photo"],
        }]

    try:
        result = gbp_auth.v4_post("localPosts", post_body)
        print(f"  Posted: {result.get('name', 'OK')}")

        # Log the post
        log_dir = os.path.expanduser("~/.config/gbp/post-log")
        os.makedirs(log_dir, exist_ok=True)
        log_entry = {
            "post_name": result.get("name"),
            "type": post["type"],
            "theme": post["theme"],
            "text": post["text"],
            "created_at": datetime.now().isoformat(),
            "scheduled_for": post["target_date"],
            "cta": post.get("cta"),
        }
        log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json")
        with open(log_file, "w") as f:
            json.dump(log_entry, f, indent=2)

        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"  DETAILS: {e.response.text[:500]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Post queued GBP posts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting")
    parser.add_argument("--list", action="store_true", help="Show all queued posts")
    parser.add_argument("--date", help="Override date (YYYY-MM-DD), default is today")
    args = parser.parse_args()

    queue = load_queue()

    if args.list:
        list_queue(queue)
        return

    target_date = args.date or datetime.now().strftime("%Y-%m-%d")
    today_posts = [p for p in queue if p["target_date"] == target_date and p["status"] == "queued"]

    if not today_posts:
        print(f"No queued posts for {target_date}.")
        return

    print(f"\nPosting {len(today_posts)} post(s) for {target_date}:")

    for post in today_posts:
        print(f"\n  {post['theme']} ({post['day']})")
        success = post_to_gbp(post, dry_run=args.dry_run)

        if not args.dry_run:
            post["status"] = "posted" if success else "failed"
            post["posted_at"] = datetime.now().isoformat()
            save_queue(queue)

    if args.dry_run:
        print("\n--- Dry run complete. No posts were published. ---")
    else:
        posted = sum(1 for p in today_posts if p["status"] == "posted")
        print(f"\n{posted}/{len(today_posts)} posts published successfully.")


if __name__ == "__main__":
    main()
