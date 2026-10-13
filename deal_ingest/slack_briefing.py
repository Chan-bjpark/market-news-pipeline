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


# ── 정책 섹션: 정부 보도자료 검증 기반 (지침 v0.4) ──
# 흐름: 4부처(금융위·기재부/재경부·공정위·거래소) 보도자료에서 중요 규제·자본시장·세제
#       변화만 추출(소분류는 내부 필터용, 출력엔 미표기) → 이를 가장 잘 반영한 뉴스 1건으로
#       압축 노출. 매칭 뉴스가 없으면 보도자료 링크를 직접 노출. 아무것도 없으면 '특이사항 없음'.
#   필터 범위: ⑴상장사 규제 ⑵자본시장 큰 변화 정책 ⑶중요 세제(법인세·부가세·부동산세제 등)
# v0.5: '제도 변화' 집중(2026-10-13 브라더 지시). 개별 집행사건(담합 심의·감리결과 조치 등)은
#       M&A 신호로 다른 섹션 몫 → 이 섹션은 '규칙/제도 변경'만. ⇒ ⒜범위 토픽(POS) ∧ ⒝규칙변경
#       신호(_POL_RULECHG) 둘 다 있어야 채택. 공정위 enforcement 어휘(담합·과징금 등) 제거.
_POL_POS = {
    "세제": ["법인세", "부가세", "부가가치세", "소득세", "상속세", "증여세", "양도세", "양도소득세",
             "종부세", "종합부동산세", "부동산세", "취득세", "재산세", "금투세", "금융투자소득세",
             "세제개편", "세제 개편", "세법개정", "세법 개정", "과세", "비과세", "분리과세",
             "세율", "세액공제", "감세", "증세"],
    "자본시장·상장규제": ["자본시장법", "상법 개정", "상법개정", "금융지주회사법", "외국환거래법",
             "은행법", "보험업법", "공매도", "밸류업", "상장폐지", "상장 폐지", "퇴출기준",
             "퇴출 기준", "퇴출", "상장적격성", "실질심사", "관리종목", "저성과기업",
             "상장요건", "상장 요건", "상장규정", "공시의무", "공시 의무", "공시제도",
             "내부회계", "스튜어드십", "자사주", "의무보유", "보호예수", "불공정거래", "사업보고서",
             "감리", "회계감리", "고시", "감독규정", "대손인정제도"],
    "공정거래": ["공정거래법", "하도급법", "가맹사업법", "대규모유통업법", "표시광고", "표시·광고법",
             "기업결합 심사", "기업결합신고", "기업결합 신고", "심사기준", "심사지침"],
    "기업지배구조": ["이사의 충실의무", "충실의무", "집중투표", "감사위원", "지배구조 개선",
             "소수주주", "주주환원"],
}
# 노이즈(제외): 펀드 판매·조성(→④국민성장/정책자금 섹션), 동정·인사, 상품안내·서민금융, 의견·행사
_POL_NEG = ["판매 결과", "잔여물량", "판매 현황", "국민참여성장펀드", "국민성장펀드", "성장펀드",
            "면담", "간담회", "동정", "축사", "수상", "위촉", "임명", "적금", "가입신청",
            "청년미래", "소상공인 지원", "서민금융", "바자회", "기부", "채용", "체결식",
            "사설", "칼럼", "기고", "오피니언", "포토", "브리핑]"]
# 규칙/제도 변경 신호(필수 게이트). 이게 없으면 개별 집행·결과 보도로 간주해 제외.
_POL_RULECHG = ["개정", "개정안", "제정", "시행", "시행령", "시행규칙", "입법예고", "규정변경예고",
                "공포", "신설", "도입", "폐지", "완화", "강화", "의무화", "가이드라인", "로드맵",
                "기본계획", "종합계획", "개선방안", "정책방향", "정책 방향"]
# 확정단계(우선순위 가중 — 실제 확정/시행 쪽을 상위로)
_POL_STAGE = ["시행", "공포", "의결", "확정", "개정", "개정안", "입법예고", "규정변경예고"]

_AGENCY_LABEL = {"금융위": "금융위 보도자료", "FSC": "금융위 보도자료",
                 "기재부": "기재부 보도자료", "재경부": "재경부 보도자료", "MOEF": "기재부 보도자료",
                 "공정위": "공정위 보도자료", "KFTC": "공정위 보도자료",
                 "거래소": "한국거래소 보도자료", "KRX": "한국거래소 보도자료"}


def _unesc(t):
    """표시용 HTML 엔티티 복원(&quot; 등)."""
    t = (t or "")
    for e, c in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'")):
        t = t.replace(e, c)
    return t


def _clean_rel_title(t):
    """정부 보도자료 원제목 → Slack 헤드라인(길이·부연 정리). 의미 왜곡 없이 경량 처리."""
    t = (t or "").strip()
    t = re.sub(r"\s*[-–]\s*[^-–]*위원회[^-–]*[-–]\s*$", "", t)  # 끝의 '- 제n차 …위원회 … -' 부연 제거
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 60:                                            # 설명형 첫 문장이 헤드라인이면 거기서 끊기
        head = re.split(r"(?<=[다요음])\.\s", t, maxsplit=1)[0]
        t = head if 10 <= len(head) <= 60 else t
    if len(t) > 72:
        cut = t[:72].rsplit(" ", 1)[0]
        t = (cut if len(cut) >= 40 else t[:72]) + "…"
    return t.strip()


def _policy_classify(title):
    """보도자료 제목 → (소분류, 확정단계여부) 또는 None(제외). 소분류는 내부용.
    채택 = 범위 토픽(POS) ∧ ¬노이즈(NEG) ∧ 규칙변경 신호(RULECHG). 개별 집행·결과 보도는 제외."""
    t = title or ""
    if any(n in t for n in _POL_NEG):
        return None
    if not any(rc in t for rc in _POL_RULECHG):  # 규칙/제도 변경 신호 필수
        return None
    for cat, kws in _POL_POS.items():
        if any(kw in t for kw in kws):
            return (cat, any(s in t for s in _POL_STAGE))
    return None


# 보도자료 제목은 문장형이라 _ents만으로는 부사·연결어가 샌다(예: '앞으로도','일관되게').
# 뉴스 매칭 때만 쓰는 추가 불용어(필러) + 코퍼스 문서빈도(DF) 희소어 조건으로 오매칭 차단.
_POL_FILLER = set("앞으로도 일관되게 지속적으로 적극적으로 통해 위해 위한 대한 관련 관련해 따라 "
                  "개선 방향 추진 마련 운영 지원 강화 확대 점검 추진한다 밝혔다 예정 등에 등을 "
                  "제도 정책 방안 계획 사항 결과 현황 내용 경우 기관 당국 정부 올해 내년".split())


def _news_common_tokens(rc, frac=0.05, floor=6):
    """최근 뉴스 코퍼스에서 흔한(=비변별) 토큰 집합. 희소어만 매칭키로 쓰기 위함."""
    df = {}
    for a in rc:
        for w in set(_ents(_dw(a.get("title", "")))):
            df[w] = df.get(w, 0) + 1
    thr = max(floor, int(len(rc) * frac))
    return {w for w, c in df.items() if c >= thr}


def _key_ents(title, common):
    """매칭에 쓸 변별 토큰: 불용어·필러·코퍼스 흔한말 제거, 2자 이하 제거."""
    return [w for w in _ents(_dw(title))
            if len(w) >= 3 and w not in _POL_FILLER and w not in common]


def _best_news_for(rel_title, rc, common):
    """보도자료와 '변별 고유명'이 겹치는 뉴스 중 최적 1건. 흔한말·부사 매칭은 배제."""
    re_ents = _key_ents(rel_title, common)
    if not re_ents:
        return None
    best = None
    best_key = (0, 0)
    for a in rc:
        shared = _ematch(re_ents, _key_ents(a.get("title", ""), common))
        strong = {x for x in shared if len(x) >= 3}
        if not strong:
            continue
        # 매칭 성립: 변별토큰 2개↑  OR  길이 5자↑ 변별토큰 1개(예: 외국환거래법·저성과기업·대손인정제도).
        # 4자 이하 광범위 토픽어(자본시장·기업결합 등) 단독 매칭은 오매칭이라 불허 → 직접노출로 넘김.
        if not (len(strong) >= 2 or any(len(x) >= 5 for x in strong)):
            continue
        key = (len(strong), max(len(x) for x in strong))
        if key > best_key:
            best_key = key
            best = a
    return best


def _rel_fresh(date_str):
    """보도자료 '직접 노출'은 최근분만(평일 2일/월요일 4일 윈도우).
    매칭 뉴스가 있으면 뉴스 신선도로 이미 판단하므로 이 게이트는 무매칭 직접노출에만 적용."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    span = 4 if NOW.weekday() == 0 else 2
    return d >= (NOW.date() - timedelta(days=span))


def sec_policy(articles, releases):
    L = ["🏛️ *[한국 정책·제도 변화]*"]
    rc = recent(articles)
    rels = (releases or {}).get("releases") or []
    # 1) 지침 필터
    kept = []
    for r in rels:
        cls = _policy_classify(r.get("title", ""))
        if not cls:
            continue
        r = dict(r)
        r["_cat"], r["_stage"] = cls
        kept.append(r)
    # 2) 확정단계 우선 + 최신순
    kept.sort(key=lambda r: (bool(r.get("_stage")), r.get("date", "")), reverse=True)
    # 3) 보도자료 간 근접중복 제거
    dkept = []
    for r in kept:
        if any(_same_story(r.get("title", ""), k.get("title", "")) for k in dkept):
            continue
        dkept.append(r)
    # 4) 각 보도자료 → 뉴스 1건 압축 / 없으면 보도자료 링크 직접 노출
    lines = []
    used = set()
    common = _news_common_tokens(rc)
    for r in dkept:
        news = _best_news_for(r.get("title", ""), rc, common)
        if news and news.get("url") not in used:
            used.add(news.get("url"))
            lines.append(f"• {_unesc(news.get('title','')).strip()} "
                         f"<{news.get('url')}|{medialabel(news.get('url'), news.get('source_label'))}>")
        elif not news and _rel_fresh(r.get("date", "")):
            # 매칭 뉴스 없음 + 보도자료가 최근분 → 보도자료 링크 직접 노출
            lab = _AGENCY_LABEL.get(r.get("agency", ""),
                                    (r.get("agency", "") + " 보도자료").strip() or "보도자료")
            title = _clean_rel_title(r.get("title", ""))
            url = r.get("url", "")
            lines.append(f"• {title} <{url}|{lab}>" if url else f"• {title} ({lab})")
        # else: (뉴스 매칭 실패 + 보도자료도 오래됨) → 스킵
    L += lines if lines else ["• 특이사항 없음"]
    # 수집 실패 부처 경고(헬스)
    srcs = (releases or {}).get("sources") or {}
    failed = [a for a, s in srcs.items() if isinstance(s, dict) and not s.get("ok", True)]
    if failed:
        L.append(f"  _⚠ 보도자료 수집 실패: {', '.join(failed)} — 해당 부처 누락 가능_")
    return "\n".join(L)


# 정책펀드 '조성 단계 변동' 탐지(④ 섹션의 종속 블록).
# fund_flow엔 단계변동 플래그가 없고 timeline이 일일 뉴스까지 흡수하므로, '실제 단계'(단계 미상 제외)
# 이벤트가 최근 윈도우 내일 때만 변동으로 본다. 국민성장펀드는 집행 섹션이 본류라 조성에선 제외(중복).
def _fund_stage_changes(fund):
    out = []
    for f in (fund.get("funds") or []):
        name = f.get("fund_name", "")
        if "국민성장" in name or "국민참여성장" in name:
            continue
        stage = f.get("current_stage_label", "")
        if stage in ("", "단계 미상", None):
            continue
        evt = None
        for t in (f.get("timeline") or []):          # timeline은 날짜 오름차순 → 마지막 매칭이 최신
            sl = t.get("stage_label")
            if sl and sl != "단계 미상" and _rel_fresh(t.get("date", "")):
                evt = t
        if evt:
            out.append((name, evt))
    return out


def sec_kgf(kgf, fund=None):
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
    # (1) 시트에 새로 반영된 신규 승인 — 정본(구글시트) 기준, 요약
    new_appr = kgf.get("new_approvals") or []
    if new_appr:
        as_of = agg.get("as_of", "")
        tot = sum(d.get("amount_num", 0) or 0 for d in new_appr)
        jo, rem = divmod(tot, 10000)
        tot_str = (f"{jo}조{rem:,}억원" if jo and rem else f"{jo}조원" if jo else f"{tot:,}억원")
        L.append("")
        L.append(f"🆕 *신규 승인 {len(new_appr)}건 · {tot_str}* (시트 반영 기준 {as_of})")
        for d in new_appr[:10]:
            comp = d.get("company", ""); amt = d.get("amount", "")
            meth = d.get("method", ""); dt = d.get("date", "")
            sec = f" · {d.get('sector')}" if d.get("sector") else ""
            L.append(f"• {comp} — {amt} ({meth}{sec}, {dt})")
    # (2) 기타 정책펀드 조성 단계 '변동시만' 1~2줄 (국민성장 제외)
    chg = _fund_stage_changes(fund or {})
    if chg:
        L.append("")
        L.append("*🧭 정책펀드 조성 단계 변동*")
        for name, evt in chg[:3]:
            amt = f" {evt.get('amount')}" if evt.get("amount") else ""
            url = evt.get("url")
            src = medialabel(url, evt.get("source") or "출처") if url else ""
            link = f" <{url}|{src}>" if url else ""
            L.append(f"• {name} — {evt.get('stage_label','')}{amt} ({evt.get('date','')}){link}")
    return "\n".join(L)


# HLB 섹션 목적(2026-10-13 지시): FDA 승인·임상 같은 '큰 이슈'에 언론이 어떻게 반응하는지 확인.
# → 주가·수급·공시·IR행사는 원천 배제. 큰 이슈 기사 반응을 ~3건으로 요약(건수 안 채워도 됨).
_HLB_NAMES = ["HLB", "에이치엘비", "넥스트사이언스", "리보세라닙", "리보세라닙", "캄렐리", "네수파립"]
_HLB_BIG = ["fda", "3상", "2상", "임상", "승인", "허가", "nda", "bla", "품목허가", "시판",
            "재신청", "재심사", "보완", "crl", "적응증", "병용", "탑라인", "유효성",
            "1차 치료제", "1차치료제", "신약", "간암", "선낭암", "우선심사", "패스트트랙",
            "가속승인", "혁신치료제", "학회", "asco", "esmo", "허가신청", "승인신청"]
# 주가·수급·시황·공시·IR행사 = 반응확인 대상 아님(원천 배제)
_HLB_EXCL = ["주가", "목표주가", "수급", "시황", "거래량", "급등", "급락", "상한가", "하한가",
             "블록딜", "공시", "유상증자", "전환사채", "자사주", "시가총액", "매도", "매수세",
             "통합 ir", "ir 데이", "ir데이", "기업설명회", "설명회", "ir 개최", "ir데이"]


def sec_hlb(articles):
    L = ["🔬 *[HLB그룹 주요 이슈]*"]
    rc = recent(articles)
    cand = []
    for a in rc:
        if a.get("category") == "flow_index":
            continue
        tl = a.get("title", "")
        t = (tl + " " + (a.get("summary") or "")).lower()
        named = any(n.lower() in tl.lower() for n in _HLB_NAMES)
        if not (a.get("category") == "hlb_signal" or named):
            continue
        if a.get("category") != "hlb_signal" and not named:
            continue
        if any(x in t for x in _HLB_EXCL):            # 주가·수급·공시·IR행사 배제
            continue
        if ("코스피" in tl or "코스닥" in tl) and not named:
            continue
        if not any(b in t for b in _HLB_BIG):          # '큰 이슈'(임상·규제·허가)만
            continue
        cand.append(a)
    cand.sort(key=lambda a: a.get("published_at", ""), reverse=True)
    # 매체 다양성 우선으로 반응 ~3건(같은 이슈 다매체 반응이 핵심이라 story 중복제거는 하지 않음)
    picked, seen_url, seen_src = [], set(), set()
    for a in cand:
        if a.get("url") in seen_url:
            continue
        src = medialabel(a.get("url"), a.get("source_label"))
        if src in seen_src and len(cand) > 3:          # 서로 다른 매체 우선
            continue
        picked.append(a); seen_url.add(a.get("url")); seen_src.add(src)
        if len(picked) >= 3:
            break
    for a in cand:                                      # 3건 못 채우면 남은 것으로 보충
        if len(picked) >= 3:
            break
        if a.get("url") in seen_url:
            continue
        picked.append(a); seen_url.add(a.get("url"))
    if not picked:
        L.append("• 특이사항 없음")
    else:
        for a in picked:
            L.append(f"• {_unesc(a.get('title','')).strip()} "
                     f"<{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
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
              "[사람]", "일문일답", "라이프", "여행", "골프", "와인", "부고·인사",
              "ir데이", "ir 데이", "통합 ir", "기업설명회", "설명회", "예금자보호", "창업 끝"]
_PE_NAMES = ["mbk", "한앤컴퍼니", "한앤코", "imm", "스틱", "글랜우드", "어피너티", "칼라일",
             "kkr", "블랙스톤", "베인", "앵커에쿼티", "vig", "jkl", "스카이레이크", "에이티넘",
             "프랙시스", "센트로이드", "케이스톤", "노틱", "유니슨", "얼라인", "오케스트라",
             "스톤브릿지", "웰투시", "크레센도", "린드먼", "프리미어"]
# 시황·매크로·국감·실적 지수기사 = 딜 아님. '강한 딜 신호' 없이 이 말이 제목에 있으면 제외.
_DEAL_NOISE = ["시황", "마감", "장중", "개장", "급락", "급등", "강세", "약세", "소외", "향배",
               "cpi", "fomc", "어닝", "컨센서스", "매크로", "훈풍", "등락", "코스피", "코스닥",
               "나홀로", "증시 향배", "환율", "국감", "국정감사", "실적 줄줄이", "선 붕괴", "선 터치"]
def _deal_score(a):
    title = a.get("title", "")
    t = (title + " " + (a.get("summary") or "")).lower()
    tl = title.lower()
    # 국민성장펀드는 ④ 전용 섹션에서 다룸 → 딜 섹션에서 제외
    if "국민성장펀드" in t or "국민참여성장펀드" in t:
        return (False, 0)
    # 딜 액션은 '제목'에 있어야 함(딜은 헤드라인에 드러남; 요약만 걸리는 시황·실적 기사 차단)
    if not any(k in tl for k in _DEAL_ACT):
        return (False, 0)
    if any(j in t for j in _DEAL_JUNK):
        return (False, 0)
    # 시황·국감·실적 지수기사는 '제목' 기준 무조건 제외
    if any(n in tl for n in _DEAL_NOISE):
        return (False, 0)
    amt = bool(re.search(r"\d[\d,]*\s*조", t) or re.search(r"\d[\d,]*\s*억", t))
    pe = any(n in t for n in _PE_NAMES)
    score = 1
    if amt:
        score += 3
    if pe:
        score += 3
    for k, s in (("경영권", 2), ("바이아웃", 2), ("우선협상", 2), ("우협", 2), ("본입찰", 2),
                 ("예비입찰", 2), ("인수", 2), ("매각", 2), ("합병", 2), ("인수전", 2),
                 ("결성", 2), ("클로징", 2), ("출자", 2), ("앵커", 2), ("기술수출", 2),
                 ("라이선스", 2), ("주관사", 1), ("투자유치", 1), ("시리즈", 1), ("프리ipo", 1)):
        if k in t:
            score += s
    return (True, score)


def _sec_deals_rule(articles):
    order = ["lp_commit", "fund_raise", "M&A", "PE", "cap_market"]
    cap = {"lp_commit": 3, "fund_raise": 3, "M&A": 6, "PE": 4, "cap_market": 4}
    label = {"M&A": "M&A", "PE": "PE동향", "fund_raise": "PE동향",
             "lp_commit": "기관출자", "cap_market": "자본시장", "other": "거버넌스"}
    L = ["🤝 *[딜·M&A]*"]
    rc = recent(articles)
    # 1) 후보 수집 + 품질 스코어링(시황·국감·실적 오분류·잡문 제외, 국민성장펀드 제외)
    cand = []; seen = set()
    for cat in order:
        for a in rc:
            if a.get("category") != cat or a.get("url") in seen:
                continue
            seen.add(a.get("url"))
            ok, sc = _deal_score(a)
            if not ok:
                continue
            cand.append((cat, a, sc))
    # 2) 점수순 정렬 후 근접중복 제거(대표는 고점수 유지). 바이오 별도 티어 없음.
    cand.sort(key=lambda x: x[2], reverse=True)
    kept = []; kept_arts = []
    for cat, a, sc in cand:
        if any(_same_story(a.get("title", ""), k.get("title", "")) for k in kept_arts):
            continue
        kept.append((cat, a, sc)); kept_arts.append(a)
    # 3) 점수순 — 카테고리별 상한·전체 상한 적용
    cnt = {}; count = 0
    for cat, a, sc in kept:
        if cnt.get(cat, 0) >= cap[cat]:
            continue
        lab = label.get(cat)
        L.append(f"[{lab}] {_unesc(a.get('title','')).strip()} "
                 f"<{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
        cnt[cat] = cnt.get(cat, 0) + 1; count += 1
        if count >= 15:
            break
    if count == 0:
        L.append("• 특이사항 없음")
    return "\n".join(L), count


# ⑦ 딜·M&A v2 — 수집단계 Claude 추출(deal_intel.json)을 5개 뷰로 렌더. 무결성·안정성을 위해
# intel이 ok가 아니거나 fallback이면 기존 룰 기반(_sec_deals_rule)으로 자동 전환.
_FOCUS_IND = ("바이오", "제약", "헬스", "의료기기", "진단", "폐기물", "에너지", "원료의약", "cdmo", "cro")
_SIGNAL_LABEL = {"FI_SHORTAGE": "FI 모집난", "SECTOR_CONCENTRATION": "산업 쏠림",
                 "VALUATION_GAP": "밸류 괴리", "DEAL_DROP": "딜 드랍·펜딩",
                 "ACQ_FINANCING": "인수금융", "REGULATORY": "규제"}


def _intel_links(it):
    arts = it.get("_articles") or []
    if not arts:
        return ""
    first = arts[0]
    lab = medialabel(first.get("url"), first.get("source") or "출처")
    s = f"<{first.get('url')}|{lab}>"
    if len(arts) > 1:
        s += f" 외 {len(arts) - 1}건"
    return s


def _is_focus(ind):
    return any(f in (ind or "").lower() for f in _FOCUS_IND)


def sec_deals(articles, intel=None):
    intel = intel or {}
    ok = intel.get("ok") and not intel.get("fallback")
    deals = intel.get("deals") or []
    pe = intel.get("pe_moves") or []
    fs = intel.get("for_sale") or []
    sig = intel.get("market_signals") or []
    # intel 사용 불가(키없음·실패·폴백) 또는 전부 비었으면 → 룰 기반 폴백
    if not ok or not (deals or pe or fs or sig):
        return _sec_deals_rule(articles)

    L = ["🤝 *[딜·M&A 모니터링]*"]
    count = 0
    gen = [d for d in deals if not _is_focus(d.get("industry"))]
    foc = [d for d in deals if _is_focus(d.get("industry"))]

    def deal_line(d):
        bits = [d.get("target", "").strip()]
        tail = []
        if d.get("stage"):
            tail.append(d["stage"])
        if d.get("counterparties"):
            tail.append(d["counterparties"])
        if d.get("amount"):
            tail.append(d["amount"])
        head = bits[0] + (f" — {', '.join(tail)}" if tail else "")
        pe_s = f" _[{'·'.join(d.get('pe_firms'))}]_" if d.get("pe_firms") else ""
        return f"• {head}{pe_s} {_intel_links(d)}".rstrip()

    if gen:
        L.append("*📌 거론 딜*")
        for d in gen[:6]:
            L.append(deal_line(d)); count += 1
    if foc:
        L.append("*🧬 그룹 관심산업 딜* _(인벌브 검토)_")
        for d in foc[:5]:
            L.append(deal_line(d)); count += 1
    if pe:
        L.append("*🏢 PE 하우스 동향*")
        for m in pe[:4]:
            tgt = f" — {m.get('target')}" if m.get("target") else ""
            L.append(f"• {m.get('pe','')} {m.get('action','')}{tgt} {_intel_links(m)}".rstrip())
            count += 1
    if fs:
        L.append("*🏷️ 매물 리스트*")
        for s in fs[:5]:
            sl = f", {s.get('seller')}" if s.get("seller") else ""
            L.append(f"• {s.get('asset','')} — {s.get('process_stage','')}{sl} {_intel_links(s)}".rstrip())
            count += 1
    if sig:
        L.append("*📈 마켓 시그널*")
        for m in sig[:3]:
            lab = _SIGNAL_LABEL.get(m.get("type"), "시그널")
            L.append(f"• [{lab}] {m.get('summary','')} {_intel_links(m)}".rstrip())
            count += 1
    if count == 0:
        L.append("• 특이사항 없음")
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
    fund = load("fund_flow_summary.json")  # ④ 정책자금/국민성장 섹션(예정)에서 사용
    kgf = load("kgf_execution_summary.json")
    releases = load("policy_releases.json")  # ③ 정책 섹션: 정부 보도자료(검증 앵커)
    intel = load("deal_intel.json")  # ⑦ 딜·M&A: 수집단계 Claude 추출(실패시 룰 폴백)
    arts = rb.get("articles", []) or []

    # 신선도: 오늘 수집분인지
    gen = kst_date(rb.get("generated_at", ""))
    stale = "" if gen == TODAY else f" _(데이터 {gen} 기준)_"

    head = f"📰 *오늘의 CEO 뉴스 브리핑* — {TODAY} ({'월화수목금토일'[NOW.weekday()]}){stale}"
    deals, dcount = sec_deals(arts, intel)
    part1 = "\n\n".join([head, sec_rates(rate),
                         sec_policy(arts, releases), sec_kgf(kgf, fund)])
    part2 = "\n\n".join([f"📰 *CEO 뉴스 브리핑* — {TODAY} [2/2]",
                         sec_hlb(arts), deals,
                         "---\n_코드 자동발송 | Claude CEO 브리핑_"])
    post(part1)
    post(part2)
    print(f"발송 완료: 딜 {dcount}건, HLB/정책/KGF 포함")


if __name__ == "__main__":
    main()
