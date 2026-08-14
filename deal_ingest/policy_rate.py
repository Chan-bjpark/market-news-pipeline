"""Policy rate collector — 한국·미국 기준금리 + 회의 일정.

한국: ECOS 722Y001/0101000 (한국은행 기준금리, 일별 시계열)
미국: FRED API (DFEDTARU=상단, DFEDTARL=하단, 또는 DFF=실효금리)
회의 일정: config.json의 policy_schedule 섹션에서 정적 박아둠
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
import requests


ECOS_BASE = "https://ecos.bok.or.kr/api"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
RETRY_MAX = 3
RETRY_BACKOFF = 2.0


def _get_with_retry(url: str, params: dict | None = None, timeout: int = 20):
    """간헐적 5xx 대응 retry wrapper."""
    last_err = None
    for attempt in range(RETRY_MAX):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = e
            if attempt < RETRY_MAX - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise last_err


def fetch_bok_policy_rate(ecos_key: str, days_back: int = 365) -> list[dict]:
    """한국은행 기준금리 일별 시계열 (1년치)."""
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)
    url = (f"{ECOS_BASE}/StatisticSearch/{ecos_key}/json/kr/1/1000/"
           f"722Y001/D/{start_dt.strftime('%Y%m%d')}/"
           f"{end_dt.strftime('%Y%m%d')}/0101000")
    r = _get_with_retry(url, timeout=20)
    data = r.json()
    if "StatisticSearch" not in data:
        return []
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


def fetch_fred_policy_rate(fred_key: str, series_id: str = "DFF",
                           days_back: int = 365) -> list[dict]:
    """미국 연방기금금리 일별 시계열.

    series_id 선택지:
    - DFF: Effective Federal Funds Rate (실효, daily)
    - DFEDTARU: Federal Funds Target Range - Upper (정책 목표 상단)
    - DFEDTARL: Federal Funds Target Range - Lower (정책 목표 하단)
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
    r = _get_with_retry(FRED_BASE, params=params, timeout=20)
    data = r.json()
    obs = data.get("observations", [])
    series = []
    for o in obs:
        d = o.get("date")
        v = o.get("value")
        if d and v and v not in (".", None):
            try:
                series.append({"date": d, "value": float(v)})
            except (ValueError, TypeError):
                pass
    series.sort(key=lambda x: x["date"])
    return series


def detect_last_change(series: list[dict]) -> dict:
    """시계열에서 마지막 값 변동 시점 + 방향 추출."""
    if not series:
        return {"error": "no series"}
    latest = series[-1]
    # 거꾸로 훑으며 값이 다른 첫 시점 찾기
    last_change_date = None
    last_change_value = None
    for r in reversed(series[:-1]):
        if abs(r["value"] - latest["value"]) > 1e-9:
            # 변동 직후 값(=현재 latest와 같은 값)의 시작 시점 찾기
            # series에서 r 다음 시점이 최초 변동일
            idx = series.index(r)
            last_change_date = series[idx + 1]["date"]
            last_change_value = r["value"]  # 이전 값
            break
    direction = "동결"
    if last_change_value is not None:
        if latest["value"] > last_change_value:
            direction = "인상"
        elif latest["value"] < last_change_value:
            direction = "인하"
    return {
        "latest_date": latest["date"],
        "latest_value": latest["value"],
        "last_change_date": last_change_date,
        "last_change_from": last_change_value,
        "direction": direction,
    }


def next_meeting(schedule: list[str], today: str | None = None) -> str | None:
    today = today or datetime.now().strftime("%Y-%m-%d")
    future = [d for d in schedule if d >= today]
    return min(future) if future else None


def last_meeting(schedule: list[str], today: str | None = None) -> str | None:
    today = today or datetime.now().strftime("%Y-%m-%d")
    past = [d for d in schedule if d < today]
    return max(past) if past else None


def meeting_direction(last_meet, last_change_date, direction_at_change):
    """최근 회의가 변동 시점이면 그 방향, 아니면 동결."""
    if not last_meet:
        return direction_at_change
    if last_change_date and last_change_date == last_meet:
        return direction_at_change
    if last_change_date and last_meet > last_change_date:
        return "동결"
    return direction_at_change


def run(ecos_key: str | None, fred_key: str | None,
        bok_schedule: list[str], fomc_schedule: list[str]) -> dict:
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "policy_rates": {}}
    # 한국 기준금리
    if ecos_key:
        try:
            series = fetch_bok_policy_rate(ecos_key)
            info = detect_last_change(series)
            info["next_meeting"] = next_meeting(bok_schedule)
            info["last_meeting"] = last_meeting(bok_schedule)
            info["meeting_direction"] = meeting_direction(
                info["last_meeting"], info.get("last_change_date"),
                info.get("direction", "동결"))
            info["source"] = "ECOS"
            out["policy_rates"]["한국 기준금리"] = info
        except Exception as e:
            out["policy_rates"]["한국 기준금리"] = {"error": str(e)}
    # 미국 기준금리
    if fred_key:
        try:
            series_u = fetch_fred_policy_rate(fred_key, "DFEDTARU")
            series_l = fetch_fred_policy_rate(fred_key, "DFEDTARL")
            if series_u and series_l:
                u = series_u[-1]["value"]
                l = series_l[-1]["value"]
                # 변동 방향 detection은 상단 기준
                info = detect_last_change(series_u)
                info["latest_value_upper"] = u
                info["latest_value_lower"] = l
                info["latest_value_str"] = f"{l:.2f}-{u:.2f}"
                info["next_meeting"] = next_meeting(fomc_schedule)
                info["last_meeting"] = last_meeting(fomc_schedule)
                info["meeting_direction"] = meeting_direction(
                    info["last_meeting"], info.get("last_change_date"),
                    info.get("direction", "동결"))
                info["source"] = "FRED"
                out["policy_rates"]["미국 기준금리"] = info
            else:
                out["policy_rates"]["미국 기준금리"] = {"error": "no DFEDTARU/L"}
        except Exception as e:
            out["policy_rates"]["미국 기준금리"] = {"error": str(e)}
    return out


if __name__ == "__main__":
    import sys, json
    ecos_key = sys.argv[1] if len(sys.argv) > 1 else None
    fred_key = sys.argv[2] if len(sys.argv) > 2 else None
    # 2026년 한국 금통위 일정 (확인 필요)
    bok_2026 = ["2026-01-15", "2026-02-26", "2026-04-09", "2026-05-28",
                "2026-07-16", "2026-08-27", "2026-10-22", "2026-11-26"]
    # 2026년 FOMC 일정 (확인 필요)
    fomc_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
                 "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
    out = run(ecos_key, fred_key, bok_2026, fomc_2026)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
