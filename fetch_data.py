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
from datetime import datetime, timezone
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
        with open(out_path) as f:
            prev = json.load(f)
        for section in ("us_global", "japan", "global_systemic"):
            for k, v in data.get(section, {}).items():
                if v.get("tier") == "stale" and section in prev and k in prev[section]:
                    prev_entry = prev[section][k]
                    if prev_entry.get("value") is not None:
                        data[section][k] = {**prev_entry, "tier": "stale", "error": v.get("error")}

    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
