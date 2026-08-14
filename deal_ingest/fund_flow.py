"""Policy-fund lifecycle tracker.

Goal:
- Track each policy-money fund by accumulating its events along a timeline.
- Stages: planning -> size_confirmed -> lp_commit -> gp_open -> gp_review ->
          gp_selected -> closed
- Daily Slack brief reads this JSON to display each active fund's current stage
  plus the next expected event.

Input:  store/articles.db (90-day retention)
Output: fund_flow_summary.json (atomic write)
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure util_io is importable when invoked directly
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from util_io import atomic_write_json  # noqa: E402


POLICY_LP_KEYWORDS = [
    "모태펀드", "한국벤처투자", "KVIC", "성장금융", "한국성장금융",
    "농금원", "농식품모태", "농식품 모태",
    "산업은행", "산은", "기업은행", "기은",
    "정책금융", "정책자금", "정책 LP", "정책LP",
    "수은", "한국수출입은행",
    "신용보증기금", "신보", "기술보증기금", "기보",
    "고용보험기금", "사학연금", "공무원연금", "노란우산",
    "K-방산수출펀드", "K-바이오펀드", "K-방산", "K-반도체",
    "혁신성장펀드", "성장사다리", "스마트대한민국펀드",
    "글로벌펀드", "녹색금융", "탄소중립펀드", "재기지원펀드",
    "메가펀드", "국민성장펀드",
]


FUND_ALIASES = {
    "성장금융 2차 K-방산수출펀드": [
        r"2차\s*K-?방산수출펀드", r"K-?방산수출펀드\s*2차",
        r"성장금융.*방산수출", r"방산수출.*성장금융",
    ],
    "농식품모태펀드 2차": [
        r"농식품모태펀드?\s*2차", r"농모태\s*2차",
        r"농금원.*2차", r"농식품\s*모태\s*2차",
    ],
    "모태펀드 2026 대형 리그": [
        r"모태펀드.*대형\s*리그", r"대형\s*리그.*모태",
        r"한국벤처투자.*대형\s*리그",
    ],
    "산업은행 메가펀드": [
        r"산은\s*\d*,?\d*\s*억\s*메가펀드", r"산업은행\s*메가펀드",
        r"산은.*메가펀드", r"메가펀드.*산은", r"메가펀드.*산업은행",
    ],
    "국민성장펀드": [
        r"국민성장펀드", r"국민\s*성장\s*펀드",
    ],
    "성장사다리펀드": [r"성장사다리펀드"],
    "혁신성장펀드": [r"혁신성장펀드"],
    "K-바이오펀드": [r"K-?바이오펀드"],
    "스마트대한민국펀드": [r"스마트대한민국펀드"],
    "녹색금융펀드": [r"녹색금융펀드", r"탄소중립.*펀드"],
}


STAGE_KEYWORDS = [
    ("closed",         "결성 완료",      [r"결성\s*완료", r"1차\s*클로징", r"클로징\s*완료",
                                          r"펀드\s*결성\b", r"파이널\s*클로징"], 1),
    ("gp_selected",    "운용사 선정",    [r"운용사\s*선정", r"운용사\s*확정", r"GP\s*선정",
                                          r"GP\s*확정", r"최종\s*선정"], 2),
    ("gp_review",      "GP 심사",        [r"뷰티콘테스트", r"뷰티\s*콘테스트",
                                          r"심사", r"프리젠테이션", r"PT\b",
                                          r"서류심사", r"1차\s*심사"], 3),
    ("gp_open",        "GP 모집",        [r"GP\s*모집", r"운용사\s*모집",
                                          r"공고", r"위탁운용사", r"공모"], 4),
    ("lp_commit",      "LP 출자 확정",   [r"출자\s*확정", r"출자\s*결정",
                                          r"\d+\s*억\s*원?\s*출자", r"출자",
                                          r"약정"], 5),
    ("size_confirmed", "규모 확정",      [r"규모\s*확정", r"총\s*\d+\s*[조억]",
                                          r"목표\s*결성액", r"결성\s*목표"], 6),
    ("planning",       "조성 계획",      [r"조성\s*계획", r"조성\s*방침",
                                          r"발표", r"신규\s*조성", r"신설"], 7),
]


def _norm_text(s):
    return (s or "").replace("&amp;", "&").replace("&quot;", '"')


def _has_policy_signal(title, summary):
    text = f"{title} {summary}"
    return any(kw in text for kw in POLICY_LP_KEYWORDS)


def _match_fund(title, summary):
    text = f"{title} {summary}"
    for display_name, patterns in FUND_ALIASES.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return display_name
    return None


def _detect_stage(title, summary):
    text = f"{title} {summary}"
    for code, label, keywords, _prio in STAGE_KEYWORDS:
        for kw in keywords:
            if re.search(kw, text, re.IGNORECASE):
                return code, label
    return "uncategorized", "단계 미상"


def _extract_amount(title, summary):
    text = f"{title} {summary}"
    m = re.search(r"(\d[\d,]*)\s*조\s*(\d[\d,]*)?\s*억?", text)
    if m:
        return m.group(0).strip()
    m = re.search(r"(\d[\d,]*)\s*억\s*원?", text)
    if m:
        return m.group(0).strip()
    return None


def load_policy_articles(db_path, days_back=90):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    rows = conn.execute(
        "SELECT * FROM articles WHERE published_at >= ? "
        "AND (category IN ('lp_commit','fund_raise','PE','M&A','cap_market','other')) "
        "ORDER BY published_at ASC",
        (cutoff,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        title = _norm_text(r["title"])
        summary = _norm_text(r["summary"])
        if not _has_policy_signal(title, summary):
            continue
        out.append({
            "id": r["id"],
            "source_label": r["source_label"],
            "url": r["url"],
            "title": title,
            "summary": summary,
            "published_at": r["published_at"],
            "category": r["category"],
        })
    return out


def build_fund_timelines(articles):
    funds = defaultdict(list)
    unmatched = []
    for art in articles:
        fund_name = _match_fund(art["title"], art["summary"])
        if not fund_name:
            unmatched.append(art)
            continue
        stage_code, stage_label = _detect_stage(art["title"], art["summary"])
        amount = _extract_amount(art["title"], art["summary"])
        funds[fund_name].append({
            "date": art["published_at"][:10],
            "published_at": art["published_at"],
            "stage_code": stage_code,
            "stage_label": stage_label,
            "amount": amount,
            "title": art["title"],
            "summary": (art["summary"] or "")[:200],
            "url": art["url"],
            "source_label": art["source_label"],
        })
    for k in funds:
        funds[k].sort(key=lambda x: x["published_at"])
    return {"matched": dict(funds), "unmatched": unmatched}


def summarize_funds(funds):
    stage_order = {
        "planning": 1, "size_confirmed": 2, "lp_commit": 3,
        "gp_open": 4, "gp_review": 5, "gp_selected": 6, "closed": 7,
        "uncategorized": 0,
    }
    stage_next = {
        "planning": "규모 확정 / LP 추가 모집",
        "size_confirmed": "GP 모집 공고",
        "lp_commit": "GP 모집 공고",
        "gp_open": "뷰티콘테스트 / GP 심사",
        "gp_review": "운용사 선정",
        "gp_selected": "펀드 결성 완료",
        "closed": "(완료)",
        "uncategorized": "추가 정보 필요",
    }
    summary = []
    for fund_name, events in funds.items():
        if not events:
            continue
        latest_stage = max(events, key=lambda e: stage_order.get(e["stage_code"], 0))
        latest_event = events[-1]
        summary.append({
            "fund_name": fund_name,
            "current_stage_code": latest_stage["stage_code"],
            "current_stage_label": latest_stage["stage_label"],
            "next_expected_event": stage_next.get(latest_stage["stage_code"], ""),
            "first_seen": events[0]["date"],
            "last_updated": latest_event["date"],
            "event_count": len(events),
            "timeline": events,
        })
    summary.sort(key=lambda x: x["last_updated"], reverse=True)
    return summary


def run(db_path, out_path, days_back=90):
    articles = load_policy_articles(db_path, days_back)
    grouped = build_fund_timelines(articles)
    fund_summary = summarize_funds(grouped["matched"])
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": days_back,
        "total_policy_articles": len(articles),
        "matched_funds_count": len(fund_summary),
        "unmatched_count": len(grouped["unmatched"]),
        "funds": fund_summary,
        "unmatched_titles": [
            {"date": a["published_at"][:10], "title": a["title"],
             "url": a["url"], "source_label": a["source_label"]}
            for a in grouped["unmatched"][:50]
        ],
    }
    # Use shared util_io.atomic_write_json (fsync + post-write validation)
    atomic_write_json(out_path, payload)
    return payload


if __name__ == "__main__":
    import sys
    here = Path(__file__).parent
    db = here / "store" / "articles.db"
    out = here / "fund_flow_summary.json"
    if len(sys.argv) > 1:
        db = Path(sys.argv[1])
    result = run(db, out)
    print(f"matched_funds: {result['matched_funds_count']}, unmatched: {result['unmatched_count']}, total: {result['total_policy_articles']}")
    for f in result["funds"]:
        print(f"  [{f['current_stage_label']:10}] {f['fund_name']} -- events={f['event_count']} (last_updated {f['last_updated']})")
