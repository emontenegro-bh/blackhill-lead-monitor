"""
Quality Score recheck — compares current irrigation campaign QS
against the March 29 baseline and emails results via SendGrid.
"""

import os
import json
from google.ads.googleads.client import GoogleAdsClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

CUSTOMER_ID = "9637062915"
CAMPAIGN_ID = 22815919817

BASELINE = {
    "sprinkler head replacement":       {"qs": 3, "ad_rel": "ABOVE_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "BELOW_AVERAGE"},
    "irrigation repair fort worth":     {"qs": 3, "ad_rel": "BELOW_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "AVERAGE"},
    "sprinkler valve repair":           {"qs": 4, "ad_rel": "AVERAGE",        "exp_ctr": "BELOW_AVERAGE", "land_page": "AVERAGE"},
    "drainage solutions":               {"qs": 5, "ad_rel": "ABOVE_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "AVERAGE"},
    "yard drainage fort worth":         {"qs": 5, "ad_rel": "ABOVE_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "AVERAGE"},
    "sprinkler system repair":          {"qs": 5, "ad_rel": "BELOW_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "ABOVE_AVERAGE"},
    "sprinkler repair near me":         {"qs": 5, "ad_rel": "BELOW_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "ABOVE_AVERAGE"},
    "irrigation repair near me":        {"qs": 5, "ad_rel": "BELOW_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "ABOVE_AVERAGE"},
    "lawn sprinkler repair fort worth": {"qs": 5, "ad_rel": "BELOW_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "ABOVE_AVERAGE"},
    "sprinkler system installation":    {"qs": 5, "ad_rel": "ABOVE_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "AVERAGE"},
    "sprinkler repair benbrook tx":     {"qs": 6, "ad_rel": "AVERAGE",        "exp_ctr": "BELOW_AVERAGE", "land_page": "ABOVE_AVERAGE"},
    "irrigation system installation":   {"qs": 7, "ad_rel": "ABOVE_AVERAGE",  "exp_ctr": "BELOW_AVERAGE", "land_page": "ABOVE_AVERAGE"},
}

NEW_KEYWORDS_MAR29 = [
    "sprinkler not working fort worth", "sprinkler system leaking fort worth",
    "low water pressure sprinkler fort worth", "sprinkler zone not working fort worth",
    "sprinkler won't turn on fort worth", "emergency sprinkler repair fort worth",
    "same day sprinkler repair fort worth", "sprinkler head replacement fort worth",
    "sprinkler valve repair fort worth", "sprinkler leak repair fort worth",
    "smart irrigation controller installation fort worth", "drip irrigation installation fort worth",
    "new sprinkler system fort worth", "sprinkler company fort worth",
    "french drain installation fort worth", "standing water solutions fort worth",
    "surface drain installation fort worth",
]

RANK = {"BELOW_AVERAGE": 0, "AVERAGE": 1, "ABOVE_AVERAGE": 2, "UNSPECIFIED": -1}


def build_client():
    config = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", CUSTOMER_ID),
        "use_proto_plus": True,
    }
    return GoogleAdsClient.load_from_dict(config)


def query_scores(client):
    ga = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            ad_group.name,
            ad_group_criterion.keyword.text,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
            ad_group_criterion.quality_info.post_click_quality_score
        FROM keyword_view
        WHERE campaign.id = {CAMPAIGN_ID}
            AND ad_group_criterion.status = 'ENABLED'
        ORDER BY ad_group.name, ad_group_criterion.keyword.text
    """
    rows = []
    for row in ga.search(customer_id=CUSTOMER_ID, query=query):
        qi = row.ad_group_criterion.quality_info
        rows.append({
            "ad_group": row.ad_group.name,
            "keyword": row.ad_group_criterion.keyword.text,
            "qs": qi.quality_score if qi.quality_score > 0 else None,
            "ad_rel": str(qi.creative_quality_score).split(".")[-1],
            "exp_ctr": str(qi.search_predicted_ctr).split(".")[-1],
            "land_page": str(qi.post_click_quality_score).split(".")[-1],
        })
    return rows


def compare(current_rows):
    changes = []
    for row in current_rows:
        kw = row["keyword"]
        if kw in BASELINE:
            base = BASELINE[kw]
            diffs = []
            if row["qs"] and row["qs"] != base["qs"]:
                direction = "+" if row["qs"] > base["qs"] else ""
                diffs.append(f"QS {base['qs']}→{row['qs']} ({direction}{row['qs'] - base['qs']})")
            for component in ["ad_rel", "exp_ctr", "land_page"]:
                old_val = base[component]
                new_val = row[component]
                if new_val != "UNSPECIFIED" and new_val != old_val:
                    direction = "improved" if RANK.get(new_val, -1) > RANK.get(old_val, -1) else "declined"
                    diffs.append(f"{component}: {old_val}→{new_val} ({direction})")
            if diffs:
                changes.append({"keyword": kw, "changes": diffs})
    return changes


def build_email(current_rows, changes):
    lines = []
    lines.append("<h2>Irrigation Campaign QS Recheck — April 1, 2026</h2>")
    lines.append("<p>Comparing against March 29 baseline (post-optimization).</p>")

    # Full table
    lines.append("<h3>All Keyword Scores</h3>")
    lines.append("<table border='1' cellpadding='5' cellspacing='0' style='border-collapse:collapse;font-family:monospace;font-size:13px'>")
    lines.append("<tr><th>Ad Group</th><th>Keyword</th><th>QS</th><th>Ad Rel</th><th>Exp CTR</th><th>Land Page</th></tr>")
    for row in sorted(current_rows, key=lambda r: (r["qs"] or 0, r["keyword"])):
        qs_display = str(row["qs"]) if row["qs"] else "--"
        lines.append(f"<tr><td>{row['ad_group']}</td><td>{row['keyword']}</td><td>{qs_display}</td>"
                      f"<td>{row['ad_rel']}</td><td>{row['exp_ctr']}</td><td>{row['land_page']}</td></tr>")
    lines.append("</table>")

    # Changes
    lines.append("<h3>Changes Since March 29</h3>")
    if changes:
        lines.append("<ul>")
        for c in changes:
            detail = "; ".join(c["changes"])
            lines.append(f"<li><strong>{c['keyword']}</strong>: {detail}</li>")
        lines.append("</ul>")
    else:
        lines.append("<p>No changes detected in existing keyword scores.</p>")

    # New keywords
    lines.append("<h3>New Keywords Status (added Mar 29)</h3>")
    new_scored = [r for r in current_rows if r["keyword"] in NEW_KEYWORDS_MAR29 and r["qs"]]
    new_unscored = [r for r in current_rows if r["keyword"] in NEW_KEYWORDS_MAR29 and not r["qs"]]
    if new_scored:
        lines.append("<p><strong>Now have QS:</strong></p><ul>")
        for r in new_scored:
            lines.append(f"<li>{r['keyword']}: QS {r['qs']} | Ad Rel: {r['ad_rel']} | CTR: {r['exp_ctr']} | LP: {r['land_page']}</li>")
        lines.append("</ul>")
    if new_unscored:
        lines.append(f"<p><strong>Still awaiting scores:</strong> {len(new_unscored)} keywords (need more impressions)</p>")

    # Recommendations
    lines.append("<h3>Recommendations</h3>")
    lines.append("<ul>")

    declined = [c for c in changes if any("declined" in d for d in c["changes"])]
    improved = [c for c in changes if any("improved" in d for d in c["changes"])]
    ad_rel_still_below = [r for r in current_rows if r["keyword"] in BASELINE and r["ad_rel"] == "BELOW_AVERAGE"]

    if declined:
        lines.append("<li><strong>Declined keywords need attention:</strong> " +
                      ", ".join(c["keyword"] for c in declined) + "</li>")
    if improved:
        lines.append("<li><strong>Improvements confirmed:</strong> " +
                      ", ".join(c["keyword"] for c in improved) +
                      " — headline changes are working.</li>")
    if ad_rel_still_below:
        lines.append(f"<li><strong>Ad relevance still BELOW_AVERAGE on {len(ad_rel_still_below)} keywords</strong> — "
                      "consider adding more keyword-specific headline variations or testing new RSA combinations.</li>")
    if not declined and not improved:
        lines.append("<li>No movement yet — Google may need more impression data. Recheck in another 3-4 days.</li>")

    ctr_below = [r for r in current_rows if r["qs"] and r["exp_ctr"] == "BELOW_AVERAGE"]
    if len(ctr_below) == len([r for r in current_rows if r["qs"]]):
        lines.append("<li><strong>Expected CTR remains BELOW_AVERAGE across all scored keywords</strong> — "
                      "this is the biggest drag on QS. Will improve with click-through performance over time.</li>")

    lines.append("</ul>")
    lines.append("<p style='color:gray;font-size:11px'>Auto-generated by qs-recheck.py via GitHub Actions</p>")

    return "\n".join(lines)


def send_email(html_body):
    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    message = Mail(
        from_email=os.environ.get("NOTIFY_FROM_EMAIL", "noreply@blackhilltx.com"),
        to_emails="evelin@blackhilltx.com",
        subject="Irrigation Campaign QS Recheck — April 1",
        html_content=html_body,
    )
    response = sg.send(message)
    print(f"Email sent: {response.status_code}")


def main():
    client = build_client()
    print("Querying current Quality Scores...")
    current = query_scores(client)
    print(f"Found {len(current)} enabled keywords.")

    changes = compare(current)
    print(f"Detected {len(changes)} keywords with score changes.")

    html = build_email(current, changes)
    send_email(html)
    print("Done.")


if __name__ == "__main__":
    main()
