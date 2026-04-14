# Keller & Haslet Page Check — April 14, 2026 (Noon CDT)

**Requested by:** Evelin  
**Checked at:** 2026-04-14 ~17:07 UTC (noon CDT)  
**Checked by:** Claude (automated)

---

## Result: UNABLE TO VERIFY — Workflow NOT triggered

The Claude Code environment runs inside a network sandbox whose egress proxy restricts outbound connections to a fixed allowlist of approved hosts. **`blackhilllandscaping.com` is not on that allowlist.** Both `curl` and WebFetch returned proxy-level 403 "host_not_allowed" errors before ever reaching the origin server. A Google site-search query (`site:blackhilllandscaping.com/areas-we-serve/keller`) returned no indexed results, but that is inconclusive for newly published pages.

Because page liveness could not be confirmed, **the `ads-keyword-urls.yml` workflow was NOT triggered** (per standing rule: only trigger on confirmed 200 OK).

---

## Pages That Needed Checking

| URL | Expected status | Verified? |
|-----|----------------|-----------|
| `https://blackhilllandscaping.com/areas-we-serve/keller/` | 200 OK | ❌ Cannot reach |
| `https://blackhilllandscaping.com/areas-we-serve/keller/commercial-landscape-maintenance/` | 200 OK | ❌ Cannot reach |
| `https://blackhilllandscaping.com/areas-we-serve/keller/sprinkler-inspection-and-repairs/` | 200 OK | ❌ Cannot reach |
| `https://blackhilllandscaping.com/areas-we-serve/keller/standing-water/` | 200 OK | ❌ Cannot reach |
| `https://blackhilllandscaping.com/areas-we-serve/haslet/` | 200 OK | ❌ Cannot reach |
| `https://blackhilllandscaping.com/areas-we-serve/haslet/commercial-landscape-maintenance/` | 200 OK | ❌ Cannot reach |

---

## What Needs to Happen

**Option A — Manual check (fastest):**  
Evelin (or anyone with browser access) should open the six URLs above and confirm HTTP 200. If the Keller pages return 200, run this GitHub Actions workflow manually:

```
gh workflow run ads-keyword-urls.yml \
  --repo emontenegro-bh/blackhill-lead-monitor \
  -f dry_run=false \
  -f keyword_urls='[{"keyword_text":"commercial landscape maintenance keller","campaign_name":"BH_CommercialMaint_Search","ad_group_name":"Commercial Property Maintenance","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/commercial-landscape-maintenance/"},{"keyword_text":"commercial landscaping keller","campaign_name":"BH_CommercialMaint_Search","ad_group_name":"Commercial Property Maintenance","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/commercial-landscape-maintenance/"},{"keyword_text":"hoa landscape maintenance keller","campaign_name":"BH_CommercialMaint_Search","ad_group_name":"HOA Landscape Maintenance","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/commercial-landscape-maintenance/"},{"keyword_text":"hoa landscaping keller","campaign_name":"BH_CommercialMaint_Search","ad_group_name":"HOA Landscape Maintenance","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/commercial-landscape-maintenance/"},{"keyword_text":"office park landscaping keller","campaign_name":"BH_CommercialMaint_Search","ad_group_name":"Property Manager Solutions","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/commercial-landscape-maintenance/"},{"keyword_text":"property management landscaping keller","campaign_name":"BH_CommercialMaint_Search","ad_group_name":"Property Manager Solutions","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/commercial-landscape-maintenance/"},{"keyword_text":"sprinkler repair keller tx","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Irrigation Repair","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/sprinkler-inspection-and-repairs/"},{"keyword_text":"sprinkler installation keller tx","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Sprinkler Installations","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/sprinkler-installation/"},{"keyword_text":"irrigation installation keller tx","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Sprinkler Installations","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/sprinkler-installation/"},{"keyword_text":"french drain keller tx","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Drainage Solutions","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/standing-water/"},{"keyword_text":"yard drainage keller tx","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Drainage Solutions","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/standing-water/"},{"keyword_text":"sprinkler repair colleyville","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Irrigation Repair","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/sprinkler-inspection-and-repairs/"},{"keyword_text":"sprinkler installation colleyville","campaign_name":"BH_PC_Irrigationservice","ad_group_name":"Sprinkler Installations","final_url":"https://blackhilllandscaping.com/areas-we-serve/keller/sprinkler-installation/"}]'
```

**Option B — Add blackhilllandscaping.com to the allowed egress hosts** in the Claude Code environment's network policy, then re-run this check.

---

## Keywords Pending URL Update (13 total)

If Keller pages ARE live, the following keyword → URL mappings are queued:

| Keyword | Campaign | Ad Group | Target URL |
|---------|----------|----------|------------|
| commercial landscape maintenance keller | BH_CommercialMaint_Search | Commercial Property Maintenance | `/keller/commercial-landscape-maintenance/` |
| commercial landscaping keller | BH_CommercialMaint_Search | Commercial Property Maintenance | `/keller/commercial-landscape-maintenance/` |
| hoa landscape maintenance keller | BH_CommercialMaint_Search | HOA Landscape Maintenance | `/keller/commercial-landscape-maintenance/` |
| hoa landscaping keller | BH_CommercialMaint_Search | HOA Landscape Maintenance | `/keller/commercial-landscape-maintenance/` |
| office park landscaping keller | BH_CommercialMaint_Search | Property Manager Solutions | `/keller/commercial-landscape-maintenance/` |
| property management landscaping keller | BH_CommercialMaint_Search | Property Manager Solutions | `/keller/commercial-landscape-maintenance/` |
| sprinkler repair keller tx | BH_PC_Irrigationservice | Irrigation Repair | `/keller/sprinkler-inspection-and-repairs/` |
| sprinkler installation keller tx | BH_PC_Irrigationservice | Sprinkler Installations | `/keller/sprinkler-installation/` |
| irrigation installation keller tx | BH_PC_Irrigationservice | Sprinkler Installations | `/keller/sprinkler-installation/` |
| french drain keller tx | BH_PC_Irrigationservice | Drainage Solutions | `/keller/standing-water/` |
| yard drainage keller tx | BH_PC_Irrigationservice | Drainage Solutions | `/keller/standing-water/` |
| sprinkler repair colleyville | BH_PC_Irrigationservice | Irrigation Repair | `/keller/sprinkler-inspection-and-repairs/` |
| sprinkler installation colleyville | BH_PC_Irrigationservice | Sprinkler Installations | `/keller/sprinkler-installation/` |

Note: Haslet pages were also on the checklist but no keyword URL updates were queued for Haslet in this request — just status verification.
