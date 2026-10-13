# -*- coding: utf-8 -*-
"""정부 보도자료 수집기 (③ 정책 섹션용) — policy_releases.json 생성.

4부처 보도자료 게시판을 각각 긁어 {agency,title,date,url}의 통합 리스트를 만든다.
부처별 어댑터는 독립적으로 try/except 격리되어, 한 곳이 실패해도 나머지는 수집된다
(sources health 기록). 수집만 담당 — 지침 필터·뉴스매칭·출력은 slack_briefing.sec_policy.

어댑터 유형(2026-10-13 실게시판 구조 확인):
  · 금융위(FSC)   html      — a[href=/no010101/{번호}], 날짜 YYYY-MM-DD, 서버렌더
  · 기재부(MOEF)  html_jsid — a[href=javascript:fn_egov_select('{nttId}')], 날짜 YYYY.MM.DD., 서버렌더
                              (2026 재정경제부 개편, 도메인 mofe.go.kr)
  · 공정위(KFTC)  html      — a[href=./selectBbsNttView.do?...&nttSn=], 테이블, 날짜 YYYY-MM-DD
  · 거래소(KRX)   krx_api   — GenerateOTP → POST OPN99000001.jspx (JSON), 날짜 YYYY/MM/DD
                              (WebSquare라 HTML 스크래핑 불가 → 내부 JSON API)

standalone 검증:  python policy_releases.py --selftest   (파서 단위테스트, 네트워크 불필요)
                  python policy_releases.py --run        (실수집 — Actions/사내망 등 gov 접근 가능 환경)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from util_io import atomic_write_json  # 파이프라인 공용(fsync+검증 atomic write)
except Exception:  # standalone/셀프테스트 허용
    atomic_write_json = None

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")}
DATE_RE = re.compile(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})")
_JUNK = re.compile(r"\s*\.?\s*금일 등록된 게시글\s*$")

# ── 부처별 게시판 어댑터 ─────────────────────────────────────────────
AGENCIES = [
    {
        "key": "금융위", "verified": True, "mode": "html",
        "base": "https://www.fsc.go.kr",
        "url": "https://www.fsc.go.kr/no010101",
        "detail_re": re.compile(r"^/no010101/\d+"),
        "strip_query": True,
    },
    {
        "key": "기재부", "verified": True, "mode": "html_jsid",
        "base": "https://www.mofe.go.kr",
        "url": "https://www.mofe.go.kr/nw/nes/nesdta.do?bbsId=MOSFBBS_000000000028&menuNo=4010100",
        "js_id_re": re.compile(r"fn_egov_select\(['\"]([^'\"]+)['\"]\)"),
        "detail_tmpl": ("https://www.mofe.go.kr/nw/nes/detailNesDtaView.do"
                        "?searchBbsId1=MOSFBBS_000000000028&searchNttId1={id}&menuNo=4010100"),
    },
    {
        "key": "공정위", "verified": True, "mode": "html",
        "base": "https://www.ftc.go.kr",
        "url": "https://www.ftc.go.kr/www/selectBbsNttList.do?bordCd=3&key=12&searchCtgry=01,02",
        "detail_re": re.compile(r"selectBbsNttView\.do"),
        "strip_query": False,
    },
    {
        "key": "거래소", "verified": True, "mode": "krx_api",
        "url": "https://open.krx.co.kr/contents/OPN/05/05000000/OPN05000000.jsp",
        "otp_url": "https://open.krx.co.kr/contents/COM/GenerateOTP.jspx",
        "otp_bld": "OPN/05/05000000/opn05000000t1_01",
        "data_url": "https://open.krx.co.kr/contents/OPN/99/OPN99000001.jspx",
    },
]


def _clean_title(t):
    t = (t or "").strip()
    t = _JUNK.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _norm_date(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _row_date(a):
    """앵커에서 위로 올라가며, 날짜를 포함한 '가장 가까운 작은' 조상의 날짜를 반환."""
    node = a
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        txt = node.get_text(" ", strip=True)
        if len(txt) <= 500 and DATE_RE.search(txt):
            return _norm_date(txt)
    return None


def _parse_board(html, ag):
    """html / html_jsid 어댑터용 게시판 파서 → [{title,date,url}]."""
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ag["mode"] == "html_jsid":
            m = ag["js_id_re"].search(href)
            if not m:
                continue
            url = ag["detail_tmpl"].format(id=m.group(1))
        else:  # html
            if not ag["detail_re"].search(href):
                continue
            url = urljoin(ag["url"], href)
            if ag.get("strip_query"):
                url = url.split("?")[0]
        title = _clean_title(a.get_text(" ", strip=True))
        if not title or len(title) < 6:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "date": _row_date(a), "url": url})
    return out


def _fetch_krx(ag, timeout, max_items):
    """KRX open.krx: GenerateOTP → POST JSON. HTML 아님."""
    s = requests.Session()
    s.headers.update(UA)
    s.headers["Referer"] = ag["url"]
    s.get(ag["url"], timeout=timeout)  # 쿠키 워밍업
    otp = s.get(ag["otp_url"], params={"bld": ag["otp_bld"], "name": "list"},
                timeout=timeout).text.strip()
    r = s.post(ag["data_url"], params={"code": otp},
               headers={"X-Requested-With": "XMLHttpRequest"}, timeout=timeout)
    rows = r.json().get("output", []) or []
    out = []
    for it in rows:
        title = _clean_title(it.get("title", ""))
        if not title:
            continue
        out.append({"title": title,
                    "date": _norm_date(it.get("noti_dd") or it.get("creat_ddtm")),
                    "url": ag["url"]})  # KRX 상세는 JS → 게시판 링크로 고정
    return out[:max_items]


def _fetch_agency(ag, timeout=15, max_items=40):
    if ag["mode"] == "krx_api":
        return _fetch_krx(ag, timeout, max_items)
    r = requests.get(ag["url"], headers=UA, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return _parse_board(r.text, ag)[:max_items]


def run(out_path=None, logger=None, recent_days=14, timeout=15):
    """4부처 수집 → policy_releases.json. 부처별 격리. 반환=payload."""
    log = logger or logging.getLogger("policy_releases")
    sources = {}
    releases = []
    cutoff = (datetime.now(KST) - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    for ag in AGENCIES:
        key = ag["key"]
        if not ag.get("verified"):
            sources[key] = {"ok": False, "error": "adapter_unverified", "count": 0}
            log.info("policy_releases %s: skipped (어댑터 미검증)", key)
            continue
        try:
            items = _fetch_agency(ag, timeout=timeout)
            recent = [it for it in items if (it["date"] or "9999-99-99") >= cutoff]
            for it in recent:
                it2 = dict(it)
                it2["agency"] = key
                releases.append(it2)
            sources[key] = {"ok": True, "count": len(recent), "fetched": len(items)}
            log.info("policy_releases %s: fetched=%d recent=%d", key, len(items), len(recent))
        except Exception as e:  # noqa: BLE001
            sources[key] = {"ok": False, "error": str(e)[:200]}
            log.warning("policy_releases %s FAILED: %s", key, e)
    releases.sort(key=lambda r: r.get("date") or "", reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": any(s.get("ok") for s in sources.values()),
        "recent_days": recent_days,
        "sources": sources,
        "releases": releases,
    }
    if out_path:
        if atomic_write_json:
            atomic_write_json(out_path, payload)
        else:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


# ── 파서 단위테스트(합성 fixture — 실데이터 아님, 셀렉터 검증용) ──────────
_FX_FSC = """<ul>
<li><span>15393</span>
 <a href="/no010101/87913?srchCtgry=&curPage=">금융권의 소멸시효 연장 관행을 개선하기 위해 조건부 대손인정제도가 시행됩니다. - 개정안 시행(10.12.)</a>
 <span>담당부서 : 서민금융과</span><span>조회수 : 3838</span>
 <a href="/comm/getFile?srvcId=BBSTY1&upperNo=87913">첨부.pdf</a><span>2026-10-12</span></li>
<li><span>15392</span>
 <a href="/no010101/87910?srchCtgry=">이억원 금융위원장, SC그룹 CFO·CRO 면담<em>. 금일 등록된 게시글</em></a>
 <span>2026-10-12</span></li></ul>"""

_FX_MOEF = """<ul>
<li><a href="javascript:fn_egov_select('MOSF_000000000079553');">「외국환거래법 시행령」 개정안 입법예고</a>
 <span class="date">2026.10.07.</span><span>외환제도과</span></li></ul>"""

_FX_KFTC = """<table><tbody>
<tr><td>9788</td><td>보도</td>
 <td><a href="./selectBbsNttView.do?pageUnit=10&pageIndex=1&key=12&bordCd=3&nttSn=48102">대한항공-아시아나 기업결합 관련 시정조치 불이행 심의 절차 개시</a></td>
 <td>기업결합과</td><td>2026-09-30</td>
 <td><a href="./downloadBbsFileAll.do?atchmnflNo=48102">파일 다운로드</a></td></tr>
</tbody></table>"""

_FX_KRX_JSON = {"output": [
    {"title": "(보도설명자료) 저성과기업의 신속·엄정한 퇴출을 통해 자본시장의 신뢰를 높이는 정책 방향",
     "noti_dd": "2026/10/09", "noti_no": "261015802"}]}


def _selftest():
    fsc = _parse_board(_FX_FSC, AGENCIES[0])
    assert len(fsc) == 2, fsc
    assert fsc[0]["url"] == "https://www.fsc.go.kr/no010101/87913", fsc[0]["url"]
    assert fsc[0]["date"] == "2026-10-12", fsc[0]
    assert fsc[1]["title"] == "이억원 금융위원장, SC그룹 CFO·CRO 면담", repr(fsc[1]["title"])

    moef = _parse_board(_FX_MOEF, AGENCIES[1])
    assert len(moef) == 1, moef
    assert moef[0]["url"] == ("https://www.mofe.go.kr/nw/nes/detailNesDtaView.do"
                              "?searchBbsId1=MOSFBBS_000000000028"
                              "&searchNttId1=MOSF_000000000079553&menuNo=4010100"), moef[0]["url"]
    assert moef[0]["date"] == "2026-10-07", moef[0]
    assert "외국환거래법" in moef[0]["title"]

    kftc = _parse_board(_FX_KFTC, AGENCIES[2])
    assert len(kftc) == 1, kftc
    assert kftc[0]["url"].endswith("nttSn=48102"), kftc[0]["url"]
    assert kftc[0]["url"].startswith("https://www.ftc.go.kr/www/selectBbsNttView.do"), kftc[0]["url"]
    assert kftc[0]["date"] == "2026-09-30", kftc[0]
    assert "기업결합" in kftc[0]["title"]

    # KRX: JSON→row 매핑(네트워크 없이 매핑 로직만)
    krx_rows = [{"title": _clean_title(x["title"]), "date": _norm_date(x["noti_dd"]),
                 "url": AGENCIES[3]["url"]} for x in _FX_KRX_JSON["output"]]
    assert krx_rows[0]["date"] == "2026-10-09", krx_rows[0]
    assert "저성과기업" in krx_rows[0]["title"]

    print("[selftest] 4부처 파서 OK")
    for lab, items in (("FSC", fsc), ("MOEF", moef), ("KFTC", kftc), ("KRX", krx_rows)):
        for it in items:
            print(f"   {lab:5} {it['date']} | {it['title'][:34]} | {it['url'][:60]}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="policy_releases.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.selftest:
        _selftest()
    elif args.run:
        p = run(args.out)
        print(json.dumps({"ok": p["ok"], "sources": p["sources"],
                          "releases_n": len(p["releases"])}, ensure_ascii=False, indent=2))
    else:
        ap.print_help()
