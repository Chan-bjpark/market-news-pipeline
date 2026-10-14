# -*- coding: utf-8 -*-
"""딜·M&A 지능화 추출기 (⑦ 섹션용) — 수집단계(GitHub Actions)에서 하루 1회 실행.

딜 후보 기사를 Anthropic Claude에 보내 구조화 JSON(deals/pe_moves/for_sale/market_signals)으로
추출 → 코드가 근거문구를 원문과 대조 검증 → deal_intel.json 생성. 발송기(sec_deals)는 LLM 없이
이 JSON을 5개 뷰로 렌더만 한다.

무결성 원칙(브라더): 기사에 명시된 팩트만, 추정·기자코멘트·예상치 금지, 없으면 null.
모든 항목은 article_ids로 원문에 연결되고, evidence 문구가 그 원문에 실존해야 채택(아니면 드롭).
LLM 실패·스키마불일치·키 없음 → ok:false,fallback:true 로 기록(발송기가 룰 기반으로 폴백).

env: ANTHROPIC_API_KEY(필수), ANTHROPIC_MODEL(선택, 기본 claude-3-5-haiku-latest)

standalone:  python deal_intel.py --selftest     (검증·클러스터 로직만, 네트워크 불필요)
             python deal_intel.py --run          (실추출, 키 필요)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

try:
    from util_io import atomic_write_json
except Exception:
    atomic_write_json = None

KST = timezone(timedelta(hours=9))
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022"
MAX_ARTICLES = 50          # 토큰·비용 통제(딜 후보 상한). 월 $0.5 상한 보장용(Haiku 계열)
MAX_OUTPUT_TOKENS = 2000
SUMMARY_CHARS = 140

# 딜 후보 1차 필터(LLM에 넣을 기사만 추림 — 비용↓·정밀↑)
_CAND_CATS = {"M&A", "PE", "lp_commit", "fund_raise", "cap_market"}
_CAND_KW = ["인수", "매각", "합병", "경영권", "지분", "출자", "결성", "바이아웃", "우협", "우선협상",
            "본입찰", "예비입찰", "매물", "인수전", "기술수출", "라이선스", "ipo", "상장", "증자",
            "펀드", "블라인드", "주관사", "투자유치", "프리ipo", "m&a", "매각가", "인수가"]

# 주요 PE 하우스 룩업(오인식 방지 — 본문에 실제 등장할 때만 태깅). 피드백으로 가감.
PE_HOUSES = ["MBK파트너스", "MBK", "한앤컴퍼니", "한앤코", "IMM PE", "IMM인베스트먼트", "IMM",
             "스틱인베스트먼트", "스틱", "글랜우드PE", "글랜우드", "어피너티", "칼라일", "KKR",
             "블랙스톤", "베인캐피탈", "베인", "앵커에쿼티", "VIG파트너스", "VIG", "JKL파트너스",
             "JKL", "스카이레이크", "에이티넘", "프랙시스캐피탈", "프랙시스", "센트로이드",
             "케이스톤", "노틱인베스트먼트", "유니슨캐피탈", "UCK", "얼라인파트너스",
             "오케스트라PE", "스톤브릿지", "웰투시", "크레센도", "린드먼아시아", "프리미어파트너스",
             "한투PE", "미래에셋PE", "스카이워크", "SG프라이빗에쿼티", "SG PE"]

MARKET_SIGNAL_TYPES = ["FI_SHORTAGE", "SECTOR_CONCENTRATION", "VALUATION_GAP",
                       "DEAL_DROP", "ACQ_FINANCING", "REGULATORY"]

SYSTEM_PROMPT = (
    "당신은 한국 M&A/PE 시장 뉴스에서 '사실'만 추출하는 분석 보조자다. "
    "아래 규칙을 반드시 지킨다: "
    "(1) 제공된 기사의 제목·요약에 '명시된' 내용만 추출한다. "
    "(2) 추정·예상·기자 개인 코멘트·배경설명은 금지. 정보가 없으면 해당 필드는 null 또는 빈 배열. "
    "(3) 모든 항목에는 근거가 된 기사의 article_id 목록과, 그 기사 원문에 '그대로 존재하는' 짧은 "
    "인용문(evidence, 한 구절)을 포함한다. 지어내지 말 것. "
    "(4) 같은 딜을 여러 매체가 보도하면 하나의 항목으로 합치되, 서로 다른 거래(예: 회사 전체 매각 vs "
    "자회사 지분 매각)는 절대 합치지 않는다. "
    "(5) PE 하우스는 본문에 실제 등장하는 이름만 pe_firms에 넣는다. "
    "오직 JSON 하나만 출력한다(설명·마크다운 금지)."
)

OUTPUT_SPEC = """출력 JSON 스키마:
{
 "deals": [ {"target": "대상기업/자산", "counterparties": "인수후보 또는 매각측(없으면 null)",
             "pe_firms": ["관련 PE"], "industry": "산업(바이오/제약/헬스케어/의료기기/폐기물·에너지/기타)",
             "stage": "거론|매각추진|예비입찰|본입찰|우선협상|본계약|완료|무산 중 명시된 것",
             "amount": "금액(명시시, 예 '3000억'), 없으면 null",
             "one_liner": "딜 한 줄 요약(기사에 근거, 추정 금지)",
             "article_ids": ["a1","a2"], "evidence": "원문에 존재하는 인용 한 구절"} ],
 "pe_moves": [ {"pe": "PE명", "action": "투자|회수|펀드결성|출자|볼트온|포트폴리오매각 등 명시된 행동",
             "target": "대상(없으면 null)", "article_ids": ["a3"], "evidence": "인용"} ],
 "for_sale": [ {"asset": "매물 기업/자산", "seller": "매각주체(없으면 null)",
             "process_stage": "매각추진|주관사선정|티저|예비입찰|본입찰 중 명시된 것",
             "article_ids": ["a4"], "evidence": "인용"} ],
 "market_signals": [ {"type": "FI_SHORTAGE|SECTOR_CONCENTRATION|VALUATION_GAP|DEAL_DROP|ACQ_FINANCING|REGULATORY",
             "summary": "시그널 한 줄(기사 근거)", "article_ids": ["a5"], "evidence": "인용"} ]
}
해당 없는 섹션은 빈 배열. 관심산업(바이오·제약·헬스케어·의료기기·폐기물에너지)은 industry로 구분 가능하게 표기."""

# 강제 tool-use(구조화 출력) 스키마 — 프리필 대신 이 tool로 JSON 구조를 강제.
# claude-haiku-5-5가 프리필(assistant 선두 '{')을 거부(HTTP 400)하므로 tool_choice로 대체.
TOOL_NAME = "emit_deal_intel"
_STR = {"type": "string"}
_STR_NULL = {"type": ["string", "null"]}
_IDS = {"type": "array", "items": {"type": "string"}}
OUTPUT_TOOL = {
    "name": TOOL_NAME,
    "description": "기사에 명시된 M&A/PE 사실만 구조화해 반환한다. 추정 금지, 근거 인용 필수.",
    "input_schema": {
        "type": "object",
        "properties": {
            "deals": {"type": "array", "items": {"type": "object", "properties": {
                "target": _STR, "counterparties": _STR_NULL,
                "pe_firms": {"type": "array", "items": {"type": "string"}},
                "industry": _STR, "stage": _STR, "amount": _STR_NULL,
                "one_liner": _STR, "article_ids": _IDS, "evidence": _STR},
                "required": ["target", "article_ids", "evidence"]}},
            "pe_moves": {"type": "array", "items": {"type": "object", "properties": {
                "pe": _STR, "action": _STR, "target": _STR_NULL,
                "article_ids": _IDS, "evidence": _STR},
                "required": ["pe", "article_ids", "evidence"]}},
            "for_sale": {"type": "array", "items": {"type": "object", "properties": {
                "asset": _STR, "seller": _STR_NULL, "process_stage": _STR,
                "article_ids": _IDS, "evidence": _STR},
                "required": ["asset", "article_ids", "evidence"]}},
            "market_signals": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": MARKET_SIGNAL_TYPES},
                "summary": _STR, "article_ids": _IDS, "evidence": _STR},
                "required": ["type", "summary", "article_ids", "evidence"]}},
        },
        "required": ["deals", "pe_moves", "for_sale", "market_signals"],
    },
}


def _norm(s):
    return re.sub(r"\s+", "", (s or "")).lower()


def load_exclusions(path=None):
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "deal_exclusions.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("keywords") or [], [s.lower() for s in (d.get("sources") or [])],
                set(d.get("urls") or []))
    except Exception:
        return ([], [], set())


def deal_candidates(articles, exclusions=None):
    kws, srcs, urls = exclusions or ([], [], set())
    out = []
    for a in articles:
        title = a.get("title", "")
        t = title + " " + (a.get("summary") or "")
        tl = t.lower()
        if a.get("url") in urls:
            continue
        if any(k in t for k in kws):
            continue
        if srcs and (a.get("source_label", "").lower() in srcs):
            continue
        is_cand = a.get("category") in _CAND_CATS or any(k in tl for k in _CAND_KW)
        if not is_cand:
            continue
        out.append(a)
    return out[:MAX_ARTICLES]


def _build_user_msg(cands):
    # 스키마는 tool(OUTPUT_TOOL)이 강제하므로 OUTPUT_SPEC 본문은 보내지 않는다(입력토큰 절감).
    lines = [f"아래 기사들에서 명시된 사실만 추출해 {TOOL_NAME} 도구로 반환하라. "
             "각 기사의 제목·요약에 '그대로 존재하는' 내용만 쓰고, evidence는 원문 인용 한 구절로 한다.\n",
             "[기사 목록] aid | 제목 | 요약"]
    for i, a in enumerate(cands):
        aid = f"a{i}"
        title = (a.get("title") or "").replace("\n", " ")
        summ = (a.get("summary") or "").replace("\n", " ")[:SUMMARY_CHARS]
        lines.append(f"{aid} | {title} | {summ}")
    return "\n".join(lines)


def _extract_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    i = text.find("{")
    j = text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("no JSON object in LLM output")
    return json.loads(text[i:j + 1])


def _resolve_model(key, timeout=15):
    """계정 가용 모델을 /v1/models로 조회해 Haiku(최저가)를 자동 선택 — ID 추측 제거·self-heal.
    env ANTHROPIC_MODEL이 가용목록에 있으면 그것을 우선. 조회 실패 시 기존 기본값(MODEL)."""
    env = os.environ.get("ANTHROPIC_MODEL")
    try:
        r = requests.get("https://api.anthropic.com/v1/models",
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                         timeout=timeout)
        if r.status_code == 200:
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            if env and env in ids:
                return env
            haikus = [i for i in ids if "haiku" in i.lower()]
            if haikus:
                return sorted(haikus, reverse=True)[0]   # 최신 Haiku 우선(모두 저가)
            if ids:
                return ids[0]
    except Exception:  # noqa: BLE001
        pass
    return env or MODEL


def _call_claude(system, user, model, timeout=60):
    """강제 tool-use로 구조화 JSON(dict)을 받는다 — 텍스트 파싱 없음.
    tool_choice로 OUTPUT_TOOL 호출을 강제하므로 설명문·마크다운이 섞일 여지가 없다."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    payload = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "system": system,
               "messages": [{"role": "user", "content": user}],
               "tools": [OUTPUT_TOOL],
               "tool_choice": {"type": "tool", "name": TOOL_NAME}}
    last = None
    for _ in range(2):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                for p in (data.get("content") or []):
                    if p.get("type") == "tool_use" and p.get("name") == TOOL_NAME:
                        inp = p.get("input")
                        if isinstance(inp, dict):
                            return inp
                stop = data.get("stop_reason")
                last = f"tool_use 블록 없음(stop_reason={stop})"
                if stop == "max_tokens":   # 토큰 상한 → 구조화 출력 잘림
                    raise RuntimeError("출력 토큰 상한 초과(max_tokens) — MAX_OUTPUT_TOKENS 상향 필요")
            else:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last = str(e)[:200]
        time.sleep(2)
    raise RuntimeError(f"Claude 호출 실패: {last}")


def _verify_and_attach(items, cands, key_fields):
    """각 항목의 article_ids가 유효하고 evidence 인용이 해당 원문에 실존하는지 검증.
    통과 항목만 반환하며 url/source(대표 1 + 나머지)를 붙인다."""
    idmap = {f"a{i}": a for i, a in enumerate(cands)}
    out = []
    for it in items or []:
        ids = [x for x in (it.get("article_ids") or []) if x in idmap]
        if not ids:
            continue
        arts = [idmap[x] for x in ids]
        corpus = _norm(" ".join((a.get("title", "") + " " + (a.get("summary") or "")) for a in arts))
        ev = _norm(it.get("evidence", ""))
        if not ev or ev not in corpus:      # 근거문구 원문 실존 검증(환각 차단)
            continue
        if not any(it.get(f) for f in key_fields):  # 핵심 필드 최소 1개
            continue
        it = dict(it)
        it["_articles"] = [{"url": a.get("url"), "source": a.get("source_label")} for a in arts]
        out.append(it)
    return out


def run(articles, out_path=None, logger=None):
    def log(m):
        (logger.info if logger else print)(m)
    ex = load_exclusions()
    cands = deal_candidates(articles, ex)
    key = os.environ.get("ANTHROPIC_API_KEY")
    model = _resolve_model(key) if key else MODEL
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "model": model,
               "candidates": len(cands), "ok": False, "fallback": True,
               "deals": [], "pe_moves": [], "for_sale": [], "market_signals": []}
    if not cands:
        payload.update(ok=True, fallback=False, reason="딜 후보 없음")
        _write(out_path, payload)
        return payload
    try:
        obj = _call_claude(SYSTEM_PROMPT, _build_user_msg(cands), model)  # tool-use → dict
        if not isinstance(obj, dict):
            raise RuntimeError(f"구조화 출력이 dict 아님: {type(obj).__name__}")
        deals = _verify_and_attach(obj.get("deals"), cands, ["target"])
        pe = _verify_and_attach(obj.get("pe_moves"), cands, ["pe"])
        fs = _verify_and_attach(obj.get("for_sale"), cands, ["asset"])
        ms = _verify_and_attach(obj.get("market_signals"), cands, ["type", "summary"])
        # market signal 타입 화이트리스트
        ms = [m for m in ms if m.get("type") in MARKET_SIGNAL_TYPES]
        payload.update(ok=True, fallback=False, deals=deals, pe_moves=pe,
                       for_sale=fs, market_signals=ms)
        log(f"deal_intel: 후보 {len(cands)} → 딜 {len(deals)}·PE {len(pe)}·매물 {len(fs)}·시그널 {len(ms)}")
    except Exception as e:  # noqa: BLE001
        payload.update(ok=False, fallback=True, reason=str(e)[:200])
        log(f"deal_intel FALLBACK: {e}")
    _write(out_path, payload)
    return payload


def _write(out_path, payload):
    if not out_path:
        return
    if atomic_write_json:
        atomic_write_json(out_path, payload)
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


# ── 검증·클러스터 로직 단위테스트(합성 — 네트워크 불필요) ──
def _selftest():
    cands = [
        {"title": "SK에너지·현대오일뱅크 합병 추진", "summary": "양사 경영권 통합 논의",
         "url": "http://x/1", "source_label": "더벨", "category": "M&A"},
        {"title": "코스피 3% 급락 마감", "summary": "외국인 매도", "url": "http://x/2",
         "source_label": "연합", "category": "cap_market"},
    ]
    ex = (["급락"], [], set())
    c = deal_candidates([*cands], ex)
    assert len(c) == 1 and "SK에너지" in c[0]["title"], c          # 급락 기사 제외됨
    # 근거문구 검증: 실존 인용 통과 / 허위 인용 드롭
    items_ok = [{"target": "SK에너지·현대오일뱅크", "article_ids": ["a0"],
                 "evidence": "합병 추진"}]
    items_bad = [{"target": "가짜기업", "article_ids": ["a0"], "evidence": "존재하지 않는 문구 XYZ"}]
    v_ok = _verify_and_attach(items_ok, c, ["target"])
    v_bad = _verify_and_attach(items_bad, c, ["target"])
    assert len(v_ok) == 1 and v_ok[0]["_articles"][0]["source"] == "더벨", v_ok
    assert len(v_bad) == 0, v_bad
    print("[selftest] deal_intel OK — 후보필터/근거문구검증/드롭 통과")
    print("  후보:", c[0]["title"], "| 검증통과 딜:", v_ok[0]["target"])
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--briefing", default="recent_briefing.json")
    ap.add_argument("--out", default="deal_intel.json")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
    elif args.run:
        arts = json.load(open(args.briefing, encoding="utf-8")).get("articles", [])
        p = run(arts, args.out)
        print(json.dumps({k: p[k] for k in ("ok", "fallback", "candidates",
              "reason") if k in p}, ensure_ascii=False))
        for k in ("deals", "pe_moves", "for_sale", "market_signals"):
            print(f"  {k}: {len(p.get(k, []))}")
    else:
        ap.print_help()
