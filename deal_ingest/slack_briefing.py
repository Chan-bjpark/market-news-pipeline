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
import json, os, sys, urllib.request
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


def recent(articles, days=3):
    out = []
    for a in articles:
        d = kst_date(a.get("published_at", ""))
        if d and (TODAY - d).days <= days:
            out.append(a)
    return out


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
        vd = d.get("vs_prev_day"); vm = d.get("vs_prev_month")
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
        L.append(f"• {label} 기준금리: {val}% on {d.get('last_meeting','')} "
                 f"({d.get('direction','')}, 다음 {d.get('next_meeting','')})".replace("%%", "%"))
    L.append("")
    L.append("출처: <https://ecos.bok.or.kr/|한국은행 ECOS> / <https://fred.stlouisfed.org/|FRED>")
    return "\n".join(L)


def sec_global(articles):
    hits = [a for a in recent(articles, 3)
            if a.get("source") == "einfomax"
            and any(k in (a.get("title", "")) for k in ("뉴욕", "글로벌", "S&P", "나스닥", "미 국채", "국제유가", "연준", "FOMC"))]
    if not hits:
        return "🌐 *[글로벌 시장]*\n특별한 뉴스없음"
    L = ["🌐 *[글로벌 시장]*"]
    for a in hits[:4]:
        L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
    return "\n".join(L)


def sec_policy(articles, fund):
    L = ["🏛️ *[한국 정책 변화]*"]
    pol = [a for a in recent(articles, 3)
           if any(k in (a.get("title", "") + (a.get("summary") or ""))
                  for k in ("금융위", "금감원", "기재부", "정부", "규제", "법안", "시행", "세제"))]
    seen = set(); n = 0
    for a in pol:
        if a.get("url") in seen:
            continue
        seen.add(a.get("url"))
        L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
        n += 1
        if n >= 3:
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


def sec_kgf(kgf):
    agg = kgf.get("aggregate") or {}
    if not agg.get("approved_count"):
        return ("🇰🇷 *[국민성장펀드 집행 추적 (90일 누적)]*\n"
                "세부현황: <https://national-growth-fund.web.app/|대시보드>")
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
    return "\n".join(L)


def sec_hlb(articles):
    L = ["🔬 *[HLB그룹 관련]*"]
    sig = [a for a in recent(articles, 3) if a.get("category") == "hlb_signal"]
    names = ["HLB", "넥스트사이언스", "리보세라닙", "에이치엘비"]
    flow = [a for a in recent(articles, 3)
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
    for a in recent(articles, 3):
        txt = a.get("title", "") + (a.get("summary") or "")
        if any(c in txt for c in crisis) and any(s in txt for s in sector):
            if a.get("url") in seen:
                continue
            seen.add(a.get("url")); rows.append(a)
    if not rows:
        L.append("특별한 뉴스없음")
    else:
        for a in rows[:5]:
            L.append(f"• {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
    return "\n".join(L)


def sec_deals(articles):
    order = ["lp_commit", "fund_raise", "M&A", "PE", "cap_market"]
    cap = {"lp_commit": 3, "fund_raise": 3, "M&A": 6, "PE": 4, "cap_market": 4}
    label = {"M&A": "M&A", "PE": "PE동향", "fund_raise": "PE동향",
             "lp_commit": "기관출자", "cap_market": "자본시장", "other": "거버넌스"}
    L = ["🤝 *[딜·M&A]*"]
    seen = set(); count = 0
    rc = recent(articles, 3)
    for cat in order:
        c = 0
        for a in rc:
            if a.get("category") != cat:
                continue
            if a.get("url") in seen:
                continue
            seen.add(a.get("url"))
            L.append(f"[{label.get(cat)}] {a.get('title','').strip()} <{a.get('url')}|{medialabel(a.get('url'), a.get('source_label'))}>")
            c += 1; count += 1
            if c >= cap[cat]:
                break
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
                         sec_policy(arts, fund), sec_kgf(kgf)])
    part2 = "\n\n".join([f"📰 *CEO 뉴스 브리핑* — {TODAY} [2/2]",
                         sec_hlb(arts), sec_distress(arts), deals,
                         "---\n_코드 자동발송 | Claude CEO 브리핑_"])
    post(part1)
    post(part2)
    print(f"발송 완료: 딜 {dcount}건, HLB/한계기업/정책 포함")


if __name__ == "__main__":
    main()
