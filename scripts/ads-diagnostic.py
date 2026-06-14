#!/usr/bin/env python3
"""One-off read-only Google Ads diagnostic: trend, device, daypart, conv actions,
search terms, and current bidding settings. Reuses ~/.config/google-ads/config.json."""
import json, os, sys
from datetime import datetime, timedelta
from google.ads.googleads.client import GoogleAdsClient

cfg = json.load(open(os.path.expanduser("~/.config/google-ads/config.json")))
client = GoogleAdsClient.load_from_dict({
    "developer_token": cfg["developer_token"], "client_id": cfg["client_id"],
    "client_secret": cfg["client_secret"], "refresh_token": cfg["refresh_token"],
    "login_customer_id": cfg["login_customer_id"], "use_proto_plus": True, "timeout": 60,
})
svc = client.get_service("GoogleAdsService")
cid = cfg["customer_id"]
def q(s):
    try: return list(svc.search(customer_id=cid, query=s))
    except Exception as e: print("  QUERY ERROR:", str(e)[:300]); return []
def usd(m): return f"${m/1e6:,.0f}"

today = datetime.now().date()
w12 = (today - timedelta(days=84)).strftime("%Y-%m-%d")
d28 = (today - timedelta(days=27)).strftime("%Y-%m-%d")
tod = today.strftime("%Y-%m-%d")

print("=" * 70, "\n1) ACCOUNT WEEKLY TREND (last 12 weeks)\n" + "=" * 70)
print(f"{'week':<12}{'cost':>8}{'clicks':>7}{'impr':>8}{'conv':>6}{'IS%':>6}{'CTR%':>6}")
rows = q(f"""SELECT segments.week, metrics.cost_micros, metrics.clicks, metrics.impressions,
  metrics.conversions, metrics.search_impression_share, metrics.ctr
  FROM customer WHERE segments.date BETWEEN '{w12}' AND '{tod}' ORDER BY segments.week""")
agg = {}
for r in rows:
    wk = r.segments.week
    a = agg.setdefault(wk, [0,0,0,0.0,0.0])
    a[0]+=r.metrics.cost_micros; a[1]+=r.metrics.clicks; a[2]+=r.metrics.impressions
    a[3]+=r.metrics.conversions
    a[4]=r.metrics.search_impression_share  # already account-level per row
for wk in sorted(agg):
    c,cl,im,cv,is_ = agg[wk]
    ctr = (cl/im*100) if im else 0
    print(f"{wk:<12}{usd(c):>8}{cl:>7}{im:>8}{cv:>6.1f}{is_*100:>6.1f}{ctr:>6.1f}")

print("\n" + "=" * 70, "\n2) PER-CAMPAIGN (last 28 days)\n" + "=" * 70)
print(f"{'campaign':<30}{'cost':>8}{'conv':>6}{'IS%':>6}{'lostRk%':>8}{'lostBud%':>9}")
for r in q(f"""SELECT campaign.name, metrics.cost_micros, metrics.conversions,
  metrics.search_impression_share, metrics.search_rank_lost_impression_share,
  metrics.search_budget_lost_impression_share
  FROM campaign WHERE segments.date BETWEEN '{d28}' AND '{tod}' AND metrics.cost_micros > 0
  ORDER BY metrics.cost_micros DESC"""):
    print(f"{r.campaign.name[:29]:<30}{usd(r.metrics.cost_micros):>8}{r.metrics.conversions:>6.1f}"
          f"{r.metrics.search_impression_share*100:>6.1f}{r.metrics.search_rank_lost_impression_share*100:>8.1f}"
          f"{r.metrics.search_budget_lost_impression_share*100:>9.1f}")

print("\n" + "=" * 70, "\n3) DEVICE (last 90 days)\n" + "=" * 70)
print(f"{'device':<10}{'cost':>8}{'clicks':>7}{'conv':>6}{'CR%':>7}")
for r in q("""SELECT segments.device, metrics.cost_micros, metrics.clicks, metrics.conversions
  FROM campaign WHERE segments.date DURING LAST_90_DAYS ORDER BY metrics.cost_micros DESC"""):
    cl=r.metrics.clicks; cv=r.metrics.conversions
    print(f"{r.segments.device.name:<10}{usd(r.metrics.cost_micros):>8}{cl:>7}{cv:>6.1f}{(cv/cl*100 if cl else 0):>7.2f}")

print("\n" + "=" * 70, "\n4) HOUR OF DAY (last 30 days)\n" + "=" * 70)
hrs={}
for r in q("""SELECT segments.hour, metrics.cost_micros, metrics.clicks, metrics.conversions
  FROM campaign WHERE segments.date DURING LAST_30_DAYS"""):
    h=r.segments.hour; a=hrs.setdefault(h,[0,0,0.0]); a[0]+=r.metrics.cost_micros; a[1]+=r.metrics.clicks; a[2]+=r.metrics.conversions
blocks={"12a-4a":range(0,4),"4a-8a":range(4,8),"8a-12p":range(8,12),"12p-4p":range(12,16),"4p-8p":range(16,20),"8p-12a":range(20,24)}
print(f"{'block':<8}{'cost':>8}{'clicks':>7}{'conv':>6}")
for b,rg in blocks.items():
    c=sum(hrs.get(h,[0,0,0])[0] for h in rg); cl=sum(hrs.get(h,[0,0,0])[1] for h in rg); cv=sum(hrs.get(h,[0,0,0.0])[2] for h in rg)
    print(f"{b:<8}{usd(c):>8}{cl:>7}{cv:>6.1f}")

print("\n" + "=" * 70, "\n5) CONVERSION ACTIONS (last 30 days, all_conversions)\n" + "=" * 70)
for r in q("""SELECT segments.conversion_action_name, metrics.all_conversions
  FROM customer WHERE segments.date DURING LAST_30_DAYS AND metrics.all_conversions > 0
  ORDER BY metrics.all_conversions DESC"""):
    print(f"  {r.segments.conversion_action_name[:45]:<46}{r.metrics.all_conversions:>7.1f}")

print("\n" + "=" * 70, "\n6) TOP SEARCH TERMS BY COST (last 30 days)\n" + "=" * 70)
print(f"{'term':<42}{'cost':>8}{'clk':>5}{'conv':>6}")
for r in q(f"""SELECT search_term_view.search_term, metrics.cost_micros, metrics.clicks, metrics.conversions
  FROM search_term_view WHERE segments.date BETWEEN '{d28}' AND '{tod}' AND metrics.cost_micros > 0
  ORDER BY metrics.cost_micros DESC LIMIT 25"""):
    print(f"{r.search_term_view.search_term[:41]:<42}{usd(r.metrics.cost_micros):>8}{r.metrics.clicks:>5}{r.metrics.conversions:>6.1f}")

print("\n" + "=" * 70, "\n7) CAMPAIGN SETTINGS (bidding / budget / tCPA)\n" + "=" * 70)
for r in q("""SELECT campaign.name, campaign.status, campaign.bidding_strategy_type,
  campaign_budget.amount_micros, campaign.maximize_conversions.target_cpa_micros,
  campaign.target_cpa.target_cpa_micros
  FROM campaign WHERE campaign.status='ENABLED' ORDER BY campaign.name"""):
    mc = r.campaign.maximize_conversions.target_cpa_micros or r.campaign.target_cpa.target_cpa_micros
    print(f"  {r.campaign.name[:30]:<31} {r.campaign.bidding_strategy_type.name:<22} "
          f"budget {usd(r.campaign_budget.amount_micros)}/day  tCPA {usd(mc) if mc else '-'}")
print("\nDONE")
