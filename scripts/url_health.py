"""Shared landing-page health check for the weekly ads reports.

Why this exists: on 2026-08-31 and 2026-09-01 the web dev team edited final URLs
and tracking fields directly in Google Ads. A broken destination costs money
silently, because the ad still serves and still charges for the click. This
module resolves the destination every live keyword actually sends traffic to,
requests it, and flags anything that is not a clean 200.

Used by ads-weekly-report.py (Google) and bing-weekly-report.py (Bing).
"""

import concurrent.futures
import urllib.error
import urllib.request

TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; BlackHillAdsMonitor/1.0)"


def service_slug(url):
    """The service a URL sells, with the city stripped out.

    /areas-we-serve/weatherford/sprinkler-inspection-and-repairs/ and
    /areas-we-serve/fort-worth/sprinkler-inspection-and-repairs/ are the same
    service in two cities and must not be treated as a mismatch.
    """
    if not url:
        return ""
    path = url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "areas-we-serve":
        return parts[2]          # drop the city segment
    return parts[-1] if parts else ""


def check_url(url):
    """Request one URL. Returns a dict describing what happened."""
    result = {
        "url": url, "status": None, "final_url": url,
        "redirects": 0, "ok": False, "error": None,
    }
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result["status"] = resp.status
            result["final_url"] = resp.geturl()
            result["redirects"] = 0 if resp.geturl() == url else 1
            result["ok"] = resp.status == 200
    except urllib.error.HTTPError as e:
        result["status"] = e.code
        result["error"] = f"HTTP {e.code}"
    except Exception as e:
        result["error"] = str(e)[:120]
    return result


def check_all(urls):
    """Check many URLs in parallel. Returns {url: result}."""
    urls = sorted(set(u for u in urls if u))
    if not urls:
        return {}
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for res in pool.map(check_url, urls):
            out[res["url"]] = res
    return out


def summarize(destinations, results, inherited=None):
    """destinations: list of (ad_group, keyword, url).

    inherited: optional set of (ad_group, keyword) pairs whose URL came from the
    ad rather than the keyword. Only those can be mis-routed by an ad group that
    covers two services, because a keyword carrying its own URL is already
    pointed where it belongs.

    Returns (problems, url_rows, stats) where problems is the list that should
    raise an alarm and url_rows is one row per distinct URL for the table.
    """
    by_url = {}
    for ag, kw, url in destinations:
        by_url.setdefault(url, {"ad_groups": set(), "keywords": []})
        by_url[url]["ad_groups"].add(ag)
        by_url[url]["keywords"].append(kw)

    url_rows, problems = [], []
    for url, meta in sorted(by_url.items()):
        r = results.get(url, {"ok": False, "error": "not checked", "status": None,
                              "redirects": 0, "final_url": url})
        row = {
            "url": url,
            "ad_groups": sorted(meta["ad_groups"]),
            "keyword_count": len(meta["keywords"]),
            "status": r.get("status"),
            "ok": r.get("ok", False),
            "error": r.get("error"),
            "redirects": r.get("redirects", 0),
            "final_url": r.get("final_url", url),
        }
        url_rows.append(row)
        if not row["ok"]:
            problems.append(row)

    # An ad group whose keywords resolve to two different SERVICES is a
    # relevance problem even when every page returns 200. City variants of the
    # same service are not: /weatherford/sprinkler-inspection-and-repairs/ and
    # /fort-worth/sprinkler-inspection-and-repairs/ are the same page localised,
    # which is the correct setup, so compare on the service slug only.
    # Only keywords that inherit the ad's URL can be mis-routed this way. When
    # every keyword carries its own URL, an ad group spanning two services is an
    # ad-copy question, not a broken destination, so it is not raised here.
    ag_urls = {}
    for ag, kw, url in destinations:
        if inherited is not None and (ag, kw) not in inherited:
            continue
        ag_urls.setdefault(ag, set()).add(url)
    split_groups = {}
    for ag, urls in ag_urls.items():
        if len(urls) < 2:
            continue
        if len({service_slug(u) for u in urls if u}) > 1:
            split_groups[ag] = sorted(urls)

    missing = [(ag, kw) for ag, kw, url in destinations if not url]

    stats = {
        "keywords": len(destinations),
        "urls": len(url_rows),
        "broken": len(problems),
        "split_groups": split_groups,
        "missing": missing,
    }
    return problems, url_rows, stats


def render_html(h, url_rows, stats, heading="Landing Page Health"):
    """Append the section to a report. h is the report's line-append callable."""
    broken = stats["broken"]
    split = stats["split_groups"]
    missing = stats["missing"]
    clean = broken == 0 and not split and not missing

    border = "#3fb950" if clean else "#f85149"
    h('<div class="section">')
    h(f'<div class="section-title">{heading}</div>')

    if clean:
        h(f'<div style="padding:10px 12px;background:#161b22;border-left:3px solid {border};'
          f'border-radius:6px;font-size:13px;color:#3fb950;">'
          f'All {stats["urls"]} live destinations returned 200. '
          f'{stats["keywords"]} keywords checked. No ad group splits traffic across pages.</div>')
    else:
        h(f'<div style="padding:10px 12px;background:#161b22;border-left:3px solid {border};'
          f'border-radius:6px;font-size:13px;color:#f85149;font-weight:600;">'
          f'{broken} broken destination(s), {len(split)} ad group(s) pointing at multiple pages, '
          f'{len(missing)} keyword(s) with no destination.</div>')

    h('<table style="width:100%;border-collapse:collapse;margin-top:10px;font-size:12px;">')
    h('<tr style="color:#888;text-align:left;">'
      '<th style="padding:4px 6px;">Destination</th>'
      '<th style="padding:4px 6px;">Ad group</th>'
      '<th style="padding:4px 6px;">Kws</th>'
      '<th style="padding:4px 6px;">Status</th></tr>')
    for row in url_rows:
        color = "#3fb950" if row["ok"] else "#f85149"
        status = row["status"] if row["status"] is not None else (row["error"] or "no response")
        if row["ok"] and row["redirects"]:
            status = f'{status} (redirected)'
        short = row["url"].replace("https://", "").replace("www.", "")
        h(f'<tr style="border-top:1px solid #21262d;">'
          f'<td style="padding:4px 6px;color:#ddd;">{short}</td>'
          f'<td style="padding:4px 6px;color:#888;">{", ".join(row["ad_groups"])}</td>'
          f'<td style="padding:4px 6px;color:#888;">{row["keyword_count"]}</td>'
          f'<td style="padding:4px 6px;color:{color};font-weight:600;">{status}</td></tr>')
    h('</table>')

    for ag, urls in split.items():
        h(f'<div style="margin-top:8px;font-size:12px;color:#d29922;">'
          f'<strong>{ag}</strong> sends traffic to {len(urls)} different pages. '
          f'Keywords there will not all match the ad copy they trigger.</div>')
    for ag, kw in missing:
        h(f'<div style="margin-top:8px;font-size:12px;color:#f85149;">'
          f'<strong>{ag}</strong>: "{kw}" has no destination URL at all.</div>')
    h('</div>')


def render_md(url_rows, stats, heading="Landing Page Health"):
    """Return markdown lines for the archived report."""
    md = [f"\n## {heading}\n"]
    if stats["broken"] == 0 and not stats["split_groups"] and not stats["missing"]:
        md.append(f"All {stats['urls']} live destinations returned 200 "
                  f"({stats['keywords']} keywords checked). No issues.\n")
    else:
        md.append(f"**{stats['broken']} broken, {len(stats['split_groups'])} split ad groups, "
                  f"{len(stats['missing'])} keywords with no destination.**\n")

    md.append("| Destination | Ad group | Keywords | Status |")
    md.append("|---|---|---|---|")
    for row in url_rows:
        status = row["status"] if row["status"] is not None else (row["error"] or "no response")
        if row["ok"] and row["redirects"]:
            status = f"{status} (redirected)"
        md.append(f"| {row['url']} | {', '.join(row['ad_groups'])} | "
                  f"{row['keyword_count']} | {status} |")

    for ag, urls in stats["split_groups"].items():
        md.append(f"\n- **{ag}** sends traffic to {len(urls)} different pages: {', '.join(urls)}")
    for ag, kw in stats["missing"]:
        md.append(f"\n- **{ag}**: `{kw}` has no destination URL.")
    return md
