"""FRED market data collector — US Treasury 2Y/10Y + USDKRW + WTI.

FRED API series IDs (market close, business-day daily):
- DGS2:       Market Yield on U.S. Treasury Securities at 2-Year Constant Maturity (%)
- DGS10:      Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity (%)
- DEXKOUS:    Korea / U.S. Foreign Exchange Rate (KRW per 1 USD)
- DCOILWTICO: Crude Oil Prices - WTI - Cushing, OK (USD per barrel)

Auto fallback to most-recent business-day close: series[-1] is whatever day the
exchange last produced a closing print. Holidays / late releases handled transparently.

Retry: 3 attempts with exponential backoff for transient 5xx errors.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
import requests


FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
RETRY_MAX = 3
RETRY_BACKOFF = 2.0  # seconds

SERIES = [
    ("미국채(2년)",  "DGS2",       "%",     "bp",    "FRED"),
    ("미국채(10년)", "DGS10",      "%",     "bp",    "FRED"),
    ("원달러환율",   "DEXKOUS",    "KRW",   "KRW",   "FRED"),
    ("WTI유가",      "DCOILWTICO", "USD",   "USD",   "FRED"),
]


def fetch_series(fred_key: str, series_id: str, days_back: int = 90) -> list[dict]:
    """FRED API series fetch with retry. Skips '.' / '' (FRED missing markers).

    days_back=90 -> ~60 business days history, so series[-1] always resolves to the
    most recent available close even if the latest 1-2 days are absent.
    """
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)
    params = {
        "series_id": series_id,
        "api_key": fred_key,
        "file_type": "json",
        "observation_start": start_dt.strftime("%Y-%m-%d"),
        "observation_end": end_dt.strftime("%Y-%m-%d"),
    }
    last_err = None
    data = None
    for attempt in range(RETRY_MAX):
        try:
            r = requests.get(FRED_BASE, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            break
        except requests.RequestException as e:
            last_err = str(e)
            if attempt < RETRY_MAX - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    if data is None:
        return [{"_error": f"FRED {series_id} retry {RETRY_MAX} failed: {last_err}"}]
    obs = data.get("observations", [])
    series = []
    for o in obs:
        d = o.get("date")
        v = o.get("value")
        if d and v and v not in (".", None, ""):
            try:
                series.append({"date": d, "value": float(v)})
            except (ValueError, TypeError):
                pass
    series.sort(key=lambda x: x["date"])
    return series


def compute_deltas(series: list[dict], delta_unit: str) -> dict:
    """Day-over-day and month-over-month delta from series[-1].

    delta_unit:
    - 'bp':  (latest - prev) * 100 -> bp (for % yields)
    - 'KRW': latest - prev (FX, KRW)
    - 'USD': latest - prev (oil, USD/bbl)
    """
    if not series or (series and series[0].get("_error")):
        err = series[0].get("_error") if series else "empty"
        return {"error": err}
    latest = series[-1]
    prev_day = series[-2] if len(series) >= 2 else None
    target = (datetime.strptime(latest["date"], "%Y-%m-%d")
              - timedelta(days=30)).strftime("%Y-%m-%d")
    prev_month = None
    for r in reversed(series[:-1]):
        if r["date"] <= target:
            prev_month = r
            break
    if prev_month is None and len(series) > 1:
        prev_month = series[0]

    def delta(now, before):
        if before is None:
            return None
        diff = now["value"] - before["value"]
        if delta_unit == "bp":
            return round(diff * 100, 1)
        return round(diff, 2)

    return {
        "latest_date": latest["date"],
        "latest_value": latest["value"],
        "prev_day_date": prev_day["date"] if prev_day else None,
        "vs_prev_day": delta(latest, prev_day),
        "prev_month_date": prev_month["date"] if prev_month else None,
        "vs_prev_month": delta(latest, prev_month),
        "delta_unit": delta_unit,
        "recent_series": series[-15:],
    }


def run(fred_key):
    out = {}
    if not fred_key:
        for name, _, unit, du, src in SERIES:
            out[name] = {"error": "no fred_key", "unit": unit, "delta_unit": du, "source": src}
        return out
    for name, series_id, unit, du, src in SERIES:
        series = fetch_series(fred_key, series_id)
        result = compute_deltas(series, du)
        result["unit"] = unit
        result["source"] = src
        result["series_id"] = series_id
        out[name] = result
    return out


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: python fred_market.py <FRED_API_KEY>")
        sys.exit(1)
    res = run(sys.argv[1])
    for name, d in res.items():
        if "error" in d:
            print(f"  [FAIL] {name}: {d['error']}")
        else:
            print(f"  [OK  ] {name}: {d['latest_value']}{d['unit']} on {d['latest_date']} "
                  f"(d {d['vs_prev_day']}{d['delta_unit']}, m {d['vs_prev_month']}{d['delta_unit']})")
