import json, sys, urllib.request, os
UA={"User-Agent":"Mozilla/5.0 (macro-dashboard probe)"}
BNM={"Accept":"application/vnd.BNM.API.v1+json","User-Agent":UA["User-Agent"]}
key=os.environ.get("FRED_API_KEY","")
out=[]
def t(name,url,headers=UA,n=500):
    try:
        req=urllib.request.Request(url,headers=headers)
        with urllib.request.urlopen(req,timeout=30) as r:
            b=r.read(); out.append(f"OK  {name} [{r.status}] {len(b)}B :: {b[:n].decode('utf-8','replace')!r}")
    except Exception as e:
        out.append(f"ERR {name} :: {e}")
for sid in ("IRLTLT01MYM156N","INTDSRMYM193N","IR3TIB01MYM156N","TRESEGMYM052N","MYSCPIALLMINMEI"):
    t("FRED "+sid, f"https://api.stlouisfed.org/fred/series?series_id={sid}&api_key={key}&file_type=json")
for ep in ("","mgs","government-securities","govt-securities","bond-yield","interest-rate","international-reserves","reserves","kijang-emas","base-rate","interbank-rate","exchange-rate"):
    t("BNM /"+ep, "https://api.bnm.gov.my/public/"+ep, BNM, 300)
for i in ("mgs","mgs_yield","govt_bond","bond_yield","mgs_gii","interest_rates","reserves","international_reserves","gdp_qtr_real","gdp_qtr_real_sa"):
    t("DOSM "+i, f"https://api.data.gov.my/data-catalogue?id={i}&limit=2", UA, 400)
t("WGB MY", "https://www.worldgovernmentbonds.com/country/malaysia/", UA, 200)
t("BPAM", "https://bpam.com.my/", UA, 100)
open(os.path.join(os.path.dirname(__file__),"..","malaysia_probe.txt"),"w").write("\n".join(out))
print("\n".join(o[:200] for o in out))
