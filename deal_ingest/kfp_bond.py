"""KFP (data.go.kr) bond price collector — 국고채 만기별 일별 종가수익률.

API: 금융위원회_채권시세정보 (한국거래소 데이터)
Filter: mrktCtg=KTS (국채전문유통시장) + itmsCtg='지표' + xpYrCnt=3/10
Field used: clprBnfRt (종가_수익률 = 금리)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


BASE = ("https://apis.data.go.kr/1160100/service/"
        "GetBondSecuritiesInfoService/getBondPriceInfo")

TENORS = [3, 10]
PAGE_SIZE = 1000


def fetch_kts_indicator(service_key: str, days_back: int = 60) -> list[dict]:
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)
    # serviceKey는 URL에 raw로 박음 (requests params의 자동 재인코딩 회피)
    url_with_key = f"{BASE}?serviceKey={service_key}"
    params_base = {
        "numOfRows": PAGE_SIZE,
        "resultType": "json",
        "beginBasDt": start_dt.strftime("%Y%m%d"),
        "endBasDt": end_dt.strftime("%Y%m%d"),
        "mrktCtg": "KTS",
    }
    items = []
    page = 1
    while page <= 50:
        params = {**params_base, "pageNo": page}
        r = requests.get(url_with_key, params=params, timeout=25)
        if r.status_code != 200:
            raise RuntimeError(
                f"HTTP {r.status_code}: {r.text[:500]}"
            )
        data = r.json()
        resp = data.get("response", {})
        header = resp.get("header", {})
        if header.get("resultCode") not in ("00", 0, "0"):
            raise RuntimeError(f"KFP API error: {header}")
        body = resp.get("body", {})
        rows = body.get("items", {})
        if isinstance(rows, dict):
            rows = rows.get("item", [])
        if not rows:
            break
        if isinstance(rows, dict):
            rows = [rows]
        items.extend(rows)
        total = int(body.get("totalCount", 0) or 0)
        if page * PAGE_SIZE >= total:
            break
        page += 1
    return items


def extract_indicator_series(items: list[dict], tenor: int) -> list[dict]:
    """For target tenor, return [{date, value, isin, name}, ...] sorted by date."""
    by_date = {}
    for r in items:
        cat = (r.get("itmsCtg") or "").strip()
        if "지표" not in cat:
            continue
        yr = (r.get("xpYrCnt") or "").strip()
        try:
            yr_n = int(str(yr).replace("년", "").strip())
        except (ValueError, TypeError):
            continue
        if yr_n != tenor:
            continue
        date = (r.get("basDt") or "").strip()
        rate = r.get("clprBnfRt")
        if not date or rate in (None, "", " "):
            continue
        try:
            rate_f = float(rate)
        except (ValueError, TypeError):
            continue
        # 같은 날짜에 여러 지표채가 있을 수 있음 — 거래량 가장 큰 것 채택
        trqu = 0
        try:
            trqu = int(r.get("trqu") or 0)
        except (ValueError, TypeError):
            pass
        prev = by_date.get(date)
        if prev is None or trqu > prev["trqu"]:
            by_date[date] = {
                "value": rate_f,
                "trqu": trqu,
                "isin": r.get("isinCd"),
                "name": r.get("itmsNm"),
            }
    series = []
    for d in sorted(by_date.keys()):
        v = by_date[d]
        series.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
            "value": v["value"],
            "isin": v["isin"],
            "name": v["name"],
        })
    return series


def compute_deltas(series: list[dict]) -> dict:
    if not series:
        return {"error": "no series data"}
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
        "isin": latest.get("isin"),
        "name": latest.get("name"),
        "prev_day_date": prev_day["date"] if prev_day else None,
        "vs_prev_day_bp": round((latest["value"] - prev_day["value"]) * 100, 1)
                          if prev_day else None,
        "prev_month_date": prev_month["date"] if prev_month else None,
        "vs_prev_month_bp": round((latest["value"] - prev_month["value"]) * 100, 1)
                            if prev_month else None,
        "recent_series": series[-15:],
    }


def run(service_key: str) -> dict:
    items = fetch_kts_indicator(service_key)
    result = {}
    for tenor in TENORS:
        series = extract_indicator_series(items, tenor)
        result[f"국고채({tenor}년)"] = compute_deltas(series)
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python kfp_bond.py <SERVICE_KEY>")
        sys.exit(1)
    out = run(sys.argv[1])
    for name, d in out.items():
        if "error" in d:
            print(f"  [FAIL] {name}: {d['error']}")
        else:
            print(f"  [OK  ] {name}: {d['latest_value']}% on {d['latest_date']} "
                  f"({d.get('name')}) "
                  f"prev_day {d['vs_prev_day_bp']}bp, "
                  f"prev_month {d['vs_prev_month_bp']}bp")
