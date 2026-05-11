#!/usr/bin/env python3
"""Weekly SEO audit for blackhilllandscaping.com.

Checks every key page for:
  - Title tags (presence, length, city name)
  - Meta descriptions (presence, length)
  - H1 count (should be exactly 1)
  - Image alt text quality
  - Schema markup (LocalBusiness, FAQPage, AggregateRating)
  - OG tags (image size, presence)
  - Canonical tags

Sends email report + saves markdown + tracks history in JSON.

Schedule: Monday 8 AM CST via GitHub Actions
Manual run: python3 scripts/seo-audit.py
"""

import json, warnings, smtplib, os, sys, signal, re
import urllib.request, urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from datetime import datetime, date
from html.parser import HTMLParser

# --- Global timeout ---
SCRIPT_TIMEOUT = 600
def _timeout_handler(signum, frame):
    print(f"ERROR: Script timed out after {SCRIPT_TIMEOUT}s", file=sys.stderr)
    sys.exit(1)
signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(SCRIPT_TIMEOUT)

warnings.filterwarnings("ignore")

# --- Config ---
SITE = "https://blackhilllandscaping.com"
TO_EMAIL = "evelin@blackhilltx.com"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
REPORT_DIR = os.path.join(REPO_ROOT, ".claude", "reports", "marketing", "seo", "weekly")
HISTORY_FILE = os.path.join(REPORT_DIR, "seo-history.json")

# Pages to audit -- grouped by importance
PAGES = {
    "Homepage": "/",
    "Commercial Services": "/commercial-landscape-services",
    "Residential Services": "/residential-landscaping",
    "About Us": "/about-us",
    "Contact Us": "/contact-us",
    "Areas We Serve": "/areas-we-serve",
    # City hub pages
    "Fort Worth": "/areas-we-serve/fort-worth",
    "Arlington": "/areas-we-serve/arlington",
    "Southlake": "/areas-we-serve/southlake",
    "Colleyville": "/areas-we-serve/colleyville",
    "Keller": "/areas-we-serve/keller",
    "Aledo": "/areas-we-serve/aledo",
    "Haslet": "/areas-we-serve/haslet",
    "Benbrook": "/areas-we-serve/benbrook",
    "White Settlement": "/areas-we-serve/white-settlement",
    "Watauga": "/areas-we-serve/watauga",
    "Weatherford": "/areas-we-serve/weatherford",
    "Westlake": "/areas-we-serve/westlake",
    "Westover Hills": "/areas-we-serve/westover-hills",
    # Key service pages (Fort Worth -- primary ad landing pages)
    "FW Sprinkler Repair": "/areas-we-serve/fort-worth/sprinkler-inspection-and-repairs",
    "FW Sod Installation": "/areas-we-serve/fort-worth/sod-installation",
    "FW Fert & Weed Control": "/areas-we-serve/fort-worth/fertilization-and-weed-control",
    "FW Sprinkler Install": "/areas-we-serve/fort-worth/sprinkler-installation",
    "FW Drainage": "/areas-we-serve/fort-worth/standing-water",
    "FW Landscape Design": "/areas-we-serve/fort-worth/landscaping-services",
    "FW Commercial Maint": "/areas-we-serve/fort-worth/commercial-landscape-maintenance",
    "FW Hardscaping": "/areas-we-serve/fort-worth/hardscaping",
    "Blog": "/blog",
}

# User agent (defined early for sitemap discovery)
UA = "Mozilla/5.0 (compatible; BlackHillSEOAudit/1.0)"

# Slug aliases -- when a page slug changes, check both old and new
# Format: { "old-slug": "new-slug" }
SLUG_ALIASES = {
    "landscape-design-and-installation": "landscaping-services",
}


def discover_pages_from_sitemap():
    """Discover all pages from the sitemap and merge with hardcoded PAGES."""
    discovered = {}
    try:
        sitemap_urls = [
            f"{SITE}/sitemap.xml",
            f"{SITE}/sitemap_index.xml",
            f"{SITE}/wp-sitemap.xml",
        ]
        all_locs = []
        for sitemap_url in sitemap_urls:
            req = urllib.request.Request(sitemap_url, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read().decode()
                # Extract all <loc> URLs
                locs = re.findall(r"<loc>(.*?)</loc>", content)
                # If this is a sitemap index, fetch each child sitemap
                if "<sitemapindex" in content:
                    for child_url in locs:
                        try:
                            req2 = urllib.request.Request(child_url, headers={"User-Agent": UA})
                            with urllib.request.urlopen(req2, timeout=15) as resp2:
                                child_content = resp2.read().decode()
                            child_locs = re.findall(r"<loc>(.*?)</loc>", child_content)
                            all_locs.extend(child_locs)
                        except Exception:
                            pass
                else:
                    all_locs.extend(locs)
                if all_locs:
                    break
            except Exception:
                continue

        for url in all_locs:
            if SITE not in url and "blackhilllandscaping.com" not in url:
                continue
            path = url.replace(SITE, "").replace("https://blackhilllandscaping.com", "").rstrip("/")
            if not path:
                path = "/"
            # Skip non-page URLs (images, feeds, etc.)
            if any(ext in path for ext in [".xml", ".jpg", ".png", ".pdf", ".css", ".js"]):
                continue
            # Skip WordPress taxonomy/template pages (no SEO value, can't control H1)
            skip_patterns = [
                "/blog/tag/", "/blog/category/", "/blog/author/",
                "elementskit_template", "elementor-preview",
                "/wp-content/", "/wp-admin/", "/feed",
            ]
            if any(pat in path.lower() for pat in skip_patterns):
                continue
            # Generate a readable name from the path
            if path == "/":
                continue  # Already in PAGES as "Homepage"
            parts = [p for p in path.strip("/").split("/") if p]
            if len(parts) >= 3:
                # e.g., areas-we-serve/keller/landscaping-services -> "Keller Landscaping Services"
                city = parts[-2].replace("-", " ").title()
                service = parts[-1].replace("-", " ").title()
                name = f"{city} {service}"
            elif len(parts) == 2:
                name = f"{parts[-1].replace('-', ' ').title()}"
            else:
                name = parts[-1].replace("-", " ").title()
            if path not in PAGES.values():
                discovered[name] = path

        print(f"Sitemap discovery: {len(discovered)} additional pages found", file=sys.stderr)
    except Exception as e:
        print(f"Sitemap discovery failed: {e}", file=sys.stderr)
    return discovered


# Merge sitemap pages with hardcoded pages
_sitemap_pages = discover_pages_from_sitemap()
PAGES.update(_sitemap_pages)

# --- HTML Parser ---
class SEOParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta_desc = ""
        self.og_image = ""
        self.og_title = ""
        self.canonical = ""
        self.h1s = []
        self.h2_count = 0
        self.images = []  # list of {"src": ..., "alt": ...}
        self.schemas = []  # list of parsed JSON-LD objects
        self._in_title = False
        self._in_h1 = False
        self._in_script_ld = False
        self._script_buf = ""
        self._h1_buf = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
            self._h1_buf = ""
        elif tag == "h2":
            self.h2_count += 1
        elif tag == "meta":
            name = attrs_dict.get("name", "").lower()
            prop = attrs_dict.get("property", "").lower()
            content = attrs_dict.get("content", "")
            if name == "description":
                self.meta_desc = content
            elif prop == "og:image":
                self.og_image = content
            elif prop == "og:title":
                self.og_title = content
        elif tag == "link":
            rel = attrs_dict.get("rel", "")
            if rel == "canonical":
                self.canonical = attrs_dict.get("href", "")
        elif tag == "img":
            self.images.append({
                "src": attrs_dict.get("src", attrs_dict.get("data-src", "")),
                "alt": attrs_dict.get("alt", ""),
            })
        elif tag == "script":
            stype = attrs_dict.get("type", "")
            if stype == "application/ld+json":
                self._in_script_ld = True
                self._script_buf = ""

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
            self.h1s.append(self._h1_buf.strip())
        elif tag == "script" and self._in_script_ld:
            self._in_script_ld = False
            try:
                data = json.loads(self._script_buf)
                if isinstance(data, list):
                    self.schemas.extend(data)
                else:
                    self.schemas.append(data)
            except json.JSONDecodeError:
                pass

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_h1:
            self._h1_buf += data
        elif self._in_script_ld:
            self._script_buf += data


def fetch_page(url):
    """Fetch a page and return (status_code, html, final_url).
    If the primary URL 404s, tries slug aliases before giving up."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), url
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Try slug aliases
            for old_slug, new_slug in SLUG_ALIASES.items():
                if old_slug in url:
                    alt_url = url.replace(old_slug, new_slug)
                    try:
                        req2 = urllib.request.Request(alt_url, headers={"User-Agent": UA})
                        with urllib.request.urlopen(req2, timeout=15) as resp2:
                            return resp2.status, resp2.read().decode("utf-8", errors="replace"), alt_url
                    except Exception:
                        pass
                elif new_slug in url:
                    alt_url = url.replace(new_slug, old_slug)
                    try:
                        req2 = urllib.request.Request(alt_url, headers={"User-Agent": UA})
                        with urllib.request.urlopen(req2, timeout=15) as resp2:
                            return resp2.status, resp2.read().decode("utf-8", errors="replace"), alt_url
                    except Exception:
                        pass
        return e.code, None, url
    except Exception as e:
        return 0, None, url


def extract_schema_types(schemas):
    """Get a flat set of @type values from JSON-LD schemas."""
    types = set()
    for s in schemas:
        t = s.get("@type", "")
        if isinstance(t, list):
            types.update(t)
        elif t:
            types.add(t)
        # Check @graph
        for item in s.get("@graph", []):
            t2 = item.get("@type", "")
            if isinstance(t2, list):
                types.update(t2)
            elif t2:
                types.add(t2)
    return types


def classify_alt_text(alt, src=""):
    """Classify image alt text quality: good, poor, or missing."""
    if not alt or not alt.strip():
        return "missing"
    alt_lower = alt.strip().lower()
    src_lower = src.lower() if src else ""
    # Skip logos and icons -- these are fine with short/branded alt text
    if "logo" in alt_lower or "logo" in src_lower:
        return "good"
    if "icon" in src_lower:
        return "good"
    # Raw filename patterns (but not logos, already handled above)
    if alt_lower.endswith((".webp", ".jpg", ".jpeg", ".png", ".gif", ".svg")):
        return "poor"
    if "cropped" in alt_lower:
        return "poor"
    if alt_lower in ("submenu image", "image", "photo", "img", "icon"):
        return "poor"
    # Single generic word (skip brand names and short proper nouns)
    if len(alt_lower.split()) <= 1 and len(alt_lower) < 5:
        return "poor"
    return "good"


def audit_page(name, path):
    """Audit a single page and return a results dict."""
    url = f"{SITE}{path}"
    status, html, final_url = fetch_page(url)
    if final_url != url:
        path = final_url.replace(SITE, "")

    result = {
        "name": name,
        "path": path,
        "url": url,
        "status": status,
        "exists": status == 200,
        "title": "",
        "title_length": 0,
        "meta_desc": "",
        "meta_desc_length": 0,
        "h1_count": 0,
        "h1s": [],
        "h2_count": 0,
        "total_images": 0,
        "images_good_alt": 0,
        "images_poor_alt": 0,
        "images_missing_alt": 0,
        "schema_types": [],
        "has_local_business": False,
        "has_faq_page": False,
        "has_aggregate_rating": False,
        "og_image": "",
        "canonical": "",
        "issues": [],
    }

    if not html:
        if status == 404:
            result["issues"].append("PAGE MISSING (404)")
        elif status == 0:
            result["issues"].append("PAGE UNREACHABLE")
        else:
            result["issues"].append(f"HTTP {status}")
        return result

    parser = SEOParser()
    try:
        parser.feed(html)
    except Exception:
        result["issues"].append("HTML parse error")
        return result

    # Title
    result["title"] = parser.title.strip()
    result["title_length"] = len(result["title"])
    if not result["title"]:
        result["issues"].append("Missing title tag")
    elif result["title_length"] > 60:
        result["issues"].append(f"Title too long ({result['title_length']} chars)")
    elif result["title_length"] < 30:
        result["issues"].append(f"Title too short ({result['title_length']} chars)")

    # Check if city name is in title for city pages
    city_pages = {
        "Fort Worth": "fort worth", "Arlington": "arlington", "Southlake": "southlake",
        "Colleyville": "colleyville", "Keller": "keller", "Aledo": "aledo",
        "Haslet": "haslet", "Benbrook": "benbrook", "White Settlement": "white settlement",
        "Watauga": "watauga", "Weatherford": "weatherford", "Westlake": "westlake",
        "Westover Hills": "westover hills",
    }
    if name in city_pages and city_pages[name] not in result["title"].lower():
        result["issues"].append(f"City name '{name}' missing from title tag")

    # Meta description
    result["meta_desc"] = parser.meta_desc
    result["meta_desc_length"] = len(parser.meta_desc)
    if not parser.meta_desc:
        result["issues"].append("Missing meta description")
    elif result["meta_desc_length"] > 160:
        result["issues"].append(f"Meta description too long ({result['meta_desc_length']} chars)")
    elif result["meta_desc_length"] < 70:
        result["issues"].append(f"Meta description too short ({result['meta_desc_length']} chars)")

    # H1
    result["h1_count"] = len(parser.h1s)
    result["h1s"] = parser.h1s
    result["h2_count"] = parser.h2_count
    if result["h1_count"] == 0:
        result["issues"].append("Missing H1 tag")
    elif result["h1_count"] > 1:
        result["issues"].append(f"Multiple H1 tags ({result['h1_count']})")

    # Images
    for img in parser.images:
        quality = classify_alt_text(img["alt"], img.get("src", ""))
        if quality == "good":
            result["images_good_alt"] += 1
        elif quality == "poor":
            result["images_poor_alt"] += 1
        else:
            result["images_missing_alt"] += 1
    result["total_images"] = len(parser.images)
    bad_alt = result["images_poor_alt"] + result["images_missing_alt"]
    if bad_alt > 0 and result["total_images"] > 0:
        pct = bad_alt / result["total_images"] * 100
        if pct > 50:
            result["issues"].append(f"{bad_alt}/{result['total_images']} images have poor/missing alt text ({pct:.0f}%)")
        elif bad_alt > 2:
            result["issues"].append(f"{bad_alt} images have poor/missing alt text")

    # Schema
    schema_types = extract_schema_types(parser.schemas)
    result["schema_types"] = sorted(schema_types)
    result["has_local_business"] = any(
        t in schema_types for t in ("LocalBusiness", "LandscapingBusiness")
    )
    result["has_faq_page"] = "FAQPage" in schema_types
    result["has_aggregate_rating"] = "AggregateRating" in schema_types
    # Also check nested
    for s in parser.schemas:
        if "aggregateRating" in json.dumps(s):
            result["has_aggregate_rating"] = True
        for item in s.get("@graph", []):
            t = item.get("@type", "")
            if t in ("LocalBusiness", "LandscapingBusiness"):
                result["has_local_business"] = True
            if "aggregateRating" in json.dumps(item):
                result["has_aggregate_rating"] = True

    # OG image
    result["og_image"] = parser.og_image
    if parser.og_image and "150x150" in parser.og_image:
        result["issues"].append("OG image is a small thumbnail (150x150)")
    elif not parser.og_image:
        result["issues"].append("Missing OG image")

    # Canonical
    result["canonical"] = parser.canonical

    return result


# ============================================================
# RUN AUDIT
# ============================================================
print(f"SEO Audit starting: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"Auditing {len(PAGES)} pages on {SITE}\n")

results = {}
for name, path in PAGES.items():
    print(f"  Checking {name} ({path})...", end=" ")
    result = audit_page(name, path)
    results[name] = result
    status_str = "OK" if result["exists"] else f"HTTP {result['status']}"
    issue_count = len(result["issues"])
    print(f"{status_str} - {issue_count} issue{'s' if issue_count != 1 else ''}")

# ============================================================
# SCORE
# ============================================================
total_pages = len(results)
pages_live = sum(1 for r in results.values() if r["exists"])
pages_missing = total_pages - pages_live

total_images = sum(r["total_images"] for r in results.values())
good_alt = sum(r["images_good_alt"] for r in results.values())
poor_alt = sum(r["images_poor_alt"] for r in results.values())
missing_alt = sum(r["images_missing_alt"] for r in results.values())
alt_score = (good_alt / total_images * 100) if total_images > 0 else 0

pages_with_local_biz = sum(1 for r in results.values() if r["has_local_business"] and r["exists"])
pages_with_faq = sum(1 for r in results.values() if r["has_faq_page"] and r["exists"])
pages_with_rating = sum(1 for r in results.values() if r["has_aggregate_rating"] and r["exists"])

title_issues = sum(1 for r in results.values() if r["exists"] and any("title" in i.lower() or "Title" in i for i in r["issues"]))
h1_issues = sum(1 for r in results.values() if r["exists"] and any("H1" in i for i in r["issues"]))
meta_issues = sum(1 for r in results.values() if r["exists"] and any("meta" in i.lower() for i in r["issues"]))

total_issues = sum(len(r["issues"]) for r in results.values())

# Overall score (percentage-based, scales with page count)
# Each component is 0-100%, then weighted
pct_live = (pages_live / total_pages * 100) if total_pages > 0 else 0         # % pages returning 200
pct_title_ok = ((pages_live - title_issues) / pages_live * 100) if pages_live > 0 else 0
pct_h1_ok = ((pages_live - h1_issues) / pages_live * 100) if pages_live > 0 else 0
pct_meta_ok = ((pages_live - meta_issues) / pages_live * 100) if pages_live > 0 else 0
pct_schema = (pages_with_local_biz / pages_live * 100) if pages_live > 0 else 0
pct_faq = (pages_with_faq / pages_live * 100) if pages_live > 0 else 0

score = (
    pct_live * 0.15 +           # 15% weight: pages exist
    pct_title_ok * 0.20 +       # 20% weight: title tags
    pct_h1_ok * 0.15 +          # 15% weight: H1 tags
    pct_meta_ok * 0.15 +        # 15% weight: meta descriptions
    alt_score * 0.15 +           # 15% weight: alt text quality
    pct_schema * 0.10 +          # 10% weight: LocalBusiness schema
    pct_faq * 0.10               # 10% weight: FAQ schema
)
score = max(0, min(100, round(score)))

# ============================================================
# GENERATE REPORT
# ============================================================
today = date.today()
today_fmt = today.strftime("%B %d, %Y")

md = []
md.append("# Black Hill Landscaping - Weekly SEO Audit")
md.append(f"**Date**: {today_fmt}")
md.append(f"**Site**: {SITE}")
md.append("")

md.append("## Scorecard")
md.append(f"| Metric | Value |")
md.append(f"|--------|-------|")
md.append(f"| Overall Score | **{score}/100** |")
md.append(f"| Pages Audited | {total_pages} |")
md.append(f"| Pages Live | {pages_live} |")
md.append(f"| Pages Missing (404) | {pages_missing} |")
md.append(f"| Total Issues | {total_issues} |")
md.append(f"| Image Alt Text Quality | {alt_score:.0f}% good ({good_alt}/{total_images}) |")
md.append(f"| Pages with LocalBusiness Schema | {pages_with_local_biz}/{pages_live} |")
md.append(f"| Pages with FAQPage Schema | {pages_with_faq}/{pages_live} |")
md.append(f"| Pages with AggregateRating | {pages_with_rating}/{pages_live} |")
md.append("")

# ============================================================
# BUILD ACTION PLAN (prioritized, specific instructions)
# ============================================================
actions = []  # (priority, category, action_text)

# --- Missing city pages (highest impact for local SEO) ---
missing_pages = [r for r in results.values() if not r["exists"]]
# Priority cities by business value
priority_cities = ["Arlington", "White Settlement", "Watauga"]
for r in missing_pages:
    if r["name"] in priority_cities:
        actions.append((1, "Build Page",
            f"**Build {r['name']} city page** at `{r['path']}` with 13 service subpages. "
            f"{'You have a major city contract here.' if r['name'] == 'Arlington' else ''}"
            f"{'This is your physical address city.' if r['name'] == 'White Settlement' else ''}"
            f"{'You have active park contracts here.' if r['name'] == 'Watauga' else ''}"
        ))
    elif r["name"].startswith("FW "):
        actions.append((2, "Build Page",
            f"**Build {r['name']} service page** at `{r['path']}`. This is a Fort Worth service "
            f"subpage that should exist for keyword targeting."
        ))
    else:
        actions.append((3, "Build Page",
            f"**Build {r['name']} city page** at `{r['path']}`. Listed on Areas We Serve "
            f"hub page but no actual page exists."
        ))

# --- Schema markup gaps ---
homepage_result = results.get("Homepage")
if homepage_result and homepage_result["exists"] and not homepage_result["has_local_business"]:
    actions.append((1, "Schema",
        "**Add LocalBusiness schema to homepage.** The homepage is the most crawled page and is "
        "missing the most important local SEO schema type. Paste JSON-LD into the `<head>` section "
        "via Yoast or a header script plugin."
    ))

pages_needing_faq = [name for name, r in results.items()
                     if r["exists"] and not r["has_faq_page"]
                     and name not in ("Blog", "About Us", "Contact Us", "Areas We Serve")]
if pages_needing_faq:
    actions.append((2, "Schema",
        f"**Add FAQPage schema** to {len(pages_needing_faq)} pages: "
        f"{', '.join(pages_needing_faq[:5])}{'...' if len(pages_needing_faq) > 5 else ''}. "
        f"These pages have FAQ sections but no structured data markup. Adding FAQPage schema "
        f"enables rich FAQ results in Google search."
    ))

pages_without_rating = [name for name, r in results.items()
                        if r["exists"] and not r["has_aggregate_rating"]
                        and name in ("Homepage", "Commercial Services", "Residential Services")]
if pages_without_rating:
    actions.append((2, "Schema",
        f"**Add AggregateRating schema** to: {', '.join(pages_without_rating)}. "
        f"You claim 70+ five-star reviews but these key pages have no rating markup. "
        f"Adding this can show star ratings in search results."
    ))

# --- Title tag issues ---
for name, r in results.items():
    if not r["exists"]:
        continue
    for issue in r["issues"]:
        if "City name" in issue and "missing from title" in issue:
            actions.append((1, "Title Tag",
                f"**Add city name to {name} title tag.** Currently: \"{r['title']}\". "
                f"Add \"{name}\" to the beginning of the title in Yoast. This directly impacts "
                f"both organic ranking and Google Ads Quality Score for this landing page."
            ))
        elif "Title too long" in issue:
            actions.append((3, "Title Tag",
                f"**Shorten {name} title tag** ({r['title_length']} chars, max 60). "
                f"Current: \"{r['title'][:65]}...\". Trim to under 60 characters so it doesn't "
                f"get cut off in search results."
            ))
        elif "Missing title" in issue:
            actions.append((1, "Title Tag",
                f"**Add title tag to {name}.** This page has no title tag at all. "
                f"Add a descriptive title with your primary keyword and city name in Yoast."
            ))

# --- H1 issues ---
for name, r in results.items():
    if not r["exists"]:
        continue
    for issue in r["issues"]:
        if "Multiple H1" in issue:
            actions.append((2, "Heading",
                f"**Fix duplicate H1 on {name}.** Has {r['h1_count']} H1 tags "
                f"({', '.join(repr(h) for h in r['h1s'][:3])}). Change extras to H2 in Elementor. "
                f"Google prefers a single H1 per page."
            ))
        elif "Missing H1" in issue:
            actions.append((1, "Heading",
                f"**Add H1 tag to {name}.** This page has no H1 heading. Add one with your "
                f"primary keyword for the page."
            ))

# --- Meta description issues ---
for name, r in results.items():
    if not r["exists"]:
        continue
    for issue in r["issues"]:
        if "Missing meta description" in issue:
            actions.append((2, "Meta",
                f"**Add meta description to {name}.** Write a 120-155 character description "
                f"with your target keyword, city name, and a call to action. Set in Yoast."
            ))
        elif "Meta description too long" in issue:
            actions.append((3, "Meta",
                f"**Shorten {name} meta description** ({r['meta_desc_length']} chars, max 160). "
                f"Trim so it doesn't get cut off in search results."
            ))

# --- Image alt text ---
worst_alt_pages = [(name, r) for name, r in results.items()
                   if r["exists"] and (r["images_poor_alt"] + r["images_missing_alt"]) > 3]
worst_alt_pages.sort(key=lambda x: -(x[1]["images_poor_alt"] + x[1]["images_missing_alt"]))
# Only flag the worst offenders (top 5)
for name, r in worst_alt_pages[:5]:
    bad = r["images_poor_alt"] + r["images_missing_alt"]
    actions.append((2, "Alt Text",
        f"**Fix {bad} images on {name}** with poor/missing alt text. "
        f"Use descriptive text that includes the service keyword and city name "
        f"(e.g., \"commercial lawn maintenance crew on Fort Worth office park\"). "
        f"Page: `{r['path']}`"
    ))

# --- OG image ---
if homepage_result and any("thumbnail" in i.lower() or "150x150" in i for i in homepage_result.get("issues", [])):
    actions.append((2, "OG Image",
        "**Replace homepage OG image.** Currently set to a 150x150 blog thumbnail. "
        "Upload a 1200x630 branded image in Yoast SEO > Social settings. This is what "
        "shows when your site is shared on Facebook, LinkedIn, or iMessage."
    ))
for name, r in results.items():
    if r["exists"] and any("Missing OG image" in i for i in r.get("issues", [])):
        actions.append((3, "OG Image",
            f"**Add OG image to {name}.** No Open Graph image set. Upload a relevant "
            f"1200x630 image in Yoast for this page."
        ))

# Sort by priority
actions.sort(key=lambda x: x[0])

# Generate action plan section
md.append("## What to Fix This Week")
md.append("")
if not actions:
    md.append("No issues found. Site is in good shape.")
else:
    # Show top 10 actions
    shown = 0
    current_priority = None
    priority_labels = {1: "HIGH IMPACT", 2: "MEDIUM IMPACT", 3: "LOW IMPACT"}
    for priority, category, text in actions:
        if shown >= 10:
            break
        if priority != current_priority:
            current_priority = priority
            md.append(f"\n**{priority_labels.get(priority, 'OTHER')}**\n")
        shown += 1
        md.append(f"{shown}. [{category}] {text}")

    remaining = len(actions) - shown
    if remaining > 0:
        md.append(f"\n*Plus {remaining} more lower-priority items (see full report in repo)*")
md.append("")

# Full action list for the markdown file (not email)
if len(actions) > 10:
    md.append("<details>")
    md.append("<summary>All action items ({} total)</summary>\n".format(len(actions)))
    for i, (priority, category, text) in enumerate(actions, 1):
        label = priority_labels.get(priority, "OTHER")
        md.append(f"{i}. **[{label}]** [{category}] {text}")
    md.append("\n</details>")
    md.append("")

# Missing pages section
if missing_pages:
    md.append("## Missing Pages (404)")
    for r in missing_pages:
        md.append(f"- **{r['name']}**: `{r['path']}`")
    md.append("")

# Issues by page
pages_with_issues = [(name, r) for name, r in results.items() if r["exists"] and r["issues"]]
if pages_with_issues:
    md.append("## Issues by Page")
    for name, r in pages_with_issues:
        md.append(f"\n### {name}")
        md.append(f"URL: `{r['path']}`")
        for issue in r["issues"]:
            md.append(f"- {issue}")
    md.append("")

# Schema coverage
md.append("## Schema Markup Coverage")
md.append("| Page | LocalBusiness | FAQPage | AggregateRating | All Types |")
md.append("|------|:---:|:---:|:---:|---|")
for name, r in results.items():
    if not r["exists"]:
        continue
    lb = "Y" if r["has_local_business"] else "-"
    faq = "Y" if r["has_faq_page"] else "-"
    ar = "Y" if r["has_aggregate_rating"] else "-"
    types = ", ".join(r["schema_types"][:5]) if r["schema_types"] else "None"
    md.append(f"| {name} | {lb} | {faq} | {ar} | {types} |")
md.append("")

# Image alt text breakdown
md.append("## Image Alt Text Breakdown")
md.append("| Page | Total | Good | Poor | Missing |")
md.append("|------|-------|------|------|---------|")
for name, r in results.items():
    if not r["exists"] or r["total_images"] == 0:
        continue
    md.append(f"| {name} | {r['total_images']} | {r['images_good_alt']} | {r['images_poor_alt']} | {r['images_missing_alt']} |")
md.append(f"| **TOTAL** | **{total_images}** | **{good_alt}** | **{poor_alt}** | **{missing_alt}** |")
md.append("")

# Title tag summary
md.append("## Title Tags")
md.append("| Page | Length | Title |")
md.append("|------|--------|-------|")
for name, r in results.items():
    if not r["exists"]:
        continue
    flag = ""
    if r["title_length"] > 60:
        flag = " (LONG)"
    elif r["title_length"] < 30:
        flag = " (SHORT)"
    title_display = r["title"][:70] + "..." if len(r["title"]) > 70 else r["title"]
    md.append(f"| {name} | {r['title_length']}{flag} | {title_display} |")
md.append("")

md.append(f"---\n*Audit complete: {total_pages} pages checked, {total_issues} issues found*")

report_text = "\n".join(md)

# ============================================================
# LOAD HISTORY & TRACK TRENDS
# ============================================================
os.makedirs(REPORT_DIR, exist_ok=True)

history = {}
if os.path.exists(HISTORY_FILE):
    try:
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        history = {}

today_key = today.strftime("%Y-%m-%d")
history[today_key] = {
    "score": score,
    "pages_live": pages_live,
    "pages_missing": pages_missing,
    "total_issues": total_issues,
    "alt_text_pct": round(alt_score, 1),
    "images_total": total_images,
    "images_good": good_alt,
    "images_poor": poor_alt,
    "images_missing": missing_alt,
    "schema_local_business": pages_with_local_biz,
    "schema_faq": pages_with_faq,
    "schema_aggregate_rating": pages_with_rating,
    "title_issues": title_issues,
    "h1_issues": h1_issues,
    "meta_issues": meta_issues,
}

# Keep last 26 weeks of history
sorted_keys = sorted(history.keys())
if len(sorted_keys) > 26:
    for old_key in sorted_keys[:-26]:
        del history[old_key]

with open(HISTORY_FILE, "w") as f:
    json.dump(history, f, indent=2)
print(f"History updated: {HISTORY_FILE}")

# ============================================================
# SAVE REPORT
# ============================================================
report_file = os.path.join(REPORT_DIR, f"{today_key}.md")
with open(report_file, "w") as f:
    f.write(report_text)
print(f"Report saved: {report_file}")

# ============================================================
# BUILD HTML EMAIL
# ============================================================
def score_color(s):
    if s >= 80: return "#2ecc71"
    if s >= 60: return "#f39c12"
    return "#e74c3c"

html_parts = []
html_parts.append(f"""<html><body style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto;">
<h2 style="color: #2c3e50;">Weekly SEO Audit - {today_fmt}</h2>
<div style="background: {score_color(score)}; color: white; padding: 15px; border-radius: 8px; text-align: center; font-size: 24px; margin-bottom: 20px;">
  Score: {score}/100
</div>
<table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
<tr style="background: #ecf0f1;"><td style="padding: 8px; border: 1px solid #bdc3c7;">Pages Live</td><td style="padding: 8px; border: 1px solid #bdc3c7;"><b>{pages_live}/{total_pages}</b></td></tr>
<tr><td style="padding: 8px; border: 1px solid #bdc3c7;">Pages Missing</td><td style="padding: 8px; border: 1px solid #bdc3c7; color: {'#e74c3c' if pages_missing > 0 else '#2ecc71'};"><b>{pages_missing}</b></td></tr>
<tr style="background: #ecf0f1;"><td style="padding: 8px; border: 1px solid #bdc3c7;">Total Issues</td><td style="padding: 8px; border: 1px solid #bdc3c7;"><b>{total_issues}</b></td></tr>
<tr><td style="padding: 8px; border: 1px solid #bdc3c7;">Alt Text Quality</td><td style="padding: 8px; border: 1px solid #bdc3c7;"><b>{alt_score:.0f}%</b> good ({good_alt}/{total_images})</td></tr>
<tr style="background: #ecf0f1;"><td style="padding: 8px; border: 1px solid #bdc3c7;">LocalBusiness Schema</td><td style="padding: 8px; border: 1px solid #bdc3c7;"><b>{pages_with_local_biz}</b> pages</td></tr>
<tr><td style="padding: 8px; border: 1px solid #bdc3c7;">FAQPage Schema</td><td style="padding: 8px; border: 1px solid #bdc3c7;"><b>{pages_with_faq}</b> pages</td></tr>
</table>""")

# Action plan in email
if actions:
    html_parts.append("<h3 style='color: #2c3e50;'>What to Fix This Week</h3>")
    current_priority = None
    priority_labels_html = {1: "HIGH IMPACT", 2: "MEDIUM IMPACT", 3: "LOW IMPACT"}
    priority_colors = {1: "#e74c3c", 2: "#e67e22", 3: "#95a5a6"}
    shown_html = 0
    for priority, category, text in actions:
        if shown_html >= 10:
            break
        if priority != current_priority:
            current_priority = priority
            label = priority_labels_html.get(priority, "OTHER")
            color = priority_colors.get(priority, "#95a5a6")
            html_parts.append(f"<p style='color: {color}; font-weight: bold; margin-top: 15px; margin-bottom: 5px;'>{label}</p>")
        shown_html += 1
        # Convert markdown bold to HTML bold
        text_html = text.replace("**", "<b>", 1).replace("**", "</b>", 1)
        text_html = text_html.replace("`", "<code>").replace("`", "</code>")
        html_parts.append(f"<p style='margin: 4px 0 4px 15px;'>{shown_html}. <span style='background: #ecf0f1; padding: 1px 6px; border-radius: 3px; font-size: 11px;'>{category}</span> {text_html}</p>")
    remaining_html = len(actions) - shown_html
    if remaining_html > 0:
        html_parts.append(f"<p style='color: #95a5a6; font-style: italic;'>Plus {remaining_html} more lower-priority items in the full report.</p>")

if missing_pages:
    html_parts.append("<h3 style='color: #e74c3c;'>Missing Pages (404)</h3><ul>")
    for r in missing_pages:
        html_parts.append(f"<li><b>{r['name']}</b>: <code>{r['path']}</code></li>")
    html_parts.append("</ul>")

# Trend (if we have prior data)
prior_keys = [k for k in sorted(history.keys()) if k < today_key]
if prior_keys:
    prev = history[prior_keys[-1]]
    delta_score = score - prev["score"]
    delta_issues = total_issues - prev["total_issues"]
    delta_alt = round(alt_score - prev["alt_text_pct"], 1)
    arrow = lambda d: f"<span style='color:#2ecc71;'>+{d}</span>" if d > 0 else (f"<span style='color:#e74c3c;'>{d}</span>" if d < 0 else "=")
    arrow_inv = lambda d: f"<span style='color:#e74c3c;'>+{d}</span>" if d > 0 else (f"<span style='color:#2ecc71;'>{d}</span>" if d < 0 else "=")
    html_parts.append(f"""<h3>Week-over-Week</h3>
    <table style="border-collapse: collapse;">
    <tr><td style="padding: 5px 15px 5px 0;">Score</td><td>{prev['score']} &rarr; {score} ({arrow(delta_score)})</td></tr>
    <tr><td style="padding: 5px 15px 5px 0;">Issues</td><td>{prev['total_issues']} &rarr; {total_issues} ({arrow_inv(delta_issues)})</td></tr>
    <tr><td style="padding: 5px 15px 5px 0;">Alt Text</td><td>{prev['alt_text_pct']}% &rarr; {alt_score:.0f}% ({arrow(delta_alt)})</td></tr>
    </table>""")

html_parts.append("<p style='color: #95a5a6; font-size: 12px; margin-top: 30px;'>Automated SEO audit by Black Hill Assistant</p>")
html_parts.append("</body></html>")

html_report = "\n".join(html_parts)

# ============================================================
# SEND EMAIL
# ============================================================
gmail_email = os.environ.get("GMAIL_EMAIL", "")
gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")

if not gmail_email or not gmail_password:
    print("No GMAIL credentials configured. Report saved but email not sent.")
    sys.exit(0)

msg = MIMEMultipart("alternative")
msg["Subject"] = f"Weekly SEO Audit - Score {score}/100 - {today_fmt}"
msg["From"] = formataddr(("Black Hill Assistant", gmail_email))
msg["To"] = TO_EMAIL

msg.attach(MIMEText(report_text, "plain"))
msg.attach(MIMEText(html_report, "html"))

try:
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(gmail_email, gmail_password)
        server.sendmail(gmail_email, TO_EMAIL, msg.as_string())
    print("Email sent successfully!")
except Exception as e:
    print(f"Email send failed: {e}")
    print("Report was saved to file but email delivery failed.")
