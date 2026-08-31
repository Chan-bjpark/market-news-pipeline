"""국민성장펀드 집행(execution) tracker — v2 (확인 baseline + 뉴스 누적).

설계
- 박이사 확인 승인내역(2026.1~2026.4)을 CONFIRMED_BASELINE 상수로 내장한다.
  이 표는 *사실로 확인된* 집행/승인 내역이며 트래킹의 기반(ground truth)이다.
  뉴스로 덮어쓰지 않는다.
- 매 실행: baseline 을 confirmed_deals 로 펼치고(기존 JSON의 news_timeline 보존),
  DB(90일)+누적 뉴스에서 각 deal 관련 기사를 매칭해 news_timeline 에 누적한다.
- baseline 에 없는 새 집행 보도는 news_discovered 로 분리(향후 confirmed 편입 후보).
- 펀드 *조성 단계*(결성/LP출자)는 fund_flow.py 소관 — 본 모듈과 분리.

입력:  store/articles.db (90일)
출력:  kgf_execution_summary.json (atomic write, fsync). 누적 트래커 — stale 판정 제외.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from util_io import atomic_write_json  # noqa: E402


# =============================================================================
# 확인된 승인 내역 (박이사 제공, 2026-05-31 갱신). 사실 확인된 ground truth.
# 집계: 2026.1~2026.5 / 승인 16건 / 누적 약 12조4,000억원 (첨단기금 5.17조 포함)
#   · 직접투자: 1조 9,600억원 (3건: 리벨리온·업스테이지·퓨리오사AI)
#   · 인프라투자: 6조 6,000억원 (3건: 신안우이·국가AI컴퓨팅센터·스마일게이트)
#   · 저리대출: 3조 8,400억원 (10건)
# 1Q (1~3월): 4건 6.6조 / 2Q (4~5월): 12건 5.8조
# =============================================================================
CONFIRMED_AS_OF = "2026.1~2026.5"

# ⚠️ 이 상수는 FALLBACK baseline 일 뿐이다. 정본(ground truth)은 대시보드
#   (https://national-growth-fund.web.app/) 이며, 발송 직전 브라우저로 직접 조회해
#   건수·금액을 갱신한 뒤 메시지에 반영한다(발송지침 4-B). 아래 값은 대시보드
#   미접속 시에만 사용하는 최종확인 스냅샷이다.
CONFIRMED_AGGREGATE = {
    "as_of": "2026.07",                        # 대시보드 자료 기준월
    "source": "national-growth-fund.web.app 대시보드 (승인현황)",
    "approved_count": 23,
    "cumulative_amount": "약 17조6,493억원",
    "frontier_fund_amount": "약 7조295억원",   # 첨단전략산업기금 합계
    "breakdown": {
        "direct_investment": {"count": 4, "amount": "2조4,600억원"},
        "infrastructure":   {"count": 4, "amount": "6조8,662억원"},
        "low_interest_loan":{"count": 14, "amount": "7조1,031억원"},
        "indirect_fund":    {"count": 1, "amount": "5,000억원(결성 7,200억원)"},
    },
    "q1_count": 4,
    "q1_amount": "6조6,000억원",
    "q2_count": 19,
    "q2_amount": "약 11조493억원",
    "identified_count": 23,
    "undisclosed_count": 0,
}

# 박이사 확인 표(2026-05-31): 각 deal 마다 첨단전략산업기금 분담 명시 (frontier_fund 필드).
# methods: 표상 분류 명확 — '직접투자'·'인프라'·'저리대출' 중 하나가 1차 method 로 적용.
CONFIRMED_BASELINE = [
    {"no": 1, "company_name": "신안우이 해상풍력", "project": "신안우이 해상풍력 (1호)",
     "amount": "3조 4,000억원", "frontier_fund": "7,500억원",
     "sector": "재생에너지", "approved": "2026.1",
     "methods": ["인프라투자"],
     "match_keywords": ["신안우이", "신안 해상풍력", "신안우이 해상풍력"]},
    {"no": 2, "company_name": "삼성전자 평택 5라인 AI반도체 클러스터", "project": None,
     "amount": "2조 5,000억원(저리대출)", "frontier_fund": "2조원",
     "sector": "반도체", "approved": "2026.2",
     "methods": ["대출"],
     "match_keywords": ["평택 5라인", "평택 AI반도체", "평택 파운드리", "삼성전자 평택", "평택 클러스터"]},
    {"no": 3, "company_name": "이수스페셜티케미컬", "project": "울산 황화리튬(전고체 전지 소재)",
     "amount": "1,000억원(저리대출)", "frontier_fund": "1,000억원",
     "sector": "이차전지 소재", "approved": "2026.2",
     "methods": ["대출"],
     "match_keywords": ["이수스페셜티", "황화리튬"]},
    {"no": 4, "company_name": "리벨리온", "project": "리벨리온 증자(직접투자 1호)",
     "amount": "6,000억원", "frontier_fund": "2,500억원",
     "sector": "소버린 AI", "approved": "2026.3",
     "methods": ["출자"],
     "match_keywords": ["리벨리온"]},
    {"no": 5, "company_name": "포스코퓨처엠/퓨처그라프", "project": "새만금 구형흑연",
     "amount": "2,500억원(저리대출)", "frontier_fund": "2,000억원",
     "sector": "이차전지(음극재)", "approved": "2026.4",
     "methods": ["대출"],
     "match_keywords": ["포스코퓨처엠", "퓨처그라프", "구형흑연", "새만금 구형흑연"]},
    {"no": 6, "company_name": "업스테이지", "project": "AI 파운데이션 모델",
     "amount": "5,600억원(직접투자, 라운드 전체)", "frontier_fund": "1,000억원",
     "sector": "소버린 AI", "approved": "2026.4",
     "methods": ["출자"],
     "match_keywords": ["업스테이지"]},
    {"no": 7, "company_name": "국가 AI 컴퓨팅센터", "project": "전남 해남 솔라시도(삼성SDS 컨소시엄 SPC)",
     "amount": "4,000억원(인프라)", "frontier_fund": "180억원",
     "sector": "소버린 AI", "approved": "2026.4",
     "methods": ["인프라투자"],
     "match_keywords": ["국가 AI 컴퓨팅", "AI 컴퓨팅센터", "AI컴퓨팅센터", "AI 컴퓨팅 센터", "솔라시도"]},
    {"no": 8, "company_name": "에스티젠바이오", "project": "동아쏘시오 비티젠 — 바이오의약품 CDMO 생산시설",
     "amount": "850억원(저리대출)", "frontier_fund": "650억원",
     "sector": "바이오(CDMO)", "approved": "2026.4",
     "methods": ["대출"],
     "match_keywords": ["에스티젠바이오", "비티젠"]},
    {"no": 9, "company_name": "후성", "project": "반도체 소재(고순도 불화수소가스) 공장 증설",
     "amount": "165억원(저리대출)", "frontier_fund": "165억원",
     "sector": "반도체 소재(불화가스)", "approved": "2026.4",
     "methods": ["대출"],
     "match_keywords": ["후성"]},
    {"no": 10, "company_name": "샘씨엔에스", "project": "반도체 부품",
     "amount": "200억원(저리대출)", "frontier_fund": "200억원",
     "sector": "반도체", "approved": "2026.4",
     "methods": ["대출"],
     "match_keywords": ["샘씨엔에스", "샘씨앤에스"]},
    {"no": 11, "company_name": "네이버", "project": "AI 데이터센터",
     "amount": "4,000억원(저리대출)", "frontier_fund": "3,400억원",
     "sector": "AI 데이터센터", "approved": "2026.4",
     "methods": ["대출"],
     "match_keywords": ["네이버", "NAVER 데이터센터", "네이버 데이터센터"]},
    {"no": 12, "company_name": "퓨리오사AI", "project": "AI 반도체(직접투자)",
     "amount": "8,000억원(직접투자)", "frontier_fund": "3,700억원",
     "sector": "AI 인프라(반도체)", "approved": "2026.5",
     "methods": ["출자"],
     "match_keywords": ["퓨리오사", "FuriosaAI", "Furiosa AI"]},
    {"no": 13, "company_name": "SK바이오사이언스", "project": "폐렴구균 백신 임상3상",
     "amount": "3,000억원(저리대출)", "frontier_fund": "2,500억원",
     "sector": "제약·바이오", "approved": "2026.5",
     "methods": ["대출"],
     "match_keywords": ["SK바이오사이언스", "SK바사", "에스케이바이오사이언스"]},
    {"no": 14, "company_name": "스마일게이트", "project": "AI 데이터센터(인프라)",
     "amount": "2조 8,000억원(인프라)", "frontier_fund": "5,000억원",
     "sector": "AI 데이터센터", "approved": "2026.5",
     "methods": ["인프라투자"],
     "match_keywords": ["스마일게이트", "Smilegate"]},
    {"no": 15, "company_name": "근우", "project": "전력기기",
     "amount": "200억원(저리대출)", "frontier_fund": "200억원",
     "sector": "전력기기", "approved": "2026.5",
     "methods": ["대출"],
     "match_keywords": ["근우", "근우전기", "근우정밀"]},
    {"no": 16, "company_name": "엘앤에프플러스", "project": "양극재",
     "amount": "2,200억원(저리대출)", "frontier_fund": "1,700억원",
     "sector": "이차전지(양극재)", "approved": "2026.5",
     "methods": ["대출"],
     "match_keywords": ["엘앤에프플러스", "엘엔에프플러스", "L&F플러스", "L&F+"]},
]


# --- 국민성장펀드 식별 ---
KGF_PATTERNS = [
    r"국민성장펀드", r"국민\s*성장\s*펀드", r"국민참여\s*성장펀드",
    r"국민참여형\s*국민성장", r"IBK\s*국민성장인프라펀드",
    r"국민성장인프라펀드", r"국민성장\s*\d*\s*호",
]

METHOD_KEYWORDS = [
    ("출자", [r"출자", r"지분\s*투자", r"증자\s*참여", r"유상증자", r"증자", r"프리\s*IPO",
              r"프리아이피오", r"지분\s*인수", r"RCPS", r"상환전환우선주", r"전환우선주"]),
    ("대출", [r"대출", r"융자", r"여신", r"초저리\s*대출", r"저리\s*대출", r"투·?융자"]),
    ("사채인수", [r"사채\s*인수", r"회사채\s*인수", r"회사채\s*매입", r"CB\s*인수",
                  r"전환사채", r"신주인수권부사채", r"BW\s*인수"]),
    ("보증", [r"보증", r"지급보증", r"신용보증"]),
    ("자산매수", [r"자산\s*매수", r"자산\s*매입", r"자산\s*인수", r"부동산\s*매입"]),
    ("인프라투자", [r"인프라\s*투자", r"인프라\s*투·?융자", r"SOC\s*투자", r"프로젝트\s*투자"]),
]

STATUS_KEYWORDS = [
    ("executed",  "집행완료", [r"납입\s*완료", r"집행\s*완료", r"수령\s*완료", r"수령했",
                               r"투자금?\s*납입", r"지급\s*완료", r"실행\s*완료"]),
    ("committed", "승인·확정", [r"투자\s*확정", r"투자\s*결정", r"투자\s*발표", r"승인",
                               r"의결", r"낙점", r"선정", r"투자하기로", r"투자한다",
                               r"매입하기로", r"참여하기로", r"지원하기로", r"지원\s*확정"]),
    ("review",    "검토단계", [r"검토", r"협의", r"추진", r"논의", r"예정", r"계획"]),
]

_COMPANY_STOPWORDS = {
    "국민성장펀드", "국민성장", "국민참여성장펀드", "성장금융", "한국성장금융",
    "산업은행", "산은", "기업은행", "정부", "금융위", "금융위원회", "기재부",
    "펀드", "정책자금", "모펀드", "운용사", "민관", "재정", "첨단기금",
}

_FUND_SIZE_CAP_EOK = 200000  # 20조원 초과는 펀드 총규모로 보고 deal 금액에서 제외


def _norm_text(s):
    return (s or "").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")


def _is_kgf(text):
    return any(re.search(p, text, re.IGNORECASE) for p in KGF_PATTERNS)


def _detect_methods(text):
    return [l for l, kws in METHOD_KEYWORDS
            if any(re.search(k, text, re.IGNORECASE) for k in kws)]


def _detect_status(text):
    for code, label, kws in STATUS_KEYWORDS:
        if any(re.search(k, text, re.IGNORECASE) for k in kws):
            return code, label
    return "mention", "관련 동향"


def _amount_to_eok(s):
    s = s.replace(",", "")
    total = 0
    jo = re.search(r"(\d+)\s*조", s)
    if jo:
        total += int(jo.group(1)) * 10000
    m_eok = re.search(r"(\d+)\s*억", s)
    if m_eok:
        total += int(m_eok.group(1))
    return total or None


def _extract_amount(text):
    cands = [m.group(0).strip() for m in re.finditer(r"(\d[\d,]*)\s*조\s*(\d[\d,]*)?\s*억?", text)]
    cands += [m.group(0).strip() for m in re.finditer(r"(\d[\d,]*)\s*억\s*원?", text)]
    scored = []
    for c in cands:
        eok = _amount_to_eok(c)
        if eok and eok <= _FUND_SIZE_CAP_EOK:
            scored.append((eok, c))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][1]


def _clean_company(cand):
    if not cand:
        return None
    cand = cand.strip().strip("'\"‘’“”")
    cand = re.sub(r"^(국민성장펀드|국민성장|정부|금융위|산은|산업은행)\s*,?\s*", "", cand).strip()
    if cand and cand not in _COMPANY_STOPWORDS and 1 < len(cand) <= 18:
        return cand
    return None


def _extract_company(title, summary=""):
    text = f"{title}"
    m = re.search(r"([가-힣A-Za-z][가-힣A-Za-z0-9·\s]{1,18}?)에\s*\d[\d,]*\s*(?:조|억)", text)
    if m and _clean_company(m.group(1)):
        return _clean_company(m.group(1))
    for m in re.finditer(r"['\"‘’“”]([가-힣A-Za-z][가-힣A-Za-z0-9·]{1,17})['\"‘’“”]", text):
        if _clean_company(m.group(1)):
            return _clean_company(m.group(1))
    m = re.search(r"^([가-힣A-Za-z][가-힣A-Za-z0-9·]{1,17}?)\s*,", text)
    if m and _clean_company(m.group(1)):
        return _clean_company(m.group(1))
    m = re.search(r"투자(?:는|처|\s*대상은?)?\s*([가-힣A-Za-z][가-힣A-Za-z0-9·]{1,17}?)(?:$|\b|이|가|에|을|를)", text)
    if m and _clean_company(m.group(1)):
        return _clean_company(m.group(1))
    m = re.search(r"\]\s*([가-힣A-Za-z][가-힣A-Za-z0-9·]{1,18}?)(?:이|가|에|은|는|,)", text)
    if m and _clean_company(m.group(1)):
        return _clean_company(m.group(1))
    return None


def _norm_company(name):
    if not name:
        return None
    return re.sub(r"(주식회사|㈜|\(주\))$", "", re.sub(r"\s+", "", name))


def load_kgf_articles(db_path, days_back=90):
    """DB에서 국민성장펀드 관련 기사 로드. 접근 실패 시 [] (기존 누적 보존)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return []
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    try:
        rows = conn.execute(
            "SELECT * FROM articles WHERE published_at >= ? ORDER BY published_at ASC",
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return []
    conn.close()
    out = []
    for r in rows:
        title = _norm_text(r["title"])
        summary = _norm_text(r["summary"])
        if not _is_kgf(f"{title} {summary}"):
            continue
        out.append({"id": r["id"], "source_label": r["source_label"], "url": r["url"],
                    "title": title, "summary": summary, "published_at": r["published_at"]})
    return out


def _load_existing(out_path):
    p = Path(out_path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _build_confirmed_deals(existing):
    """CONFIRMED_BASELINE -> confirmed_deals. 기존 JSON의 news_timeline / status 보존."""
    prev = {d.get("company_name"): d for d in existing.get("confirmed_deals", [])}
    deals = []
    for b in CONFIRMED_BASELINE:
        old = prev.get(b["company_name"], {})
        deals.append({
            "no": b["no"],
            "company_name": b["company_name"],
            "project": b.get("project"),
            "amount_confirmed": b["amount"],
            "frontier_fund": b.get("frontier_fund"),
            "sector": b["sector"],
            "approved": b["approved"],
            "methods": list(b["methods"]),
            "status_code": old.get("status_code", "approved"),
            "status_label": old.get("status_label", "승인확정"),
            "_match": b["match_keywords"],
            "news_timeline": old.get("news_timeline", []),
        })
    return deals


def _append_event(deal, ev):
    if not any(e.get("url") == ev["url"] and e.get("date") == ev["date"]
               for e in deal["news_timeline"]):
        deal["news_timeline"].append(ev)
        return True
    return False


def _merge_news(confirmed, news_discovered_existing, articles):
    rank = {"mention": 0, "review": 1, "committed": 2, "approved": 2, "executed": 3}
    discovered = {d.get("company_name"): d for d in news_discovered_existing}
    candidates = {}

    for art in articles:
        text = f"{art['title']} {art['summary']}"
        methods = _detect_methods(text)
        status_code, status_label = _detect_status(text)
        amount = _extract_amount(text)
        company = _extract_company(art["title"], art["summary"])
        date = art["published_at"][:10]
        ev = {"date": date, "event": art["title"], "methods": methods,
              "amount": amount, "status_code": status_code, "status_label": status_label,
              "url": art["url"], "source": art["source_label"]}

        # 1) confirmed deal 매칭 (match_keywords or company)
        matched = None
        for d in confirmed:
            if any(kw in text for kw in d["_match"]):
                matched = d
                break
            if company and _norm_company(company) == _norm_company(d["company_name"]):
                matched = d
                break
        if matched:
            _append_event(matched, ev)
            # 상태 격상 (집행완료 등)
            if rank.get(status_code, 0) > rank.get(matched.get("status_code", "approved"), 0):
                matched["status_code"], matched["status_label"] = status_code, status_label
            for m in methods:
                if m not in matched["methods"]:
                    matched["methods"].append(m)
            continue

        # 2) baseline 밖 신규 집행 보도 -> news_discovered (회사+금액/방식 있을 때)
        if company and (methods or amount):
            cnorm = company
            d = discovered.get(cnorm)
            if d is None:
                d = {"company_name": company, "sector": None, "methods": [],
                     "amount": amount, "status_code": status_code,
                     "status_label": status_label, "first_seen": date,
                     "last_seen": date, "timeline": []}
                discovered[cnorm] = d
            if not any(e.get("url") == ev["url"] and e.get("date") == ev["date"]
                       for e in d["timeline"]):
                d["timeline"].append(ev)
            for m in methods:
                if m not in d["methods"]:
                    d["methods"].append(m)
            if amount and not d.get("amount"):
                d["amount"] = amount
            if rank.get(status_code, 0) > rank.get(d.get("status_code", "mention"), 0):
                d["status_code"], d["status_label"] = status_code, status_label
            d["last_seen"] = max(d["last_seen"], date)
            d["first_seen"] = min(d["first_seen"], date)
            continue

        # 3) 일반 동향 -> candidates(출력 안 함)
        candidates[art["url"]] = {"date": date, "title": art["title"],
                                  "url": art["url"], "source": art["source_label"]}

    for d in confirmed:
        d["news_timeline"].sort(key=lambda e: e["date"])
        d.pop("_match", None)
    disc = sorted(discovered.values(), key=lambda d: d.get("last_seen", ""), reverse=True)
    return confirmed, disc, list(candidates.values())[-100:]


def run(db_path, out_path, days_back=90):
    existing = _load_existing(out_path)
    confirmed = _build_confirmed_deals(existing)
    articles = load_kgf_articles(db_path, days_back)
    confirmed, discovered, candidates = _merge_news(
        confirmed, existing.get("news_discovered", []), articles)

    # aggregate 정본 = 구글시트(국민성장펀드_집행현황). 시트 fetch 성공 시 상수보다
    # 우선 채택 → 상수가 매 수집 때 JSON을 낡은 값으로 되돌리던 revert 사고 차단.
    # 실패(비공개·네트워크) 시에만 CONFIRMED_AGGREGATE fallback.
    aggregate = CONFIRMED_AGGREGATE
    aggregate_source = "상수 fallback (시트 미접속 — 공유설정 확인)"
    try:
        import kgf_sheet_sync
        _sheet_agg = kgf_sheet_sync.fetch_sheet_aggregate()
        if _sheet_agg:
            aggregate = _sheet_agg
            aggregate_source = f"구글시트 동기화 (as_of {_sheet_agg.get('as_of')})"
    except Exception:
        pass

    # 신규 승인 산출: 시트 딜 목록을 직전 실행분과 비교 → 새로 추가된 건만.
    # 첫 실행(직전 목록 없음)엔 '최신 승인월' 딜을 신규로 간주(부트스트랩).
    def _dkey(d):
        return f"{d.get('company','')}|{d.get('date','')}|{d.get('amount_num','')}"
    cur_deals = aggregate.get("deals") or []
    prev_deals = ((existing.get("aggregate") or {}).get("deals")) or []
    new_approvals = []
    if cur_deals:
        prev_keys = {_dkey(d) for d in prev_deals}
        if prev_keys:
            new_approvals = [d for d in cur_deals if _dkey(d) not in prev_keys]
        else:
            mx = max((d.get("date", "") for d in cur_deals), default="")
            new_approvals = [d for d in cur_deals if d.get("date") == mx] if mx else []
        new_approvals = sorted(new_approvals, key=lambda d: d.get("seq", 0))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fund_name": "국민성장펀드",
        "confirmed_as_of": CONFIRMED_AS_OF,
        "baseline_source": "정본=구글시트(국민성장펀드_집행현황) → dashboard. 시트 fetch 성공 시 aggregate 자동 갱신, 실패 시 상수 fallback (발송지침 4-B).",
        "aggregate_source": aggregate_source,
        "lookback_days": days_back,
        "version": 2,
        "aggregate": aggregate,
        "new_approvals": new_approvals,
        "undisclosed_count": aggregate.get("undisclosed_count", 0),
        "confirmed_deals": confirmed,
        "news_discovered": discovered,
        "candidates": candidates,
        "matched_articles_this_run": len(articles),
        "confirmed_deals_count": len(confirmed),
    }
    atomic_write_json(out_path, payload)
    return payload


if __name__ == "__main__":
    here = Path(__file__).parent
    db = here / "store" / "articles.db"
    out = here / "kgf_execution_summary.json"
    if len(sys.argv) > 1:
        db = Path(sys.argv[1])
    res = run(db, out)
    print(f"confirmed={res['confirmed_deals_count']} "
          f"news_discovered={len(res['news_discovered'])} "
          f"matched_articles={res['matched_articles_this_run']} "
          f"candidates={len(res['candidates'])}")
    for d in res["confirmed_deals"]:
        print(f"  #{d['no']} {d['company_name']} | {d['amount_confirmed']} | "
              f"{d['sector']} | {d['approved']} | {d['status_label']} | "
              f"news={len(d['news_timeline'])}")
