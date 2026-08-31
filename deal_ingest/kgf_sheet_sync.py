"""국민성장펀드 집행현황 — 구글시트(정본) → aggregate 동기화.

박이사가 기사 기반으로 월별 직접 갱신하는 구글시트가 KGF 집행현황의 ground
truth이며, 대시보드(national-growth-fund.web.app)도 이 시트로 만든다. 과거엔
kgf_execution.py의 CONFIRMED_AGGREGATE 상수가 정본 행세를 해 매 수집 때 JSON을
낡은 값(16건)으로 되돌렸다(2026-08-04 오발송 사고). 본 모듈은 시트 CSV를 직접
읽어 aggregate를 구성하고, kgf_execution.run() 이 이를 상수보다 우선 채택한다.

시트 접근 요건: 시트가 "링크가 있는 모든 사용자 = 뷰어" 이상으로 공유돼 있어야
export CSV 엔드포인트가 무인증으로 응답한다(비공개면 fetch 실패 → 상수 fallback).

데이터 무결성: fetch/parse 실패 시 절대 추정값을 만들지 않고 None 을 돌려
호출자가 직전 값(JSON) 또는 상수 fallback 을 쓰게 한다.
"""

from __future__ import annotations

import csv
import io
import urllib.request
from datetime import datetime, timezone

# 정본 시트 (Sheet1)
SHEET_ID = "1HTHv8VxP-K6-NcWRmg43bGaZvFVot8iR8XTacJNatU4"
GID = "1621139155"
CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export"
    f"?format=csv&gid={GID}"
)

# 시트 method 컬럼 → aggregate breakdown 키 (대시보드 분야구분과 동일)
METHOD_COLS = [
    ("직접투자", "direct_investment"),
    ("간접투자", "indirect_fund"),
    ("인프라", "infrastructure"),
    ("저리대출", "low_interest_loan"),
]
TOTAL_ROW_KEY = "전체투자규모"
PARTICIPATORY_FUND_KEY = "국민참여성장펀드"


def _num(s):
    """'34,000' → 34000, 빈칸/비수치 → None."""
    if s is None:
        return None
    t = str(s).replace(",", "").replace(" ", "").strip()
    if not t:
        return None
    try:
        return int(float(t))
    except ValueError:
        return None


def _fmt_eok(n):
    """억 단위 정수 → '17조6,493억원' / '5,000억원' 형식."""
    if n is None:
        return None
    if n >= 10000:
        jo, rem = divmod(n, 10000)
        return f"{jo}조원" if rem == 0 else f"{jo}조{rem:,}억원"
    return f"{n:,}억원"


def parse_csv(text):
    """CSV 텍스트 → aggregate dict. 파싱 불가 시 ValueError."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("empty csv")

    # 헤더 행 탐색 ('순번'·'투자규모'·'첨단기금' 포함하는 행)
    header_idx = None
    for i, r in enumerate(rows[:5]):
        if "순번" in r and "투자규모" in r and "첨단기금" in r:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("header row not found")
    header = [c.strip() for c in rows[header_idx]]
    col = {name: header.index(name) for name in header}

    def cell(r, name):
        j = col.get(name)
        return r[j] if (j is not None and j < len(r)) else ""

    breakdown = {key: {"count": 0, "amount_num": 0} for _, key in METHOD_COLS}
    approved_count = 0
    as_of = None
    fund_committed = None
    total_amount = None
    total_frontier = None
    deals = []  # 개별 집행 deal 목록(신규 승인 산출용)

    for r in rows[header_idx + 1:]:
        if not any(c.strip() for c in r):
            continue
        seq = cell(r, "순번").strip()

        # 합계 행
        if seq == TOTAL_ROW_KEY or cell(r, "회사명").strip() == TOTAL_ROW_KEY:
            total_amount = _num(cell(r, "투자규모"))
            total_frontier = _num(cell(r, "첨단기금"))
            continue
        # 국민참여성장펀드(결성) — 별도 표기용, 승인 건수엔 미포함(대시보드 규칙)
        if seq == PARTICIPATORY_FUND_KEY or cell(r, "회사명").strip() == PARTICIPATORY_FUND_KEY:
            fund_committed = _num(cell(r, "간접투자")) or _num(cell(r, "투자규모"))
            continue
        # 번호 매겨진 실집행 deal
        if not seq.isdigit():
            continue
        amt = _num(cell(r, "투자규모"))
        if amt is None:
            continue
        # 분야: 4개 method 컬럼 중 값이 있는 첫 컬럼 (deal 당 1개만 채워짐)
        cat = None
        for src, key in METHOD_COLS:
            if cell(r, src).strip():
                cat = key
                break
        if cat is None:
            continue
        breakdown[cat]["count"] += 1
        breakdown[cat]["amount_num"] += amt
        approved_count += 1
        appr = cell(r, "승인일시").strip()
        if appr and (as_of is None or appr > as_of):
            as_of = appr
        method_label = next((src for src, key in METHOD_COLS if key == cat), "")
        deals.append({
            "seq": int(seq),
            "company": cell(r, "회사명").strip(),
            "sector": cell(r, "산업분야").strip(),
            "amount_num": amt,
            "amount": _fmt_eok(amt),
            "method": method_label,
            "date": appr,
        })

    if approved_count == 0:
        raise ValueError("no numbered deals parsed")

    # 총액·첨단기금: 합계 행 우선, 없으면 deal 합으로 보정
    if total_amount is None:
        total_amount = sum(b["amount_num"] for b in breakdown.values()) + (fund_committed or 0)

    out_breakdown = {}
    for _, key in METHOD_COLS:
        b = breakdown[key]
        amt_str = _fmt_eok(b["amount_num"])
        if key == "indirect_fund" and fund_committed:
            amt_str = f"{_fmt_eok(b['amount_num'])}(결성 {_fmt_eok(fund_committed)})"
        out_breakdown[key] = {"count": b["count"], "amount": amt_str}

    return {
        "as_of": as_of,
        "source": "구글시트(국민성장펀드_집행현황) → dashboard",
        "sheet_synced_at": datetime.now(timezone.utc).isoformat(),
        "approved_count": approved_count,
        "cumulative_amount": _fmt_eok(total_amount),
        "frontier_fund_amount": _fmt_eok(total_frontier) if total_frontier else None,
        "breakdown": out_breakdown,
        "participatory_fund_committed": _fmt_eok(fund_committed) if fund_committed else None,
        "identified_count": approved_count,
        "undisclosed_count": 0,
        "deals": sorted(deals, key=lambda d: d.get("seq", 0)),
    }


def fetch_sheet_aggregate(timeout=15):
    """시트 CSV fetch+parse → aggregate dict. 실패 시 None (fallback 유도)."""
    try:
        req = urllib.request.Request(CSV_URL, headers={"User-Agent": "deal_ingest/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8-sig", errors="replace")
        # 비공개 시트는 로그인 HTML을 반환 → CSV 아님
        if "<html" in raw[:200].lower():
            return None
        return parse_csv(raw)
    except Exception:
        return None


if __name__ == "__main__":
    agg = fetch_sheet_aggregate()
    if agg is None:
        print("FETCH_FAILED — 시트 공유설정(링크 뷰어) 확인 필요. 상수 fallback 사용.")
    else:
        import json
        print(json.dumps(agg, ensure_ascii=False, indent=2))
