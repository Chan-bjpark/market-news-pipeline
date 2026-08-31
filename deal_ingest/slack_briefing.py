"""
CEO 뉴스 브리핑 — 코드 발송기 (무인·확정).

GitHub Actions에서 실행. deal_ingest가 수집·커밋한 JSON을 읽어 브리핑을
조립하고 Slack Incoming Webhook으로 직접 발송한다.
LLM·Cowork·승인·크롬 불필요 → 매 평일 100% 무인 발송.

환경변수:
  SLACK_WEBHOOK_URL  (필수) — #news_claud 채널 Incoming Webhook URL
  BRIEFING_DIR       (선택) — JSON 경로, 기본 'deal_ingest'

데이터 무결성: JSON에 있는 값·URL만 사용. 없는 값은 '미확보'/'특별한 뉴스없음'.
어떤 섹션이 비어도 나머지로 발송을 완료한다(부분 실패로 전체 중단 금지).
"""
from __future__ import annotations
import json, os, re, sys, urllib.request
from datetime import datetime, timezone, timedelta

DIR = os.environ.get("BRIEFING_DIR", "deal_ingest")
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()

HOST_MAP = {
    "thebell.co.kr": "더벨", "dealsite.co.kr": "딜인사이트", "investchosun.com": "인베스트조선",
    "markets.hankyung.com": "마켓인사이트", "hankyung.com": "한국경제", "einfomax.co.kr": "연합인포맥스",
    "newspim.com": "뉴스핌", "yna.co.kr": "연합뉴스", "newsis.com": "뉴시스", "mk.co.kr": "매일경제",
    "mt.co.kr": "머니투데이", "edaily.co.kr": "이데일리", "sedaily.com": "서울경제",
    "fnnews.com": "파이낸셜뉴스", "biz.chosun.com": "조선비즈", "chosun.com": "조선일보",
    "news1.kr": "뉴스1", "heraldcorp.com": "헤럴드경제", "etoday.co.kr": "이투데이",
    "ajunews.com": "아주경제", "asiae.co.kr": "아시아경제", "g-enews.com": "글로벌이코노믹",
}


def load(name):
    p = os.path.join(DIR, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] load {name}: {e}", file=sys.stderr)
        return {}


def medialabel(url, fallback=""):
    u = (url or "").lower()
    for host, lab in HOST_MAP.items():
        if host in u:
            return lab
    return fallback or "출처"


def kst_date(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(KST).date()
    except Exception:
        return None


def kst_dt(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST)
    except Exception:
        return None


def recent(articles, hours=None):
    """직전 영업일 브리핑 이후 기사만 (전일 중복 노출 방지).

    평일: 28시간(전 영업일 발송 이후 + 소폭 여유).
    월요일: 74시간(금요일 발송 이후 주말분 포함).
    """
    if hours is None:
        hours = 74 if NOW.weekday() == 0 else 28
    cutoff = NOW - timedelta(hours=hours)
    out = []
    for a in articles:
        dt = kst_dt(a.get("published_at", ""))
        if dt and dt >= cutoff:
            out.append(a)
    return out


# ---------- 근접 중복(같은 사건·여러 매체) 제거 ----------
_STOP = set("의장 대표 사장 회장 부회장 계열사 주식 장내 규모 관련 종목 시장 그룹 사업 최대 최소 신규 기존 이달 내년 올해 상반기 하반기 국내 해외 억원 조원 만주 지분 본격 동시 선택 선택지 새 각각 등".split())
_THEME = set("hlb hlb그룹 금융위 금감원 기재부 재경부 정부 한국 미국 코스피 코스닥 증시".split())
_GEN = set("결정 추진 검토 전망 예상 발표 계획 논의 나서 밝혀 관측 확대 강화 완화 시동 향방 출범 조성한다 한다 위해 관련해".split())
_ACT = ["매수", "매도", "매각", "인수", "합병", "발행", "조성", "출시", "상장", "결성",
        "공모", "증자", "유상증자", "무상증자", "배당", "공시", "모집", "인상", "인하",
        "부도", "회생", "청산", "출자", "베팅", "도입"]


def _amt(t):
    """제목에서 금액 시그니처 추출(조/억) — 같은 딜(동일 금액) 병합용."""
    s = set()
    for m in re.findall(r"(\d+(?:\.\d+)?)\s*조", t or ""):
        s.add(m + "조")
    for m in re.findall(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*억", t or ""):
        s.add(m.replace(",", "") + "억")
    return s


def _dw(t):
    t = (t or "")
    for e in ("&quot;", "&amp;", "&lt;", "&gt;", "&#39;"):
        t = t.replace(e, "")
    t = re.sub(r"[^0-9A-Za-z가-힣]", " ", t)
    return [w.lower() for w in t.split() if w and not re.search(r"\d", w) and len(w) >= 2]


def _ents(ws):
    return [w for w in ws if w not in _STOP and w not in _THEME and w not in _GEN and w not in _ACT]


def _acts(ws):
    g = set()
    for w in ws:
        for a in _ACT:
            if w.startswith(a):
                g.add(a)
    return g


def _ematch(ea, eb):
    m = []
    for a in ea:
        for b in eb:
            if a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)):
                m.append(min(a, b, key=len))
    return m


def _same_story(ta, tb):
    wa, wb = _dw(ta), _dw(tb)
    me = _ematch(_ents(wa), _ents(wb))
    aa = _acts(set(wa)) & _acts(set(wb))
    am = _amt(ta) & _amt(tb)
    # 병합 조건: 공유 고유명 2개↑  OR  (공유 고유명 1개↑ AND 공유 행위 1개↑)
    #            OR (공유 고유명 1개↑ AND 동일 금액 1개↑)  OR  긴 고유명(8자↑) 공유
    return (len(set(me)) >= 2
            or (len(me) >= 1 and len(aa) >= 1)
            or (len(me) >= 1 and len(am) >= 1)
            or any(len(x) >= 8 for x in me))


def dedupe_stories(arts):
    """같은 사건을 여러 매체가 쓴 근접중복을 사건당 1건(먼저 나온 것)으로 축약."""
    kept = []
    for a in arts:
        if any(_same_story(a.get("title", ""), k.get("title", "")) for k in kept):
            continue
        kept.append(a)
    return kept


# ---------- 섹션 빌더 ----------
def sec_rates(rate):
    items = rate.get("items", {}) or {}
    mkt = rate.get("market", {}) or {}
    pol = rate.get("policy_rates", {}) or {}
    L = ["🏦 *[금리·채권시장 동향]*", "", "_주요 시장금리 (vs 전일 / 전월)_"]

    def line(label, key):
        d = items.get(key)
        if not d or d.get("error") or d.get("latest_value") in (None, ""):
            return f"• {label}: _미확보_"
        vd = d.get("vs_prev_day_bp"); vm = d.get("vs_prev_month_bp")
        vd = f"전일 {vd:+.1f}bp" if isinstance(vd, (int, float)) else "전일 –"
        vm = f"전월 {vm:+.1f}bp" if isinstance(vm, (int, float)) else "전월 –"
        return f"• {label}: {d.get('latest_value')}% ({vd}, {vm}) — {d.get('latest_date','')} 기준"

    L.append(line("CD 3개월", "CD(91일)"))
    L.append(line("국고채 3년", "국고채(3년)"))
    L.append(line("국고채 10년", "국고채(10년)"))
    L.append(line("미국채 2년", "미국채(2년)"))
    L.append(line("미국채 10년", "미국채(10년)"))
    L += ["", "_환율·유가_"]
    for label, key in (("원달러", "원달러환율"), ("WTI유가", "WTI유가")):
        d = mkt.get(key)
        if d and d.get("latest_value") not in (None, ""):
            L.append(f"• {label}: {d.get('latest_value')} — {d.get('latest_date','')} 기준")
        else:
            L.append(f"• {label}: _미확보_")
    L += ["", "_정책 기준금리_"]
    for label, key in (("한국", "한국 기준금리"), ("미국", "미국 기준금리")):
        d = pol.get(key) or {}
        val = d.get("latest_value_str") or d.get("latest_value")
        if val is None:
            L.append(f"• {label} 기준금리: _미확보_"); continue
        dir_ = d.get("meeting_direction") or d.get("direction", "")
        L.append(f"• {label} 기준금리: {val}% on {d.get('last_meeting','')} "
                 f"({dir_}, 다음 {d.get('next_meeting','')})".replace("%%", "%"))
    L.append("")
    L.append("출처: <https://ecos.bok.or.kr/|한국은행 ECOS> / <https://fred.stlouisfed.org/|FRED>")
    return "\n".join(L)


def sec_global(articles):
    hits = [a for a in recent(articles)
            if a.get("source") == "einfomax"
            and any(k in (a.get("title", "")) for k in ("뉴욕", "글로벌", "S&P", "나스닥", "미 국채", "국제유가", "연준", "FOMC"))]
    if not hits:
        return "🌐 *[글로벌 시장]*\n특별한 뉴스없음"
    hits = dedupe_stories(hits)
    L = ["🌐 *[글로벌 시장]*"]
    for a in hits[:4]:
        L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
    return "\n".join(L)


# 정책 뉴스 = 산업·경제·기업에 영향을 주는 '규제·세제·제도·통화정책의 변화'.
# 정부 지출/투자 발표, 시장 시황·반응, 사설/칼럼, 인사/거버넌스는 정책이 아님.

# (A) 규제·세제·제도 변화 키워드 — 그 자체로 제도성이 강한 것만(느슨한 단어 배제)
_REG_POS = [
    # 세제
    "법인세", "소득세", "상속세", "증여세", "양도소득세", "양도세", "종부세", "종합부동산세",
    "부동산 세제", "부동산세제", "재산세", "취득세", "금투세", "금융투자소득세",
    "세제개편", "세제 개편", "세법개정", "세법 개정", "비과세", "분리과세", "감세", "증세", "세액공제",
    # 자본시장 제도
    "공매도", "밸류업 프로그램", "밸류업 지수", "밸류업 공시", "밸류업 가이드라인", "밸류업 지원", "밸류업 세제",
    "상장폐지", "상장 폐지", "퇴출기준", "퇴출 기준", "상장규정", "상장 요건",
    "배당소득 분리과세", "스튜어드십", "의무보유", "보호예수", "공시 의무", "공시제도",
    # 법·규제 변화
    "자본시장법", "상법 개정", "상법개정", "공정거래법", "금융지주회사법", "은행법", "보험업법",
    "시행령", "시행규칙", "고시 개정", "입법예고", "규제 완화", "규제완화", "규제 강화", "규제강화",
    "규제개혁", "규제 개혁", "감독규정", "제도 개선방안", "의무화", "허용키로", "금지키로", "도입키로",
]
# 시황·시장반응·사설/칼럼·인사 → 정책 아님(배제)
_POLICY_NEG = ["사설", "칼럼", "기고", "오피니언", "시론", "취재수첩", "기자수첩", "데스크",
               "마감]", "[채권", "[증시", "[마켓", "칠천피", "육천피", "스팁", "엔비디아",
               "달러화 강세", "연임", "취임", "선임", "동정]", "포토]", "인사]"]
_RATE_VERB = ("인상", "인하", "동결", "의결", "결정", "찬성", "소수의견", "점도표", "통화정책방향")


def _is_rate_decision(a):
    """한국 통화정책 '결정' 뉴스(시장 시황·해설 아님)."""
    t = a.get("title", "") + (a.get("summary") or "")
    if any(m in t for m in _POLICY_NEG):
        return False
    if not any(k in t for k in ("금통위", "통화정책방향", "기준금리")):
        return False
    return any(v in t for v in _RATE_VERB)


def _rate_score(a):
    t = a.get("title", "") + (a.get("summary") or "")
    s = 0
    for k, v in (("찬성", 2), ("의결", 2), ("소수의견", 2), ("[전문]", 2),
                 ("통화정책방향", 1), ("인상", 1), ("인하", 1), ("동결", 1),
                 ("3.00", 1), ("점도표", 1)):
        if k in t:
            s += v
    return s


def sec_policy(articles, fund):
    L = ["🏛️ *[한국 정책·제도 변화]*"]
    rc = recent(articles)
    # (1) 통화정책 결정 — 최대 2건(대표만)
    rate = dedupe_stories(sorted([a for a in rc if _is_rate_decision(a)],
                                 key=_rate_score, reverse=True))[:2]
    # (2) 규제·세제·제도 변화
    reg = []
    for a in rc:
        t = a.get("title", "") + " " + (a.get("summary") or "")
        if any(j in t for j in _POLICY_NEG):
            continue
        if any(k in t for k in _REG_POS):
            reg.append(a)
    reg.sort(key=lambda a: a.get("published_at", ""), reverse=True)
    reg = dedupe_stories(reg)
    seen = {a.get("url") for a in rate}
    n = 0
    for a in rate:
        L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
        n += 1
    for a in reg:
        if a.get("url") in seen:
            continue
        seen.add(a.get("url"))
        L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
        n += 1
        if n >= 6:
            break
    if n == 0:
        L.append("• 특별한 뉴스없음")
    funds = (fund.get("funds") or [])
    funds = sorted(funds, key=lambda f: f.get("last_updated", ""), reverse=True)[:4]
    if funds:
        L += ["", "*🧭 정책자금 펀드 조성 추적 (90일 누적)*"]
        for f in funds:
            L.append(f"• {f.get('fund_name','')} — {f.get('current_stage_label','')} ({f.get('last_updated','')})")
            tl = (f.get("timeline") or [])[:1]
            for t in tl:
                lab = medialabel(t.get("url"), t.get("source", ""))
                amt = f" {t.get('amount')}" if t.get("amount") else ""
                url = f" <{t.get('url')}|{lab}>" if t.get("url") else ""
                L.append(f"  · {t.get('date','')} {t.get('stage_label','')}{amt}{url}")
    return "\n".join(L)


# 국민성장펀드 '추가 집행' 보도 탐지(대시보드 미반영 점검용 최소 경보).
# 정본=구글시트 수동 갱신이므로, 뉴스로 새 집행이 잡히면 반영 여부만 확인하도록 알린다.
_KGF_KEYS = ("국민성장펀드", "국민참여성장펀드")
_KGF_EXEC = ("직접투자", "저리대출", "정책대출", "인프라", "집행", "의결", "승인", "선정",
             "출자", "지원", "투자", "대출")
_KGF_NEG = ("사설", "칼럼", "기고", "오피니언", "마중물", "전망대", "데스크", "취재수첩", "기자수첩",
            "포럼", "확대", "증액", "2호", "상생·성장", "상생성장", "뱅크샐러드", "연임", "홈런",
            "대해부", "질서 회복", "200조", "브리핑", "걸림돌", "중복상장", "수출입은행")


def _kgf_exec_alert(articles):
    """국민성장펀드 추가 집행으로 '보도된' 건(회사+금액+집행신호). 대시보드 반영 여부 점검용."""
    cand = []
    for a in recent(articles or []):
        t = a.get("title", "") + " " + (a.get("summary") or "")
        if not any(k in t for k in _KGF_KEYS):
            continue
        if any(j in t for j in _KGF_NEG):
            continue
        if not any(e in t for e in _KGF_EXEC):
            continue
        if not re.search(r"\d+(?:\.\d+)?\s*조|\d[\d,]*\s*억", t):
            continue
        cand.append(a)
    return dedupe_stories(cand)


def sec_kgf(kgf, articles=None):
    agg = kgf.get("aggregate") or {}
    if not agg.get("approved_count"):
        L = ["🇰🇷 *[국민성장펀드 집행 추적 (90일 누적)]*",
             "세부현황: <https://national-growth-fund.web.app/|국민성장펀드 집행현황 대시보드>"]
    else:
        b = agg.get("breakdown") or {}
        def bd(k):
            x = b.get(k) or {}
            return f"{x.get('count','?')}건 {x.get('amount','')}"
        L = [f"🇰🇷 *[국민성장펀드 집행 추적 (확인 {agg.get('approved_count')}건·{agg.get('cumulative_amount','')} / 90일 누적)]*",
             f"_누적 {agg.get('approved_count')}건·{agg.get('cumulative_amount','')} (자료 기준 {agg.get('as_of','')}). "
             f"직접투자 {bd('direct_investment')} · 인프라 {bd('infrastructure')} · "
             f"저리대출 {bd('low_interest_loan')} · 간접펀드 {bd('indirect_fund')}. "
             f"첨단전략산업기금 {agg.get('frontier_fund_amount','')} 포함_",
             "세부현황: <https://national-growth-fund.web.app/|국민성장펀드 집행현황 대시보드>"]
    alert = _kgf_exec_alert(articles)
    if alert:
        as_of = agg.get("as_of", "")
        L.append("")
        L.append(f"⚠️ *추가 집행 보도 — 대시보드 미반영 점검* (대시보드 기준 {as_of})")
        for a in alert[:3]:
            d = kst_dt(a.get("published_at", ""))
            ds = d.strftime("%m.%d") if d else ""
            L.append(f"• [{ds}] {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
        L.append("_↑ 뉴스로 보도된 집행 건. 대시보드 반영 여부 확인 후 구글시트 수동 갱신 요망._")
    return "\n".join(L)


def sec_hlb(articles):
    L = ["🔬 *[HLB그룹 관련]*"]
    sig = [a for a in recent(articles) if a.get("category") == "hlb_signal"]
    names = ["HLB", "넥스트사이언스", "리보세라닙", "에이치엘비"]
    flow = [a for a in recent(articles)
            if a.get("category") == "flow_index" and any(n in a.get("title", "") for n in names)]
    seen = set(); rows = []
    for a in sig + flow:
        if a.get("url") in seen:
            continue
        # 코스피 전체 시황 배제
        t = a.get("title", "")
        if ("코스피" in t or "코스닥" in t) and not any(n in t for n in names):
            continue
        seen.add(a.get("url")); rows.append(a)
    rows = dedupe_stories(rows)
    if not rows:
        L.append("특별한 뉴스없음")
    else:
        for a in rows[:6]:
            L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
    return "\n".join(L)


def sec_distress(articles):
    crisis = ["관리종목", "상장폐지", "감사의견", "자본잠식", "회생", "법정관리", "워크아웃",
              "유동성위기", "디폴트", "매각설", "매각 추진", "매각주관사", "M&A 매물", "구조조정"]
    sector = ["제약", "바이오", "건기식", "K뷰티", "화장품", "의료기기", "진단", "신약", "임상", "헬스"]
    L = ["🆘 *[한계기업·저가매수 기회 (제약·바이오·K뷰티·헬스)]*",
         "※ 모니터링 목적. DD 트리거 별도 필요."]
    rows = []; seen = set()
    for a in recent(articles):
        txt = a.get("title", "") + (a.get("summary") or "")
        if any(c in txt for c in crisis) and any(s in txt for s in sector):
            if a.get("url") in seen:
                continue
            seen.add(a.get("url")); rows.append(a)
    rows = dedupe_stories(rows)
    if not rows:
        L.append("특별한 뉴스없음")
    else:
        for a in rows[:5]:
            L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
    return "\n".join(L)


# ---------- 딜 품질 스코어링 (소소한 기사 배제·주요 딜 우선) ----------
_DEAL_ACT = ["인수", "매각", "합병", "경영권", "지분", "투자유치", "출자", "결성", "클로징",
             "바이아웃", "물적분할", "인적분할", "스핀오프", "프리ipo", "프리 ipo", "시리즈",
             "주관사", "우선협상", "본입찰", "예비입찰", "매물", "인수전", "콜옵션", "풋옵션",
             "ipo", "상장", "m&a", "블라인드", "펀드 조성", "펀드조성", "앵커", "라이선스",
             "기술수출", "매각가", "인수가", "지주", "완전자회사", "합작", "jv", "공모", "조성",
             "gp", "위탁운용", "앵커출자", "펀드결성"]
_DEAL_JUNK = ["에세이", "칼럼", "출간", "별세", "부고", "인생", "여정", "자서전", "회고록",
              "르포", "기고", "오피니언", "소탈", "온화", "브런치", "레시피", "맛집",
              "다이소", "홍삼", "인터뷰[", "책 출간", "신간", "수필",
              "로펌이슈", " 진단]", "평행이론", "영입", "위촉", "취임", "연임", "시론", "사설",
              "취재수첩", "기자수첩", "전망대", "동정]", "포토]", "부음", "인사]",
              "[사람]", "일문일답", "라이프", "여행", "골프", "와인", "부고·인사"]
_PE_NAMES = ["mbk", "한앤컴퍼니", "한앤코", "imm", "스틱", "글랜우드", "어피너티", "칼라일",
             "kkr", "블랙스톤", "베인", "앵커에쿼티", "vig", "jkl", "스카이레이크", "에이티넘",
             "프랙시스", "센트로이드", "케이스톤", "노틱", "유니슨", "얼라인", "오케스트라",
             "스톤브릿지", "웰투시", "크레센도", "린드먼", "프리미어", "국민성장펀드"]
_PHARMA = ["제약", "바이오", "신약", "임상", "헬스케어", "진단", "의료기기", "cdmo", "cro",
           "바이오텍", "세포치료", "유전자", "백신", "항체", "펩타이드", "원료의약", "제약사"]


def _deal_score(a):
    t = (a.get("title", "") + " " + (a.get("summary") or "")).lower()
    pharma = any(k in t for k in _PHARMA)
    if not any(k in t for k in _DEAL_ACT):
        return (False, 0, pharma)
    if any(j in t for j in _DEAL_JUNK):
        return (False, 0, pharma)
    score = 1
    if re.search(r"\d[\d,]*\s*조", t) or re.search(r"\d[\d,]*\s*억", t):
        score += 3
    if any(n in t for n in _PE_NAMES):
        score += 3
    for k, s in (("경영권", 2), ("바이아웃", 2), ("우선협상", 2), ("본입찰", 2),
                 ("예비입찰", 2), ("매각주관사", 2), ("인수전", 2), ("결성", 2),
                 ("클로징", 2), ("출자", 2), ("앵커", 2), ("기술수출", 2),
                 ("라이선스", 2), ("주관사", 1), ("투자유치", 1), ("시리즈", 1),
                 ("프리ipo", 1)):
        if k in t:
            score += s
    if pharma:
        score += 2
    return (True, score, pharma)


def sec_deals(articles):
    order = ["lp_commit", "fund_raise", "M&A", "PE", "cap_market"]
    cap = {"lp_commit": 3, "fund_raise": 3, "M&A": 6, "PE": 4, "cap_market": 4}
    label = {"M&A": "M&A", "PE": "PE동향", "fund_raise": "PE동향",
             "lp_commit": "기관출자", "cap_market": "자본시장", "other": "거버넌스"}
    L = ["🤝 *[딜·M&A]*"]
    rc = recent(articles)
    # 1) 후보 수집 + 품질 스코어링(딜 신호 없음·잡문 제외)
    cand = []; seen = set()
    for cat in order:
        for a in rc:
            if a.get("category") != cat or a.get("url") in seen:
                continue
            seen.add(a.get("url"))
            ok, sc, ph = _deal_score(a)
            if not ok:
                continue
            cand.append((cat, a, sc, ph))
    # 2) 점수 높은 것 우선으로 정렬 후 근접중복 제거(대표는 고점수 유지)
    cand.sort(key=lambda x: x[2], reverse=True)
    kept = []; kept_arts = []
    for cat, a, sc, ph in cand:
        if any(_same_story(a.get("title", ""), k.get("title", "")) for k in kept_arts):
            continue
        kept.append((cat, a, sc, ph)); kept_arts.append(a)
    # 3) 제약·바이오 딜 먼저, 그다음 점수순 — 카테고리별 상한·전체 상한 적용
    kept.sort(key=lambda x: (0 if x[3] else 1, -x[2]))
    cnt = {}; count = 0
    for cat, a, sc, ph in kept:
        if cnt.get(cat, 0) >= cap[cat]:
            continue
        lab = label.get(cat)
        prefix = f"[💊{lab}]" if ph else f"[{lab}]"
        L.append(f"{prefix} {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
        cnt[cat] = cnt.get(cat, 0) + 1; count += 1
        if count >= 15:
            break
    if count == 0:
        L.append("특별한 뉴스없음")
    return "\n".join(L), count


def post(text):
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(WEBHOOK, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    if not WEBHOOK:
        print("SLACK_WEBHOOK_URL 미설정", file=sys.stderr); sys.exit(1)
    # 주말 미발송
    if NOW.weekday() >= 5:
        print("주말 — 발송 안 함"); return
    rate = load("rate_summary.json")
    rb = load("recent_briefing.json")
    fund = load("fund_flow_summary.json")
    kgf = load("kgf_execution_summary.json")
    arts = rb.get("articles", []) or []

    # 신선도: 오늘 수집분인지
    gen = kst_date(rb.get("generated_at", ""))
    stale = "" if gen == TODAY else f" _(데이터 {gen} 기준)_"

    head = f"📰 *오늘의 CEO 뉴스 브리핑* — {TODAY} ({'월화수목금토일'[NOW.weekday()]}){stale}"
    deals, dcount = sec_deals(arts)
    part1 = "\n\n".join([head, sec_rates(rate), sec_global(arts),
                         sec_policy(arts, fund), sec_kgf(kgf, arts)])
    part2 = "\n\n".join([f"📰 *CEO 뉴스 브리핑* — {TODAY} [2/2]",
                         sec_hlb(arts), sec_distress(arts), deals,
                         "---\n_코드 자동발송 | Claude CEO 브리핑_"])
    post(part1)
    post(part2)
    print(f"발송 완료: 딜 {dcount}건, HLB/한계기업/정책 포함")


if __name__ == "__main__":
    main()
