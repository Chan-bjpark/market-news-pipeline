"""ECOS (BOK) API rate collector — short-term rates (CD, 통안, 콜)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Shared atomic write (fsync + validation)
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from util_io import atomic_write_json  # noqa: E402


STAT_CODE = "817Y002"
PERIODICITY = "D"
ECOS_BASE = "https://ecos.bok.or.kr/api"

# CD(91일) only. 국고채는 KFP가 담당.
ITEMS = [
    ("CD(91일)", ["010502000"]),
    ("국고채(3년)", ["010200000"]),
    ("국고채(10년)", ["010210000"]),
]


def fetch_series(api_key, item_code, days_back=90):
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)
    end = end_dt.strftime("%Y%m%d")
    start = start_dt.strftime("%Y%m%d")
    url = (f"{ECOS_BASE}/StatisticSearch/{api_key}/json/kr/1/1000/"
           f"{STAT_CODE}/{PERIODICITY}/{start}/{end}/{item_code}")
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return [{"_error": str(e)}]
    if "RESULT" in data:
        return [{"_error": data["RESULT"].get("MESSAGE", "ECOS error")}]
    if "StatisticSearch" not in data:
        return [{"_error": f"unexpected: {list(data.keys())}"}]
    rows = data["StatisticSearch"].get("row", [])
    series = []
    for row in rows:
        t = row.get("TIME")
        v = row.get("DATA_VALUE")
        if t and v:
            try:
                series.append({
                    "date": f"{t[:4]}-{t[4:6]}-{t[6:8]}",
                    "value": float(v),
                })
            except (ValueError, IndexError):
                pass
    series.sort(key=lambda x: x["date"])
    return series


def fetch_with_fallback(api_key, name, codes):
    last = None
    for code in codes:
        series = fetch_series(api_key, code)
        if series and series[0].get("_error") is None:
            return {"item_code": code, "series": series}
        last = series
    err = (last[0].get("_error") if last else "no data")
    return {"item_code": None, "series": last or [], "error": err}


def compute_deltas(series):
    if not series or series[0].get("_error"):
        return {"error": series[0].get("_error") if series else "empty"}
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
    return {
        "latest_date": latest["date"],
        "latest_value": latest["value"],
        "prev_day_date": prev_day["date"] if prev_day else None,
        "vs_prev_day_bp": round((latest["value"] - prev_day["value"]) * 100, 1)
                          if prev_day else None,
        "prev_month_date": prev_month["date"] if prev_month else None,
        "vs_prev_month_bp": round((latest["value"] - prev_month["value"]) * 100, 1)
                            if prev_month else None,
        "recent_series": series[-15:],
    }


def run(api_key, out_path):
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "items": {}}
    for name, codes in ITEMS:
        fetched = fetch_with_fallback(api_key, name, codes)
        if fetched["item_code"]:
            stats = compute_deltas(fetched["series"])
            out["items"][name] = {"item_code": fetched["item_code"], **stats}
        else:
            out["items"][name] = {"error": fetched.get("error", "failed")}
    atomic_write_json(out_path, out)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ecos_rate.py <ECOS_API_KEY>")
        sys.exit(1)
    out = run(sys.argv[1], Path(__file__).parent / "rate_summary.json")
    for name, d in out["items"].items():
        if "error" in d:
            print(f"  [FAIL] {name}: {d['error']}")
        else:
            print(f"  [OK  ] {name}: {d['latest_value']}% on {d['latest_date']} "
                  f"(prev_day {d['vs_prev_day_bp']}bp, "
                  f"prev_month {d['vs_prev_month_bp']}bp)")
