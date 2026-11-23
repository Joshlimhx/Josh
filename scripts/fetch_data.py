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


def _shift_month(ym, k):
    """'YYYY-MM' shifted by k months."""
    y, m = int(ym[:4]), int(ym[5:7]) - 1 + k
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def yoy_at(obs_map, ym):
    """YoY % for month `ym` from a {'YYYY-MM': value} map, matched BY DATE, not by
    position. If the year-ago month is missing (e.g. US Oct-2025 CPI was never
    published because of the federal shutdown) the base is the geometric mean of
    the neighbouring months and the result is flagged. Returns (yoy, flag) or None."""
    if ym not in obs_map:
        return None
    base_ym = _shift_month(ym, -12)
    if base_ym in obs_map:
        return (obs_map[ym] / obs_map[base_ym] - 1) * 100, None
    lo, hi = _shift_month(base_ym, -1), _shift_month(base_ym, 1)
    if lo in obs_map and hi in obs_map:
        base = (obs_map[lo] * obs_map[hi]) ** 0.5
        return (obs_map[ym] / base - 1) * 100, f"year-ago month {base_ym} missing at source; base interpolated from {lo} & {hi}"
    return None


def get_cpi_yoy():
    obs = fred_latest("CPIAUCSL", n=30)
    om = {d[:7]: float(v) for d, v in obs}
    latest_ym = max(om)
    r = yoy_at(om, latest_ym)
    if r is None:
        raise RuntimeError("could not locate year-ago CPI print")
    yoy, flag = r
    out = {"value": round(yoy, 2), "unit": "% YoY", "as_of": latest_ym + "-01", "source": "FRED:CPIAUCSL"}
    if flag:
        out["source"] += f" — NOTE: {flag}"
    prev = yoy_at(om, _shift_month(latest_ym, -3))
    if prev:
        out["yoy_3m_ago"] = round(prev[0], 2)
    return out


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


def get_net_liquidity():
    """Fed net liquidity = Fed total assets − Treasury General Account − overnight RRP.
    The practitioner proxy for how much central-bank cash is actually circulating in
    markets. Matched on the weekly (Wednesday) H.4.1 date; RRP taken on/just before it."""
    walcl = dict(fred_latest("WALCL", n=20))          # USD mn
    tga = dict(fred_latest("WTREGEN", n=20))          # USD mn
    rrp = sorted(fred_latest("RRPONTSYD", n=140))      # USD bn, daily, ascending
    weeks = sorted(set(walcl) & set(tga))
    if not weeks:
        raise RuntimeError("no common WALCL/WTREGEN week")

    def rrp_on(d):
        v = None
        for rd, rv in rrp:
            if rd <= d: v = float(rv)
            else: break
        return v

    def nl(d):
        r = rrp_on(d)
        return None if r is None else float(walcl[d]) / 1000 - float(tga[d]) / 1000 - r
    d0 = weeks[-1]; v0 = nl(d0)
    if v0 is None:
        raise RuntimeError("no RRP reading on/before the H.4.1 date")
    target = (datetime.fromisoformat(d0) - timedelta(days=91)).date().isoformat()
    old = [d for d in weeks if d <= target]
    drain = None
    if old and nl(old[-1]) is not None:
        drain = round(-(v0 - nl(old[-1])))
    return {"value": round(v0), "unit": "USD bn", "as_of": d0, "drain_13w": drain,
            "source": "derived: Fed total assets − TGA − overnight RRP, weekly H.4.1 date [FRED:WALCL, FRED:WTREGEN, FRED:RRPONTSYD]"}


FMCG_INPUTS = [
    # key, FRED id, label
    ("sugar", "PSUGAISAUSDM", "Sugar (No. 11)"),
    ("wheat", "PWHEAMTUSDM", "Wheat"),
    ("cocoa", "PCOCOUSDM", "Cocoa"),
    ("coffee", "PCOFFOTMUSDM", "Coffee (arabica)"),
    ("palm_oil", "PPOILUSDM", "Palm oil"),
]


def build_fmcg_costs():
    """Input-cost pressure for a Malaysian FMCG P&L: world price YoY in USD, then
    converted into RINGGIT terms using the monthly-average USD/MYR, because that is
    the cost the company actually books."""
    out = {"_purpose": "What the commodity cycle does to a Malaysian FMCG company's gross margin: each input's price change over 12 months in RINGGIT terms (USD price move × ringgit move). Monthly IMF data — lags 1–2 months."}
    fx = {}
    try:
        daily = fred_latest("DEXMAUS", n=700)
        acc = {}
        for d, v in daily:
            acc.setdefault(d[:7], []).append(float(v))
        fx = {m: sum(v) / len(v) for m, v in acc.items() if len(v) >= 10}
    except Exception as e:
        print(f"[warn] fmcg fx fetch failed: {e}", file=sys.stderr)
    hits, missing = 0, 0
    for key, sid, label in FMCG_INPUTS:
        def fn(sid=sid, label=label):
            obs = fred_latest(sid, n=26)
            om = {d[:7]: float(v) for d, v in obs}
            ym = max(om)
            r = yoy_at(om, ym)
            if r is None:
                raise RuntimeError(f"{sid}: no year-ago month")
            usd_yoy = r[0]
            base = _shift_month(ym, -12)
            if ym in fx and base in fx:
                fx_yoy = (fx[ym] / fx[base] - 1) * 100
                myr_yoy = ((1 + usd_yoy / 100) * (1 + fx_yoy / 100) - 1) * 100
                note = f"USD price {usd_yoy:+.1f}% · ringgit effect {fx_yoy:+.1f}% (+ = weaker MYR)"
            else:
                fx_yoy, myr_yoy, note = None, usd_yoy, "MYR conversion unavailable — showing USD YoY"
            return {"value": round(myr_yoy, 1), "unit": "% YoY in MYR", "usd_yoy": round(usd_yoy, 1),
                    "fx_effect": None if fx_yoy is None else round(fx_yoy, 1), "level_usd": om[ym],
                    "as_of": ym + "-01", "source": f"FRED:{sid} (IMF {label}, monthly) × FRED:DEXMAUS monthly avg — {note}"}
        out[key] = safe_fetch("fmcg_" + key, fn, {"value": None, "unit": "% YoY in MYR"})
        v = out[key].get("value")
        if v is None: missing += 1
        elif v > 15: hits += 1
    out["pressure_score"] = {"value": hits, "unit": f"/ {len(FMCG_INPUTS)} inputs up >15% in MYR",
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "source": "derived: count of sugar, wheat, cocoa, coffee, palm oil whose 12M price change in ringgit terms exceeds +15%",
        "tier": "live-api" if missing == 0 else "stale", "error": None if missing == 0 else f"{missing} input(s) missing"}
    return out


def get_klci_drawdown():
    raw = http_get("https://query1.finance.yahoo.com/v8/finance/chart/%5EKLSE?range=1y&interval=1d", timeout=25)
    r = json.loads(raw)["chart"]["result"][0]
    closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
    if len(closes) < 120:
        raise RuntimeError("KLCI: under 120 daily closes")
    last, high = closes[-1], max(closes)
    return {"value": round((high - last) / high * 100, 2), "unit": "% below 1Y high", "level": round(last, 2),
            "as_of": datetime.fromtimestamp(r["timestamp"][-1], timezone.utc).date().isoformat(),
            "source": "Yahoo Finance ^KLSE 1Y daily closes — best-effort, unofficial; drawdown from trailing-1-year high"}


def build_decision(data):
    """The 'so what' layer. Every rule is written out so it can be audited and
    changed; these are heuristics (evidence tier: Josh's own rules, NOT backtested)."""
    def v(sec, key, field="value"):
        e = data.get(sec, {}).get(key, {})
        return e.get(field) if isinstance(e, dict) else None

    def m1(sec, key):
        e = data.get(sec, {}).get(key, {})
        c = (e.get("changes") or {}).get("m1") if isinstance(e, dict) else None
        return c

    out = {"_purpose": "Turns the indicators into three answers: what regime are we in, is liquidity helping or hurting, and is the Malaysian ECM window open. Rules are explicit and editable in fetch_data.py → build_decision().",
           "evidence_tier": "Heuristic — analyst-defined rules, not backtested. Use as a structured checklist, not a signal."}

    # --- 1. Growth direction (US, because it sets the global cycle) ---
    g_votes, g_why = 0, []
    sahm = v("world", "sahm_rule")
    if sahm is not None:
        if sahm < 0.3: g_votes += 1; g_why.append(f"Sahm {sahm} < 0.30 (+1)")
        else: g_votes -= 1; g_why.append(f"Sahm {sahm} ≥ 0.30 (−1)")
    cl = m1("world", "jobless_claims")
    if cl and cl.get("pct") is not None:
        if cl["pct"] <= 5: g_votes += 1; g_why.append(f"claims 1M {cl['pct']:+.1f}% ≤ +5% (+1)")
        else: g_votes -= 1; g_why.append(f"claims 1M {cl['pct']:+.1f}% > +5% (−1)")
    nfci = v("world", "nfci")
    if nfci is not None:
        if nfci < 0: g_votes += 1; g_why.append(f"NFCI {nfci} < 0, loose (+1)")
        else: g_votes -= 1; g_why.append(f"NFCI {nfci} ≥ 0, tight (−1)")
    c3 = v("world", "curve_3m10y")
    if c3 is not None:
        if c3 > 0: g_votes += 1; g_why.append(f"3m10y {c3} > 0 (+1)")
        else: g_votes -= 1; g_why.append(f"3m10y {c3} inverted (−1)")
    growth = "up" if g_votes > 0 else "down" if g_votes < 0 else "flat"

    # --- 2. Inflation direction ---
    i_votes, i_why = 0, []
    cpi, cpi3 = v("us_global", "cpi_yoy"), v("us_global", "cpi_yoy", "yoy_3m_ago")
    if cpi is not None and cpi3 is not None:
        d = cpi - cpi3
        # realised CPI counts double: market expectations (breakevens, oil) can't overrule an actual print
        if d > 0.15: i_votes += 2; i_why.append(f"CPI {cpi}% vs {cpi3}% 3M ago, rising (+2, realised data counts double)")
        elif d < -0.15: i_votes -= 2; i_why.append(f"CPI {cpi}% vs {cpi3}% 3M ago, falling (−2, realised data counts double)")
        else: i_why.append(f"CPI {cpi}% vs {cpi3}% 3M ago, flat (0)")
    be = m1("world", "breakeven_10y")
    if be and be.get("abs") is not None:
        if be["abs"] > 0.05: i_votes += 1; i_why.append(f"10Y breakeven 1M {be['abs']:+.2f}pp (+1)")
        elif be["abs"] < -0.05: i_votes -= 1; i_why.append(f"10Y breakeven 1M {be['abs']:+.2f}pp (−1)")
        else: i_why.append(f"10Y breakeven 1M {be['abs']:+.2f}pp (0)")
    oil = m1("global_systemic", "wti_oil")
    if oil and oil.get("pct") is not None:
        if oil["pct"] > 10: i_votes += 1; i_why.append(f"WTI 1M {oil['pct']:+.1f}% (+1)")
        elif oil["pct"] < -10: i_votes -= 1; i_why.append(f"WTI 1M {oil['pct']:+.1f}% (−1)")
    inflation = "up" if i_votes > 0 else "down" if i_votes < 0 else "flat"

    names = {("up", "down"): "Goldilocks — growth up, inflation down",
             ("up", "up"): "Reflation / overheating — growth up, inflation up",
             ("down", "up"): "Stagflation — growth down, inflation up",
             ("down", "down"): "Disinflationary slowdown — growth down, inflation down"}
    regime = names.get((growth, inflation), f"Transition — growth {growth}, inflation {inflation}")
    out["regime"] = {"growth": growth, "growth_score": g_votes, "growth_why": g_why,
                     "inflation": inflation, "inflation_score": i_votes, "inflation_why": i_why, "label": regime}

    # --- 3. Liquidity direction ---
    l_votes, l_why = 0, []
    dr = v("world", "net_liquidity_drain_13w")
    if dr is not None:
        if dr < -100: l_votes += 1; l_why.append(f"net liquidity +{-dr}bn over 13W (+1)")
        elif dr > 100: l_votes -= 1; l_why.append(f"net liquidity −{dr}bn over 13W (−1)")
        else: l_why.append(f"net liquidity {-dr:+}bn over 13W, flat (0)")
    ry = m1("world", "real_yield_10y")
    if ry and ry.get("abs") is not None:
        if ry["abs"] < -0.15: l_votes += 1; l_why.append(f"real yield 1M {ry['abs']:+.2f}pp (+1)")
        elif ry["abs"] > 0.15: l_votes -= 1; l_why.append(f"real yield 1M {ry['abs']:+.2f}pp (−1)")
        else: l_why.append(f"real yield 1M {ry['abs']:+.2f}pp (0)")
    dx = m1("global_systemic", "dxy")
    if dx and dx.get("pct") is not None:
        if dx["pct"] < -1.5: l_votes += 1; l_why.append(f"broad USD 1M {dx['pct']:+.1f}% (+1)")
        elif dx["pct"] > 1.5: l_votes -= 1; l_why.append(f"broad USD 1M {dx['pct']:+.1f}% (−1)")
    out["liquidity"] = {"direction": "tailwind" if l_votes > 0 else "headwind" if l_votes < 0 else "neutral",
                        "score": l_votes, "why": l_why}

    # --- 4. Malaysia ECM / IPO window ---
    flags, checks = 0, []
    def chk(name, val, bad, rule):
        nonlocal flags
        if val is None:
            checks.append({"name": name, "value": None, "bad": None, "rule": rule}); return
        b = bad(val); flags += 1 if b else 0
        checks.append({"name": name, "value": val, "bad": b, "rule": rule})
    chk("VIX", v("us_global", "vix"), lambda x: x > 22, "> 22 = global risk appetite too weak")
    chk("KLCI drawdown from 1Y high", v("malaysia", "klci_drawdown"), lambda x: x > 10, "> 10% = local buyers nursing losses")
    chk("Ringgit 3M move", v("malaysia", "myr_3m_move"), lambda x: x > 3, "> +3% weaker = foreign money leaving")
    chk("EM stress score", v("world", "em_stress_score"), lambda x: x >= 3, "≥ 3 flags = EM risk-off")
    chk("US HY credit spread", v("global_systemic", "us_hy_credit_spread"), lambda x: x > 4.5, "> 4.5% = risk capital repricing")
    chk("Net liquidity drain (13W)", dr, lambda x: x > 200, "> USD 200bn drained = liquidity headwind")
    known = sum(1 for c in checks if c["bad"] is not None)
    state = "OPEN" if flags <= 1 else "CAUTION" if flags <= 3 else "SHUT"
    out["ipo_window"] = {"state": state, "flags": flags, "of": known, "checks": checks,
                         "note": "Market-conditions only. It does NOT see the deal pipeline (recent Bursa IPO first-day performance, oversubscription, cornerstone appetite) — check those in manual.json → my_ipo_activity before advising a client on timing."}
    return out


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
    w["tga"] = safe_fetch("tga", _plain("WTREGEN", "US Treasury General Account at the Fed, week average", "USD bn", divisor=1000.0, digits=0), {"value": None, "unit": "USD bn"})
    w["bank_reserves"] = safe_fetch("wresbal", _plain("WRESBAL", "Bank reserve balances at the Fed, week average", "USD bn", divisor=1000.0, digits=0), {"value": None, "unit": "USD bn"})
    w["net_liquidity"] = safe_fetch("netliq", get_net_liquidity, {"value": None, "unit": "USD bn"})
    nl = w["net_liquidity"]
    w["net_liquidity_drain_13w"] = {"value": nl.get("drain_13w"), "unit": "USD bn (+ = draining, 13W)", "as_of": nl.get("as_of"),
        "source": "derived from net liquidity: −(today − 13 weeks ago) [FRED:WALCL, FRED:WTREGEN, FRED:RRPONTSYD]",
        "tier": nl.get("tier") if nl.get("drain_13w") is not None else "stale", "error": nl.get("error")}
    w["curve_3m10y"] = safe_fetch("t10y3m", _plain("T10Y3M", "10Y minus 3M Treasury — the Fed's preferred curve recession signal", "pp"), {"value": None, "unit": "pp"})
    w["sahm_rule"] = safe_fetch("sahm", _plain("SAHMREALTIME", "Real-time Sahm rule: 3M avg unemployment vs its 12M low; ≥0.50 = recession has likely begun", "pp"), {"value": None, "unit": "pp"})
    w["us_ig_credit_spread"] = safe_fetch("ig_oas", _plain("BAMLC0A0CM", "ICE BofA US Corporate (investment-grade) OAS", "%"), {"value": None, "unit": "%"})

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
    ("usd_myr", "DEXMAUS", "USD/MYR (ringgit per dollar)", "MYR", None),
    ("brent", "DCOILBRENTEU", "Brent crude oil", "USD/bbl", None),
    ("palm_oil", "PPOILUSDM", "Palm oil price (monthly)", "USD/mt", None),
    ("ez_hicp_yoy", "CP0000EZ19M086NEST", "Euro-area inflation (HICP, YoY)", "%", "yoy"),
    ("food_price_yoy", "PFOODINDEXM", "Global food prices (IMF index, YoY)", "%", "yoy"),
    ("us_gasoline", "GASREGW", "US regular gasoline", "USD/gal", None),
    ("global_epu", "GEPUCURRENT", "Global economic policy uncertainty", "index", None),
    ("sp500", "SP500", "S&P 500", "pts", None),
    ("copper", "PCOPPUSDM", "Copper price (monthly)", "USD/mt", None),
    ("ust_3m10y", "T10Y3M", "US 3m10y yield curve (10Y − 3M)", "pp", None),
    ("sahm", "SAHMREALTIME", "Sahm rule recession indicator (real-time)", "pp", None),
    ("us_ig_credit_spread", "BAMLC0A0CM", "US Investment-Grade credit spread", "%", None),
    ("real_yield_10y", "DFII10", "US 10Y real yield (TIPS)", "%", None),
    ("bank_reserves", "WRESBAL", "Bank reserves at the Fed (USD mn)", "USD mn", None),
    ("sugar_yoy", "PSUGAISAUSDM", "Sugar price (YoY, USD)", "%", "yoy"),
    ("wheat_yoy", "PWHEAMTUSDM", "Wheat price (YoY, USD)", "%", "yoy"),
    ("cocoa_yoy", "PCOCOUSDM", "Cocoa price (YoY, USD)", "%", "yoy"),
    ("coffee_yoy", "PCOFFOTMUSDM", "Coffee (arabica) price (YoY, USD)", "%", "yoy"),
    ("palm_oil_yoy", "PPOILUSDM", "Palm oil price (YoY, USD)", "%", "yoy"),
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
                om = {d[:7]: v for d, v in pts}
                pts = [(d, round(r[0], 2)) for d, v in pts if (r := yoy_at(om, d[:7]))]
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


# ---------------------------------------------------------------------------
# MALAYSIA — BNM public API + OpenDOSM (data.gov.my) + FRED + Yahoo (KLCI)
# ---------------------------------------------------------------------------
BNM_HEADERS = {"Accept": "application/vnd.BNM.API.v1+json"}
DOSM = "https://api.data.gov.my/data-catalogue"


def get_my_opr():
    j = json.loads(http_get("https://api.bnm.gov.my/public/opr", headers=BNM_HEADERS, timeout=30))
    d = j["data"]
    return {"value": float(d["new_opr_level"]), "unit": "%", "as_of": d.get("date"),
            "change_at_last_decision": d.get("change_in_opr"),
            "source": "BNM public API (api.bnm.gov.my) — Overnight Policy Rate, date = last decision that set this level"}


def _dosm_rows(dataset, limit=30):
    j = json.loads(http_get(f"{DOSM}?id={dataset}&sort=-date&limit={limit}", timeout=30))
    if isinstance(j, dict) and "data" in j:
        j = j["data"]
    return j


def get_my_cpi():
    # cpi_headline has one row per month PER division, so ask only for the overall
    # series (data.gov.my filter syntax: value@column) and fetch >12 months.
    rows = None
    for q in (f"{DOSM}?id=cpi_headline&filter=overall@division&sort=-date&limit=30",
              f"{DOSM}?id=cpi_headline&sort=-date&limit=1500"):
        try:
            j = json.loads(http_get(q, timeout=40))
            r = j["data"] if isinstance(j, dict) and "data" in j else j
            if "division" in (r[0] if r else {}):
                r = [x for x in r if str(x.get("division")).lower() in ("overall", "all", "00", "total")]
            if r and len({x["date"] for x in r}) >= 13:
                rows = r; break
        except Exception as e:
            print(f"[warn] cpi query failed: {e}", file=sys.stderr)
    if not rows:
        raise RuntimeError("cpi_headline: could not get >=13 months of overall CPI rows")
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    latest = rows[0]
    for k in ("inflation_yoy", "yoy", "growth_yoy", "cpi_yoy"):
        if latest.get(k) is not None:
            return {"value": round(float(latest[k]), 2), "unit": "% YoY", "as_of": latest["date"],
                    "source": "DOSM via data.gov.my (cpi_headline)"}
    idx_key = next((k for k in ("index", "cpi") if latest.get(k) is not None), None)
    if not idx_key:
        raise RuntimeError(f"cpi_headline: unknown columns {list(latest.keys())}")
    y, mth = latest["date"][:4], latest["date"][5:7]
    prev = next((r for r in rows if r["date"][:4] == str(int(y) - 1) and r["date"][5:7] == mth), None)
    if not prev:
        raise RuntimeError(f"cpi_headline: no year-ago row; dates seen {sorted({r['date'] for r in rows})[:3]}..")
    return {"value": round((float(latest[idx_key]) / float(prev[idx_key]) - 1) * 100, 2), "unit": "% YoY", "as_of": latest["date"],
            "source": "DOSM via data.gov.my (cpi_headline index, YoY computed)"}


def get_my_unemployment():
    rows = sorted(_dosm_rows("lfs_month", 6), key=lambda r: r["date"], reverse=True)
    latest = rows[0]
    k = next((k for k in ("u_rate", "unemployment_rate") if latest.get(k) is not None), None)
    if not k:
        raise RuntimeError(f"lfs_month: unknown columns {list(latest.keys())}")
    return {"value": round(float(latest[k]), 2), "unit": "%", "as_of": latest["date"],
            "source": "DOSM via data.gov.my (lfs_month, Labour Force Survey)"}


def get_klci():
    raw = http_get("https://query1.finance.yahoo.com/v8/finance/chart/%5EKLSE?range=5d&interval=1d", timeout=20)
    m = json.loads(raw)["chart"]["result"][0]["meta"]
    px = m["regularMarketPrice"]
    return {"value": round(float(px), 2), "unit": "pts", "as_of": datetime.fromtimestamp(m["regularMarketTime"], timezone.utc).date().isoformat(),
            "source": "Yahoo Finance ^KLSE (FBM KLCI) — best-effort, unofficial; Bursa Malaysia is the official source"}


def get_my_gdp():
    j = json.loads(http_get(f"{DOSM}?id=gdp_qtr_real&filter=growth_yoy@series&sort=-date&limit=2", timeout=30))
    rows = j["data"] if isinstance(j, dict) and "data" in j else j
    r = sorted(rows, key=lambda x: x["date"], reverse=True)[0]
    return {"value": round(float(r["value"]), 2), "unit": "% YoY (real, quarterly)", "as_of": r["date"],
            "source": "DOSM via data.gov.my (gdp_qtr_real, growth_yoy) — date = start of the quarter"}


def get_my_interbank_3m():
    j = json.loads(http_get("https://api.bnm.gov.my/public/interest-rate", headers=BNM_HEADERS, timeout=30))
    row = next(r for r in j["data"] if r.get("product") == "interbank")
    for tenor, label in (("3_month", "3-month"), ("1_month", "1-month (3M not published today)"), ("1_week", "1-week (longer tenors not published today)")):
        if row.get(tenor) is not None:
            return {"value": float(row[tenor]), "unit": "%", "as_of": row["date"], "overnight": row.get("overnight"),
                    "source": f"BNM public API (api.bnm.gov.my) — interbank {label} rate; compare with the OPR for funding-cost stress"}
    raise RuntimeError("no interbank tenor published")


def build_malaysia():
    m = {"_purpose": "Malaysia: policy rate, inflation, jobs, ringgit, KLCI, and the commodity prices (crude, palm oil) the economy and fiscal accounts are most exposed to."}
    m["opr"] = safe_fetch("my_opr", get_my_opr, {"value": 2.75, "unit": "%", "as_of": "seed: held at 2.75% per press reports (Sep 2026) — NOT verified from BNM", "source": "BNM — seed value, live fetch failed"})
    m["cpi_yoy"] = safe_fetch("my_cpi", get_my_cpi, {"value": None, "unit": "% YoY"})
    m["unemployment"] = safe_fetch("my_unemp", get_my_unemployment, {"value": None, "unit": "%"})
    m["gdp_yoy"] = safe_fetch("my_gdp", get_my_gdp, {"value": None, "unit": "% YoY"})
    m["interbank_3m"] = safe_fetch("my_ib3m", get_my_interbank_3m, {"value": None, "unit": "%"})
    m["usd_myr"] = safe_fetch("usd_myr", _plain("DEXMAUS", "Malaysian ringgit per USD, daily (Fed H.10)", "MYR per USD", digits=4), {"value": None, "unit": "MYR per USD"})
    m["myr_3m_move"] = safe_fetch("myr_3m", _fx_3m_move("DEXMAUS", "Malaysian ringgit"), {"value": None, "unit": "%"})
    m["klci"] = safe_fetch("klci", get_klci, {"value": None, "unit": "pts"})
    m["klci_drawdown"] = safe_fetch("klci_dd", get_klci_drawdown, {"value": None, "unit": "% below 1Y high"})
    m["brent"] = safe_fetch("brent", _plain("DCOILBRENTEU", "Brent crude spot, daily", "USD/bbl"), {"value": None, "unit": "USD/bbl"})
    m["palm_oil"] = safe_fetch("palm", _plain("PPOILUSDM", "Global price of palm oil, monthly (IMF)", "USD/mt"), {"value": None, "unit": "USD/mt"})
    return m


# ---------------------------------------------------------------------------
# SOCIAL & GEOPOLITICAL STRESS — cost of living, uncertainty, unrest intensity
# ---------------------------------------------------------------------------

def _yoy(series_id, label, unit="% YoY", n=16):
    def fn():
        obs = fred_latest(series_id, n=max(n, 26))
        om = {d[:7]: float(v) for d, v in obs}
        r = yoy_at(om, max(om))
        if r is None:
            raise RuntimeError(f"{series_id}: no year-ago observation")
        src = f"FRED:{series_id} ({label}; YoY computed, date-matched)"
        if r[1]:
            src += f" — NOTE: {r[1]}"
        return {"value": round(r[0], 2), "unit": unit, "as_of": max(om) + "-01", "source": src}
    return fn


def _eurostat_youth(geo, label):
    def fn():
        url = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_m"
               f"?format=JSON&lang=EN&geo={geo}&age=Y_LT25&sex=T&unit=PC_ACT&s_adj=SA&lastTimePeriod=6")
        j = json.loads(http_get(url, timeout=40))
        idx = j["dimension"]["time"]["category"]["index"]          # {"2026-08": 0, ...}
        vals = j["value"]                                            # {"0": 17.9, ...}
        have = [(t, vals[str(i)]) for t, i in idx.items() if str(i) in vals and vals[str(i)] is not None]
        if not have:
            raise RuntimeError(f"Eurostat une_rt_m {geo}: no values")
        t, v = sorted(have)[-1]
        return {"value": round(float(v), 1), "unit": "%", "as_of": t + "-01",
                "source": f"Eurostat une_rt_m ({label} youth <25 unemployment rate, seasonally adjusted, monthly)"}
    return fn


def get_sp500_drawdown():
    obs = fred_latest("SP500", n=300)
    last = float(obs[0][1]); high = max(float(v) for _, v in obs[:252])
    return {"value": round((high - last) / high * 100, 2), "unit": "% below 1Y high", "level": last, "as_of": obs[0][0],
            "source": "FRED:SP500 (S&P 500 close; drawdown from trailing-1-year high)"}


def get_gscpi():
    import pandas as pd
    raw = http_get("https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx", timeout=60)
    sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None)
    best = None
    for name, df in sheets.items():
        for _, row in df.iterrows():
            vals = list(row.values)
            for i in range(len(vals) - 1):
                d = pd.to_datetime(vals[i], errors="coerce")
                v = vals[i + 1]
                if pd.notna(d) and isinstance(v, (int, float)) and v == v and d.year >= 2015:
                    if best is None or d > best[0]:
                        best = (d, float(v))
    if not best:
        raise RuntimeError(f"GSCPI: no dated numeric rows; sheets={list(sheets.keys())}")
    return {"value": round(best[1], 2), "unit": "std dev from avg", "as_of": best[0].date().isoformat(),
            "source": "NY Fed Global Supply Chain Pressure Index (gscpi_data.xlsx), 0 = average"}


def get_gpr():
    import pandas as pd
    raw = http_get("https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls", timeout=60)
    df = pd.read_excel(io.BytesIO(raw))
    col = next((c for c in df.columns if str(c).strip().upper() == "GPR"), None)
    dcol = next((c for c in df.columns if str(c).strip().lower() in ("month", "date")), df.columns[0])
    if col is None:
        raise RuntimeError(f"GPR column not found; columns={list(df.columns)[:8]}")
    d = df[[dcol, col]].dropna()
    last = d.iloc[-1]
    return {"value": round(float(last[col]), 1), "unit": "index (avg = 100)", "as_of": str(pd.to_datetime(last[dcol]).date()),
            "source": "Caldara & Iacoviello Geopolitical Risk Index (matteoiacoviello.com/gpr.htm), monthly"}


def get_gdelt_ratio(query, label):
    def fn():
        import urllib.parse
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=" + urllib.parse.quote(query) +
               "&mode=timelinevol&timespan=3months&format=json")
        j = None
        for wait in (10, 30, 60):                      # GDELT rate-limits shared IPs; back off and retry
            time.sleep(wait)
            try:
                j = json.loads(http_get(url, timeout=60)); break
            except urllib.error.HTTPError as e:
                if e.code != 429: raise
        if j is None:
            raise RuntimeError("GDELT rate-limited (HTTP 429) after 3 attempts")
        data = j["timeline"][0]["data"]
        vals = [float(x["value"]) for x in data if x.get("value") is not None]
        if len(vals) < 30:
            raise RuntimeError(f"GDELT: only {len(vals)} points")
        recent, base = sum(vals[-7:]) / 7, sum(vals) / len(vals)
        return {"value": round(recent / base, 2), "unit": "x of 3-month avg", "as_of": data[-1]["date"][:8][:4] + "-" + data[-1]["date"][4:6] + "-" + data[-1]["date"][6:8],
                "source": f"GDELT news-volume (gdeltproject.org): share of global coverage matching {label}, last 7 days vs 3-month average — a MEDIA-ATTENTION proxy, not a count of events"}
    return fn


def build_social():
    s = {"_purpose": "Pressure that turns economic stress into street stress: cost of living, fuel, youth jobs, policy/geopolitical uncertainty, supply chains, and how much the world's news is about unrest right now."}
    s["ez_hicp_yoy"] = safe_fetch("ez_hicp", _yoy("CP0000EZ19M086NEST", "Euro-area HICP inflation"), {"value": None, "unit": "% YoY"})
    s["food_price_yoy"] = safe_fetch("food", _yoy("PFOODINDEXM", "IMF global food price index"), {"value": None, "unit": "% YoY"})
    s["us_gasoline"] = safe_fetch("gas", _plain("GASREGW", "US regular gasoline, weekly (EIA)", "USD/gal"), {"value": None, "unit": "USD/gal"})
    s["us_diesel"] = safe_fetch("diesel", _plain("GASDESW", "US diesel retail, weekly (EIA)", "USD/gal"), {"value": None, "unit": "USD/gal"})
    for key, geo, lab in (("youth_unemp_fr", "FR", "France"), ("youth_unemp_de", "DE", "Germany"),
                          ("youth_unemp_es", "ES", "Spain"), ("youth_unemp_it", "IT", "Italy"),
                          ("youth_unemp_pt", "PT", "Portugal")):
        s[key] = safe_fetch(key, _eurostat_youth(geo, lab), {"value": None, "unit": "%"})
    s["global_epu"] = safe_fetch("gepu", _plain("GEPUCURRENT", "Global Economic Policy Uncertainty index", "index", digits=1), {"value": None, "unit": "index"})
    s["us_epu"] = safe_fetch("usepu", _plain("USEPUINDXD", "US Economic Policy Uncertainty, daily", "index", digits=1), {"value": None, "unit": "index"})
    s["gpr"] = safe_fetch("gpr", get_gpr, {"value": None, "unit": "index"})
    s["gscpi"] = safe_fetch("gscpi", get_gscpi, {"value": None, "unit": "std dev"})
    s["sp500_drawdown"] = safe_fetch("spdd", get_sp500_drawdown, {"value": None, "unit": "%"})
    s["copper"] = safe_fetch("copper", _plain("PCOPPUSDM", "Global copper price, monthly (IMF) — growth barometer", "USD/mt", digits=0), {"value": None, "unit": "USD/mt"})
    return s


# ---------------------------------------------------------------------------
# MARKETS NOW — near-real-time quotes (Yahoo Finance chart API, unofficial)
# ---------------------------------------------------------------------------
MARKET_SYMBOLS = [
    # key, Yahoo symbol, label, group, unit
    ("sp500", "^GSPC", "S&P 500", "Equities", "pts"),
    ("nasdaq", "^IXIC", "Nasdaq Composite", "Equities", "pts"),
    ("nikkei", "^N225", "Nikkei 225", "Equities", "pts"),
    ("hsi", "^HSI", "Hang Seng", "Equities", "pts"),
    ("kospi", "^KS11", "KOSPI", "Equities", "pts"),
    ("klci", "^KLSE", "FBM KLCI", "Equities", "pts"),
    ("shcomp", "000001.SS", "Shanghai Composite", "Equities", "pts"),
    ("sox", "^SOX", "Philadelphia Semiconductor (SOX) — Malaysia E&E proxy", "Equities", "pts"),
    ("dax", "^GDAXI", "DAX", "Equities", "pts"),
    ("cac", "^FCHI", "CAC 40", "Equities", "pts"),
    ("ftse", "^FTSE", "FTSE 100", "Equities", "pts"),
    ("vix", "^VIX", "VIX", "Risk & rates", ""),
    ("us10y", "^TNX", "US 10Y yield", "Risk & rates", "%"),
    ("dxy", "DX-Y.NYB", "Dollar index (DXY)", "Risk & rates", ""),
    ("usdjpy", "JPY=X", "USD/JPY", "FX", "JPY"),
    ("usdmyr", "MYR=X", "USD/MYR", "FX", "MYR"),
    ("eurusd", "EURUSD=X", "EUR/USD", "FX", "USD"),
    ("wti", "CL=F", "WTI crude (front month)", "Commodities", "USD/bbl"),
    ("brent", "BZ=F", "Brent crude (front month)", "Commodities", "USD/bbl"),
    ("gold", "GC=F", "Gold (front month)", "Commodities", "USD/oz"),
    ("copper", "HG=F", "Copper (front month)", "Commodities", "USD/lb"),
    ("btc", "BTC-USD", "Bitcoin", "Crypto", "USD"),
]


def get_yahoo_quote(sym, label, group, unit):
    def fn():
        import urllib.parse
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=5d&interval=1d"
        r = json.loads(http_get(url, timeout=25))["chart"]["result"][0]
        meta = r["meta"]; ts = r["timestamp"]; closes = r["indicators"]["quote"][0]["close"]
        bars = [(t, c) for t, c in zip(ts, closes) if c is not None]
        if len(bars) < 2:
            raise RuntimeError(f"{sym}: fewer than 2 daily bars")
        price = float(meta.get("regularMarketPrice") or bars[-1][1])
        prev_close = float(bars[-2][1])
        qt = int(meta.get("regularMarketTime") or bars[-1][0])
        return {"value": round(price, 4 if price < 20 else 2), "unit": unit,
                "label": label, "group": group, "symbol": sym,
                "prev_close": round(prev_close, 4 if prev_close < 20 else 2),
                "pct_1d": round((price / prev_close - 1) * 100, 2),
                "abs_1d": round(price - prev_close, 4),
                "quote_time": datetime.fromtimestamp(qt, timezone.utc).isoformat(),
                "as_of": datetime.fromtimestamp(qt, timezone.utc).date().isoformat(),
                "source": f"Yahoo Finance {sym} — unofficial, best-effort; the exchange/central bank is the official source"}
    return fn


def build_markets():
    out = {"_purpose": "Near-real-time market quotes with the move vs the previous close. Refreshed every few hours by the automation; the quote time shows how old each price is (markets are closed at weekends)."}
    for key, sym, label, group, unit in MARKET_SYMBOLS:
        out[key] = safe_fetch("mkt_" + key, get_yahoo_quote(sym, label, group, unit),
                              {"value": None, "unit": unit, "label": label, "group": group, "symbol": sym})
        time.sleep(0.4)
    return out


# ---------------------------------------------------------------------------
# CHANGES — how far each FRED-backed reading moved vs previous / 1 week / 1 month
# ---------------------------------------------------------------------------
def add_changes(data):
    import re as _re
    for section in ("us_global", "global_systemic", "world", "malaysia", "social", "markets"):
        for key, entry in data.get(section, {}).items():
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), (int, float)):
                continue
            if entry.get("tier") != "live-api":
                continue
            m = _re.match(r"FRED:([A-Z0-9]+)\b", entry.get("source", ""))
            if not m or "derived" in entry.get("source", "")[:12]:
                continue
            try:
                obs = fred_latest(m.group(1), n=90)
                latest_d, latest_v = obs[0][0], float(obs[0][1])
                v = float(entry["value"])
                # only attach when this card really shows the raw latest observation
                if abs(v - latest_v) > max(0.006, abs(latest_v) * 0.002):
                    continue
                ld = datetime.fromisoformat(latest_d)

                def ref_at(days):
                    tgt = ld - timedelta(days=days)
                    for d, val in obs[1:]:
                        if datetime.fromisoformat(d) <= tgt:
                            return d, float(val)
                    return None

                def chg(ref):
                    if not ref:
                        return None
                    d, rv = ref
                    return {"ref": d, "abs": round(latest_v - rv, 4),
                            "pct": round((latest_v / rv - 1) * 100, 2) if rv else None}

                entry["changes"] = {"d1": chg((obs[1][0], float(obs[1][1]))) if len(obs) > 1 else None,
                                    "w1": chg(ref_at(7)), "m1": chg(ref_at(30))}
                time.sleep(0.12)
            except Exception as e:
                print(f"[warn] changes {section}.{key} skipped: {e}", file=sys.stderr)


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
        "malaysia": build_malaysia(),
        "fmcg_costs": build_fmcg_costs(),
        "social": build_social(),
        "markets": build_markets(),
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

    try:
        add_changes(data)
    except Exception as e:
        print(f"[warn] add_changes failed: {e}", file=sys.stderr)

    try:
        data["decision"] = build_decision(data)
    except Exception as e:
        print(f"[warn] decision layer failed: {e}", file=sys.stderr)
        data["decision"] = {"error": str(e)}

    # Preserve last-good values for anything that came back stale, by
    # merging against the previous data.json if present.
    out_path = os.path.join(os.path.dirname(__file__), "..", "data.json")
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                prev = json.load(f)
            for section in ("us_global", "japan", "global_systemic", "world", "malaysia", "fmcg_costs", "social", "markets"):
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

    # ---- Daily snapshot of every numeric metric (powers "since last run" deltas) ----
    try:
        flat = {}
        for section in ("us_global", "japan", "global_systemic", "world", "malaysia", "fmcg_costs", "social", "markets"):
            for k, v in data.get(section, {}).items():
                if not isinstance(v, dict):
                    continue
                if isinstance(v.get("value"), (int, float)):
                    flat[f"{section}.{k}"] = v["value"]
                if k == "jgb_curve" and isinstance(v.get("curve"), dict):
                    for tenor, val in v["curve"].items():
                        if isinstance(val, (int, float)):
                            flat[f"japan.jgb_{tenor.lower()}"] = val
        snap_path = os.path.join(os.path.dirname(__file__), "..", "daily_snapshot.json")
        snaps = {}
        if os.path.exists(snap_path):
            with open(snap_path) as f:
                snaps = json.load(f)
        snaps[datetime.now(timezone.utc).date().isoformat()] = flat
        for old in sorted(snaps.keys())[:-45]:
            del snaps[old]
        with open(snap_path, "w") as f:
            json.dump(snaps, f, separators=(",", ":"))
    except Exception as e:
        print(f"[warn] daily snapshot skipped: {e}", file=sys.stderr)

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
