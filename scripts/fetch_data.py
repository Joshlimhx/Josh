#!/usr/bin/env python3
"""
Josh Macro Playbook — data fetcher
Runs inside GitHub Actions (server-side, full internet access — NOT inside a
claude.ai Artifact sandbox, so normal fetch/requests calls to third-party
APIs work here without CORS restrictions).

Design principle (per evidence-tier discipline): every field carries a
`tier` tag so the frontend never presents a periodically-checked number as
if it were live-fed.

  tier = "live-api"      -> fetched fresh every run from a primary/official API
  tier = "event-driven"  -> carried over from manual.json; only changes when
                            a real-world event happens (BOJ meeting, budget)
  tier = "stale"         -> the live fetch failed this run; last-good value
                            kept, but flagged so the frontend can warn

Each fetch is wrapped so one broken source never takes down the whole run.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
import csv
import io
import os

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

UA = {"User-Agent": "josh-macro-dashboard/1.0 (personal research tool)"}


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fred_latest(series_id, n=14):
    """Return the n most recent (date, value) pairs for a FRED series,
    skipping missing observations ('.')."""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY not set")
    url = (f"{FRED_BASE}?series_id={series_id}&api_key={FRED_API_KEY}"
           f"&file_type=json&sort_order=desc&limit={n}")
    raw = http_get(url)
    obs = json.loads(raw)["observations"]
    out = [(o["date"], o["value"]) for o in obs if o["value"] != "."]
    if not out:
        raise RuntimeError(f"no valid observations for {series_id}")
    return out


def fred_point(series_id):
    date, val = fred_latest(series_id, n=5)[0]
    return {"date": date, "value": float(val)}


def safe_fetch(name, fn, fallback):
    """Run fn(); on any failure, return fallback marked stale instead of
    crashing the whole pipeline."""
    try:
        result = fn()
        result["tier"] = "live-api"
        result["error"] = None
        return result
    except Exception as e:
        print(f"[warn] {name} fetch failed: {e}", file=sys.stderr)
        stale = dict(fallback)
        stale["tier"] = "stale"
        stale["error"] = str(e)
        return stale


# ---------------------------------------------------------------------------
# US / global macro — all via FRED (official, free, stable)
# ---------------------------------------------------------------------------

def get_fed_funds():
    p = fred_point("DFF")
    return {"value": p["value"], "unit": "%", "as_of": p["date"], "source": "FRED:DFF"}


def get_ust(series_id, label):
    def _f():
        p = fred_point(series_id)
        return {"value": p["value"], "unit": "%", "as_of": p["date"], "source": f"FRED:{series_id}"}
    return _f


def get_cpi_yoy():
    obs = fred_latest("CPIAUCSL", n=14)
    obs_sorted = sorted(obs, key=lambda x: x[0])
    latest_date, latest_val = obs_sorted[-1]
    year_ago_val = None
    for d, v in obs_sorted:
        if d[:4] == str(int(latest_date[:4]) - 1) and d[5:7] == latest_date[5:7]:
            year_ago_val = float(v)
            break
    if year_ago_val is None:
        raise RuntimeError("could not locate year-ago CPI print")
    yoy = (float(latest_val) / year_ago_val - 1) * 100
    return {"value": round(yoy, 2), "unit": "% YoY", "as_of": latest_date, "source": "FRED:CPIAUCSL"}


def get_unemployment():
    p = fred_point("UNRATE")
    return {"value": p["value"], "unit": "%", "as_of": p["date"], "source": "FRED:UNRATE"}


def get_debt_gdp():
    p = fred_point("GFDEGDQ188S")
    return {"value": p["value"], "unit": "%", "as_of": p["date"], "source": "FRED:GFDEGDQ188S"}


def get_vix():
    p = fred_point("VIXCLS")
    return {"value": p["value"], "unit": "", "as_of": p["date"], "source": "FRED:VIXCLS"}


def get_dxy():
    p = fred_point("DTWEXBGS")
    return {"value": p["value"], "unit": "", "as_of": p["date"], "source": "FRED:DTWEXBGS (trade-weighted broad dollar index)"}


def get_hy_credit_spread():
    p = fred_point("BAMLH0A0HYM2")
    return {"value": p["value"], "unit": "%", "as_of": p["date"], "source": "FRED:BAMLH0A0HYM2 (ICE BofA US High Yield OAS)"}


def get_oil():
    p = fred_point("DCOILWTICO")
    return {"value": p["value"], "unit": "USD/bbl", "as_of": p["date"], "source": "FRED:DCOILWTICO (WTI spot)"}


def get_rrp():
    p = fred_point("RRPONTSYD")
    return {"value": p["value"], "unit": "USD bn", "as_of": p["date"], "source": "FRED:RRPONTSYD (Fed overnight reverse repo, daily)"}


# ---------------------------------------------------------------------------
# JGB full history (1974-present) — for the historical context chart.
# MOF's all-history file uses Japanese era dates (S=Showa, H=Heisei, R=Reiwa)
# and "N" (年) column suffixes instead of the English file's "Y" suffixes.
# Written to a SEPARATE file (jgb_history.json) so data.json stays lean.
# ---------------------------------------------------------------------------

ERA_START_YEAR = {"S": 1925, "H": 1988, "R": 2018}  # add era_number to get Gregorian year


def era_date_to_iso(era_str):
    """Convert 'R8.4.1' style Japanese-era date to '2026-04-01'."""
    import re
    m = re.match(r"^([SHR])(\d+)\.(\d+)\.(\d+)$", era_str.strip())
    if not m:
        return None
    era, era_year, month, day = m.groups()
    year = ERA_START_YEAR[era] + int(era_year)
    return f"{year:04d}-{int(month):02d}-{int(day):02d}"


def get_jgb_history():
    raw = http_get("https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv", timeout=120)
    text = raw.decode("shift_jis", errors="replace")
    reader = list(csv.reader(io.StringIO(text)))
    def _tenor(c):
        # Normalise "10年" / "10Y" / "10N" / "10" -> "10"
        c = c.strip().replace("\u5e74", "").rstrip("YyNn").strip()
        return c

    header_row = None
    header_idx = None
    for i, row in enumerate(reader):
        norm = [_tenor(c) for c in row]
        if "10" in norm and "30" in norm:
            header_row, header_idx = row, i
            break
    if header_row is None:
        sample = " | ".join(",".join(r) for r in reader[:6])
        raise RuntimeError(f"could not find header row in MOF full-history CSV; first rows: {sample}")
    start_idx = header_idx + 1
    norm = [_tenor(c) for c in header_row]
    idx_10y, idx_30y = norm.index("10"), norm.index("30")

    series_10y, series_30y = [], []
    for row in reader[start_idx:]:
        if not row or not row[0].strip():
            continue
        iso_date = era_date_to_iso(row[0])
        if not iso_date:
            continue
        v10 = row[idx_10y].strip() if len(row) > idx_10y else ""
        v30 = row[idx_30y].strip() if len(row) > idx_30y else ""
        if v10 and v10 != "-":
            try:
                series_10y.append([iso_date, float(v10)])
            except ValueError:
                pass
        if v30 and v30 != "-":
            try:
                series_30y.append([iso_date, float(v30)])
            except ValueError:
                pass

    if not series_10y:
        raise RuntimeError("parsed zero valid 10Y rows from MOF full-history CSV")

    return {
        "10y": series_10y,
        "30y": series_30y,
        "source": "MOF Japan — 国債金利情報 全期間データ (jgbcm_all.csv), 1974-present",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_buffett_indicator():
    # FRED discontinued the Wilshire 5000 series (WILL5000INDFC) on 2024-06-03
    # (Wilshire Associates pulled licensing). Use the Fed's own Z.1 flow-of-funds
    # series instead: Nonfinancial Corporate Business Equities, Liability Level
    # (NCBEILQ027S) — quarterly, official, and arguably closer to what Buffett's
    # original formulation meant than the Wilshire proxy anyway.
    equities = fred_point("NCBEILQ027S")  # $ millions
    gdp_obs = fred_latest("GDP", n=4)
    gdp_date, gdp_val = gdp_obs[0]
    # NCBEILQ027S is reported in $ millions; FRED's GDP series is in $ billions.
    # Convert equities to billions before dividing, or the ratio comes out ~1000x too high.
    equities_billions = equities["value"] / 1000
    ratio = (equities_billions / float(gdp_val)) * 100
    return {
        "value": round(ratio, 1),
        "unit": "%",
        "as_of": equities["date"],
        "source": "FRED:NCBEILQ027S / FRED:GDP (Fed Z.1 corporate equities-to-GDP)",
        "note": "Methodology changed 2026-08 after FRED discontinued Wilshire 5000 data in 2024. This series covers nonfinancial corporate equities only (excludes financials), so it will likely read structurally LOWER than the classic Wilshire-based Buffett print (~190-240% range). Treat the first live reading as a new baseline, not a direct continuation of the old seed number (235.7%) — check the level makes sense before trusting the Valuation Heat score, which was calibrated against the old series.",
        "components": {"corp_equities_billion": equities_billions, "gdp_billion": float(gdp_val), "gdp_as_of": gdp_date},
    }


# ---------------------------------------------------------------------------
# Crypto / FX / metals
# ---------------------------------------------------------------------------

def get_btc():
    raw = http_get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
    val = json.loads(raw)["bitcoin"]["usd"]
    return {"value": val, "unit": "USD", "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "source": "CoinGecko"}


def get_usdjpy():
    raw = http_get("https://api.frankfurter.dev/v1/latest?from=USD&to=JPY")
    d = json.loads(raw)
    return {"value": d["rates"]["JPY"], "unit": "JPY per USD", "as_of": d["date"], "source": "Frankfurter (ECB reference rates)"}


def get_gold():
    raw = http_get("https://api.gold-api.com/price/XAU")
    d = json.loads(raw)
    return {"value": d["price"], "unit": "USD/oz", "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "source": "gold-api.com"}


# ---------------------------------------------------------------------------
# Japan — Ministry of Finance JGB yield curve (primary source, daily CSV)
# ---------------------------------------------------------------------------

def get_jgb_curve():
    import re
    raw = http_get("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv")
    text = raw.decode("shift_jis", errors="replace")
    reader = list(csv.reader(io.StringIO(text)))
    header_row = None
    for row in reader:
        if row and row[0].strip().lower() == "date":
            header_row = row
            break
    if header_row is None:
        raise RuntimeError("could not find header row in MOF CSV")
    start_idx = reader.index(header_row) + 1
    date_pattern = re.compile(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$")
    # Only accept rows whose first cell is an actual date AND that have at
    # least as many columns as the header — this excludes the trailing
    # disclaimer/footer text MOF appends to the file (e.g. "if you cannot
    # download the latest csv data, please clear your browser's cache..."),
    # which previously got picked up as if it were the last data row.
    data_rows = [
        r for r in reader[start_idx:]
        if r and date_pattern.match(r[0].strip()) and len(r) >= len(header_row)
    ]
    if not data_rows:
        raise RuntimeError("no valid dated rows found in MOF CSV after filtering")
    last = data_rows[-1]
    cols = [c.strip() for c in header_row]
    values = dict(zip(cols, last))
    tenors = {"10Y": "10Y", "20Y": "20Y", "30Y": "30Y", "40Y": "40Y", "2Y": "2Y"}
    out = {}
    for label, col in tenors.items():
        if col in values and values[col].strip() not in ("", "-"):
            out[label] = float(values[col])
    return {
        "value": out.get("10Y"),
        "curve": out,
        "unit": "%",
        "as_of": values.get("Date", "").strip(),
        "source": "MOF Japan — 国債金利情報 (jgbcme.csv)",
    }


# ---------------------------------------------------------------------------
# Shiller CAPE — best-effort (Yale source is an .xls file, fragile to parse)
# ---------------------------------------------------------------------------

def get_cape():
    import pandas as pd  # local import: optional heavy dependency
    raw = http_get("https://img1.wsimg.com/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53/downloads/ie_data.xls")
    df = pd.read_excel(io.BytesIO(raw), sheet_name="Data", skiprows=7)
    df = df.dropna(subset=[df.columns[0]])
    cape_col = [c for c in df.columns if "CAPE" in str(c).upper()]
    if not cape_col:
        raise RuntimeError("CAPE column not found in Shiller workbook")
    last_valid = df[cape_col[0]].dropna().iloc[-1]
    return {"value": round(float(last_valid), 2), "unit": "x", "as_of": "latest available month", "source": "Shiller/Yale ie_data.xls"}



# ---------------------------------------------------------------------------
# WORLD: Europe / UK / Asia-EM / liquidity & financial conditions (all FRED)
# ---------------------------------------------------------------------------

def _monthly_yield(series_id, label):
    def fn():
        p = fred_point(series_id)
        return {"value": p["value"], "unit": "%", "as_of": p["date"],
                "source": f"FRED:{series_id} ({label} 10Y gov bond yield, OECD monthly avg — lags ~1-2 months)"}
    return fn


def _fx_3m_move(series_id, label):
    """3-month % change of local currency vs USD. FRED H.10 quotes local per USD,
    so a POSITIVE number = local currency WEAKENED vs the dollar."""
    def fn():
        obs = fred_latest(series_id, n=90)           # ~4 months of daily obs, newest first
        latest_d, latest_v = obs[0][0], float(obs[0][1])
        target = datetime.fromisoformat(latest_d) - timedelta(days=91)
        old_v = None
        for d, v in obs:
            if datetime.fromisoformat(d) <= target:
                old_v = float(v); break
        if old_v is None:
            old_v = float(obs[-1][1])
        return {"value": round((latest_v / old_v - 1) * 100, 2), "unit": "% (+ = weaker vs USD, 3M)",
                "level": latest_v, "as_of": latest_d,
                "source": f"FRED:{series_id} ({label} per USD, daily; 3-month change)"}
    return fn


def _plain(series_id, label, unit, divisor=1.0, digits=2):
    def fn():
        p = fred_point(series_id)
        return {"value": round(p["value"] / divisor, digits), "unit": unit, "as_of": p["date"],
                "source": f"FRED:{series_id} ({label})"}
    return fn


def build_world():
    w = {"_purpose": "Beyond US/Japan: sovereign spreads in Europe/UK, Asia & EM currency stress, EM credit, and US financial conditions — the channels through which a local problem becomes a global one."}
    w["de_10y"] = safe_fetch("de_10y", _monthly_yield("IRLTLT01DEM156N", "Germany"), {"value": None, "unit": "%"})
    w["fr_10y"] = safe_fetch("fr_10y", _monthly_yield("IRLTLT01FRM156N", "France"), {"value": None, "unit": "%"})
    w["it_10y"] = safe_fetch("it_10y", _monthly_yield("IRLTLT01ITM156N", "Italy"), {"value": None, "unit": "%"})
    w["uk_10y"] = safe_fetch("uk_10y", _monthly_yield("IRLTLT01GBM156N", "UK gilt"), {"value": None, "unit": "%"})
    for key, sid, lab in (("fx_cny", "DEXCHUS", "China yuan"), ("fx_krw", "DEXKOUS", "Korean won"),
                          ("fx_twd", "DEXTAUS", "Taiwan dollar"), ("fx_inr", "DEXINUS", "Indian rupee"),
                          ("fx_mxn", "DEXMXUS", "Mexican peso"), ("fx_brl", "DEXBZUS", "Brazilian real")):
        w[key] = safe_fetch(key, _fx_3m_move(sid, lab), {"value": None, "unit": "%"})
    w["em_corp_oas"] = safe_fetch("em_oas", _plain("BAMLEMCBPIOAS", "ICE BofA EM corporate OAS", "%"), {"value": None, "unit": "%"})
    w["nfci"] = safe_fetch("nfci", _plain("NFCI", "Chicago Fed National Financial Conditions Index, weekly; >0 = tighter than average", "", digits=3), {"value": None, "unit": ""})
    w["jobless_claims"] = safe_fetch("claims", _plain("ICSA", "US initial jobless claims, weekly", "claims"), {"value": None, "unit": "claims"})
    w["breakeven_10y"] = safe_fetch("be10", _plain("T10YIE", "10Y breakeven inflation", "%"), {"value": None, "unit": "%"})
    w["real_yield_10y"] = safe_fetch("tips10", _plain("DFII10", "10Y TIPS real yield", "%"), {"value": None, "unit": "%"})
    w["fed_balance_sheet"] = safe_fetch("walcl", _plain("WALCL", "Fed total assets, weekly", "USD bn", divisor=1000.0, digits=0), {"value": None, "unit": "USD bn"})

    # ---- Derived: sovereign spreads (computed from the monthly yields above) ----
    def spread(a, b, name, ids=""):
        """Spread of two monthly yields at their latest COMMON month (the two
        series publish with different lags, so never subtract mismatched dates)."""
        sid = {"fr_10y": "IRLTLT01FRM156N", "it_10y": "IRLTLT01ITM156N", "de_10y": "IRLTLT01DEM156N"}
        try:
            da = dict(fred_latest(sid[a], n=6)); db = dict(fred_latest(sid[b], n=6))
            common = sorted(set(da) & set(db))
            if not common:
                raise RuntimeError("no common month")
            d = common[-1]
            return {"value": round((float(da[d]) - float(db[d])) * 100), "unit": "bp", "as_of": d,
                    "source": f"derived: {name}, same-month match (monthly data — lags 1-2 months) [FRED:{ids}]",
                    "tier": "live-api", "error": None}
        except Exception as e:
            return {"value": None, "unit": "bp", "tier": "stale", "error": str(e), "source": name}
    w["oat_bund_spread"] = spread("fr_10y", "de_10y", "France 10Y − Germany 10Y", "IRLTLT01FRM156N, FRED:IRLTLT01DEM156N")
    w["btp_bund_spread"] = spread("it_10y", "de_10y", "Italy 10Y − Germany 10Y", "IRLTLT01ITM156N, FRED:IRLTLT01DEM156N")

    # ---- Derived: EM stress composite. Rule is explicit so it can be audited. ----
    #  +1 for each of 6 EM/Asia currencies that lost >5% vs USD in 3 months
    #  +1 if EM corporate OAS > 5.0%        => score 0..7
    flags, missing = 0, 0
    for k in ("fx_cny", "fx_krw", "fx_twd", "fx_inr", "fx_mxn", "fx_brl"):
        v = w[k].get("value")
        if v is None: missing += 1
        elif v > 5.0: flags += 1
    oas = w["em_corp_oas"].get("value")
    if oas is None: missing += 1
    elif oas > 5.0: flags += 1
    w["em_stress_score"] = {"value": flags, "unit": "/ 7 flags", "as_of": datetime.now(timezone.utc).date().isoformat(),
        "source": "derived: count of {CNY,KRW,TWD,INR,MXN,BRL} down >5% vs USD over 3M, plus EM corp OAS >5% [FRED:DEXCHUS, FRED:DEXKOUS, FRED:DEXTAUS, FRED:DEXINUS, FRED:DEXMXUS, FRED:DEXBZUS, FRED:BAMLEMCBPIOAS]",
        "tier": "live-api" if missing == 0 else "stale", "error": None if missing == 0 else f"{missing} input(s) missing"}
    return w


# ---------------------------------------------------------------------------
# Cross-asset long history (FRED) -> history_us.json, for the dropdown chart.
# ---------------------------------------------------------------------------
HISTORY_SPECS = [
    # key, FRED id, label, unit, transform
    ("ust_10y", "DGS10", "US 10Y Treasury yield", "%", None),
    ("ust_2s10s", "T10Y2Y", "US 2s10s yield curve (10Y − 2Y)", "pp", None),
    ("fed_funds", "FEDFUNDS", "Fed Funds rate", "%", None),
    ("us_hy_credit_spread", "BAMLH0A0HYM2", "US High Yield credit spread", "%", None),
    ("vix", "VIXCLS", "VIX", "", None),
    ("usdjpy", "DEXJPUS", "USD/JPY", "JPY", None),
    ("wti_oil", "DCOILWTICO", "WTI crude oil", "USD/bbl", None),
    ("dxy", "DTWEXBGS", "Dollar index (broad, trade-weighted)", "", None),
    ("cpi_yoy", "CPIAUCSL", "US CPI inflation (YoY)", "%", "yoy"),
    ("unemployment", "UNRATE", "US unemployment rate", "%", None),
    ("nfci", "NFCI", "Financial conditions (NFCI, >0 = tight)", "", None),
    ("em_corp_oas", "BAMLEMCBPIOAS", "EM corporate credit spread", "%", None),
    ("de_10y", "IRLTLT01DEM156N", "Germany 10Y Bund yield (monthly)", "%", None),
    ("fr_10y", "IRLTLT01FRM156N", "France 10Y OAT yield (monthly)", "%", None),
    ("it_10y", "IRLTLT01ITM156N", "Italy 10Y BTP yield (monthly)", "%", None),
    ("uk_10y", "IRLTLT01GBM156N", "UK 10Y gilt yield (monthly)", "%", None),
]


def fred_full(series_id):
    url = (f"{FRED_BASE}?series_id={series_id}&api_key={FRED_API_KEY}"
           f"&file_type=json&sort_order=asc&observation_start=1950-01-01")
    obs = json.loads(http_get(url, timeout=60))["observations"]
    return [(o["date"], float(o["value"])) for o in obs if o["value"] != "."]


def _weekly(points):
    """Keep the last observation of each ISO week (shrinks daily series ~5x)."""
    out, last_key = [], None
    for d, v in points:
        y, w, _ = datetime.fromisoformat(d).isocalendar()
        key = (y, w)
        if key == last_key:
            out[-1] = (d, v)
        else:
            out.append((d, v)); last_key = key
    return out


def build_us_history():
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY not set")
    series = {}
    for key, sid, label, unit, tf in HISTORY_SPECS:
        try:
            pts = fred_full(sid)
            if tf == "yoy":
                pts = [(pts[i][0], round((pts[i][1] / pts[i - 12][1] - 1) * 100, 2)) for i in range(12, len(pts))]
            elif len(pts) > 5000:
                pts = _weekly(pts)
            series[key] = {"label": label, "unit": unit, "source": f"FRED:{sid}",
                           "data": [[d, round(v, 4)] for d, v in pts]}
        except Exception as e:
            print(f"[warn] history {key} ({sid}) failed: {e}", file=sys.stderr)
    # Derived sovereign spreads (bp), from the monthly yields above
    def _spread(a, b, key, label, src):
        if a in series and b in series:
            bd = dict((d, v) for d, v in series[b]["data"])
            data = [[d, round((v - bd[d]) * 100)] for d, v in series[a]["data"] if d in bd]
            if data:
                series[key] = {"label": label, "unit": "bp", "source": src, "data": data}
    _spread("fr_10y", "de_10y", "oat_bund_spread", "France–Germany spread (OAT–Bund, monthly)", "FRED:IRLTLT01FRM156N − FRED:IRLTLT01DEM156N")
    _spread("it_10y", "de_10y", "btp_bund_spread", "Italy–Germany spread (BTP–Bund, monthly)", "FRED:IRLTLT01ITM156N − FRED:IRLTLT01DEM156N")
    # NBER recession periods from USREC (monthly 0/1)
    recessions = []
    try:
        rec = fred_full("USREC")
        start = None
        for d, v in rec:
            if v == 1 and start is None: start = d
            if v == 0 and start is not None:
                recessions.append([start, d]); start = None
        if start is not None: recessions.append([start, rec[-1][0]])
    except Exception as e:
        print(f"[warn] recession bands failed: {e}", file=sys.stderr)
    if not series:
        raise RuntimeError("no history series fetched")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "series": series, "recessions": recessions}


def main():
    now = datetime.now(timezone.utc).isoformat()

    data = {
        "generated_at": now,
        "us_global": {
            "fed_funds_rate": safe_fetch("fed_funds", lambda: get_fed_funds(), {"value": None, "unit": "%"}),
            "ust_10y": safe_fetch("ust_10y", get_ust("DGS10", "10Y"), {"value": None, "unit": "%"}),
            "ust_30y": safe_fetch("ust_30y", get_ust("DGS30", "30Y"), {"value": None, "unit": "%"}),
            "ust_2y": safe_fetch("ust_2y", get_ust("DGS2", "2Y"), {"value": None, "unit": "%"}),
            "cpi_yoy": safe_fetch("cpi", lambda: get_cpi_yoy(), {"value": None, "unit": "% YoY"}),
            "unemployment": safe_fetch("unemployment", lambda: get_unemployment(), {"value": None, "unit": "%"}),
            "debt_to_gdp": safe_fetch("debt_gdp", lambda: get_debt_gdp(), {"value": None, "unit": "%"}),
            "vix": safe_fetch("vix", lambda: get_vix(), {"value": None, "unit": ""}),
            "buffett_indicator": safe_fetch("buffett", lambda: get_buffett_indicator(), {"value": None, "unit": "%"}),
            "shiller_cape": safe_fetch("cape", lambda: get_cape(), {"value": None, "unit": "x"}),
            "bitcoin": safe_fetch("btc", lambda: get_btc(), {"value": None, "unit": "USD"}),
            "gold": safe_fetch("gold", lambda: get_gold(), {"value": None, "unit": "USD/oz"}),
        },
        "japan": {
            "jgb_curve": safe_fetch("jgb", lambda: get_jgb_curve(), {"value": None, "curve": {}, "unit": "%"}),
            "usdjpy": safe_fetch("usdjpy", lambda: get_usdjpy(), {"value": None, "unit": "JPY per USD"}),
        },
        "world": build_world(),
        "global_systemic": {
            "_purpose": "Cross-asset gauges that matter regardless of which country is the proximate story — dollar liquidity, credit stress beyond equity vol, and the energy channel that feeds into every inflation print.",
            "dxy": safe_fetch("dxy", lambda: get_dxy(), {"value": None, "unit": ""}),
            "us_hy_credit_spread": safe_fetch("hy_spread", lambda: get_hy_credit_spread(), {"value": None, "unit": "%"}),
            "wti_oil": safe_fetch("oil", lambda: get_oil(), {"value": None, "unit": "USD/bbl"}),
            "rrp": safe_fetch("rrp", lambda: get_rrp(), {"value": None, "unit": "USD bn"}),
        },
    }

    # Merge in the hand-maintained fields (BOJ rate, debt-service ratio,
    # intervention log, fiscal calendar) — these are event-driven, not
    # daily-live, and are edited directly in manual.json when something
    # actually happens.
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "manual.json")) as f:
            manual = json.load(f)
    except FileNotFoundError:
        manual = {}

    data["manual"] = manual

    # Preserve last-good values for anything that came back stale, by
    # merging against the previous data.json if present.
    out_path = os.path.join(os.path.dirname(__file__), "..", "data.json")
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                prev = json.load(f)
            for section in ("us_global", "japan", "global_systemic", "world"):
                for k, v in data.get(section, {}).items():
                    if not isinstance(v, dict):
                        continue  # skip non-metric entries like "_purpose" descriptive strings
                    if v.get("tier") == "stale" and section in prev and isinstance(prev.get(section), dict) and k in prev[section]:
                        prev_entry = prev[section][k]
                        if isinstance(prev_entry, dict) and prev_entry.get("value") is not None:
                            data[section][k] = {**prev_entry, "tier": "stale", "error": v.get("error")}
        except Exception as e:
            # Never let the stale-preservation step block writing fresh data.
            print(f"[warn] stale-value merge skipped due to error: {e}", file=sys.stderr)

    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Wrote {out_path}")

    # ---- JGB full history (separate file, only refetched successfully — ----
    # ---- on failure, leave whatever history file already exists alone) ----
    history_path = os.path.join(os.path.dirname(__file__), "..", "jgb_history.json")
    try:
        history = get_jgb_history()
        with open(history_path, "w") as f:
            json.dump(history, f, separators=(",", ":"))  # compact — this file can get large
        print(f"Wrote {history_path} ({len(history['10y'])} points)")
    except Exception as e:
        print(f"[warn] jgb_history fetch failed, leaving existing file untouched: {e}", file=sys.stderr)
        import traceback
        status_path = os.path.join(os.path.dirname(__file__), "..", "jgb_history_error.txt")
        with open(status_path, "w") as f:
            f.write(traceback.format_exc())


    # ---- Cross-asset history (FRED) ----
    try:
        us_hist = build_us_history()
        with open(os.path.join(os.path.dirname(__file__), "..", "history_us.json"), "w") as f:
            json.dump(us_hist, f, separators=(",", ":"))
        print(f"Wrote history_us.json ({len(us_hist['series'])} series)")
    except Exception as e:
        print(f"[warn] history_us failed, leaving existing file untouched: {e}", file=sys.stderr)
        import traceback
        with open(os.path.join(os.path.dirname(__file__), "..", "history_us_error.txt"), "w") as f:
            f.write(traceback.format_exc())

if __name__ == "__main__":
    main()
