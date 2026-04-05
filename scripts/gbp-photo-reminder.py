#!/usr/bin/env python3
"""Weekly photo reminder - emails Evelin + shows macOS notification.

Runs every Sunday at 10am via launchd. Reminds to capture project photos
for the GBP profile throughout the coming week.

Usage:
  python3 scripts/gbp-photo-reminder.py
  python3 scripts/gbp-photo-reminder.py --dry-run
"""

import os, sys, smtplib, subprocess
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

EMAIL_ADDRESS = "evelin@blackhilltx.com"
SENDGRID_SMTP = "smtp.sendgrid.net"
SENDGRID_PORT = 587
SENDGRID_KEY_FILE = os.path.expanduser("~/.config/sendgrid-api-key")
DRY_RUN = "--dry-run" in sys.argv

# Photo types to capture (rotates weekly)
PHOTO_TYPES = [
    {"focus": "Before/After", "tips": "Capture the property BEFORE starting work. Same angle after completion. Good lighting, landscape orientation."},
    {"focus": "Crew at Work", "tips": "Action shots of the crew working. Shows professionalism and process. Avoid faces if crew prefers."},
    {"focus": "Equipment/Process", "tips": "Close-ups of equipment, materials, or technique. Shows the precision behind the work."},
    {"focus": "Completed Projects", "tips": "Final result photos. Multiple angles. Include context (house, street, surrounding area)."},
]

week_num = (datetime.now().isocalendar()[1]) % len(PHOTO_TYPES)
focus = PHOTO_TYPES[week_num]

subject = f"GBP Photo Reminder: {focus['focus']} shots this week"
body = f"""Weekly Photo Reminder for Google Business Profile
{'=' * 50}

This week's focus: {focus['focus']}

Tips:
{focus['tips']}

General reminders:
- Landscape orientation (horizontal)
- Good natural lighting
- Clean, professional framing
- Minimum 1080px wide
- Capture 3-5 photos minimum this week

Upload to: Google Business Profile > Photos
Or send to Evelin for batch upload.

Current GBP photo target: 10+ new photos per month.
"""

# macOS notification (skip in CI/cloud)
if sys.platform == "darwin":
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{focus["focus"]} photos this week" with title "GBP Photo Reminder" subtitle "Black Hill Landscaping"'
        ], check=False)
    except Exception:
        pass

if DRY_RUN:
    print(f"DRY RUN - Would send:\nSubject: {subject}\n\n{body}")
    sys.exit(0)

# Send email
api_key = os.environ.get("SENDGRID_API_KEY", "")
if not api_key:
    if not os.path.exists(SENDGRID_KEY_FILE):
        print(f"No SendGrid API key at {SENDGRID_KEY_FILE}. Notification shown but email skipped.")
        sys.exit(0)
    with open(SENDGRID_KEY_FILE) as f:
        api_key = f.read().strip()

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"] = EMAIL_ADDRESS
msg["To"] = EMAIL_ADDRESS
msg.attach(MIMEText(body, "plain"))

try:
    with smtplib.SMTP(SENDGRID_SMTP, SENDGRID_PORT) as server:
        server.starttls()
        server.login("apikey", api_key)
        server.sendmail(EMAIL_ADDRESS, EMAIL_ADDRESS, msg.as_string())
    print("Photo reminder email sent.")
except Exception as e:
    print(f"Email failed: {e}")
