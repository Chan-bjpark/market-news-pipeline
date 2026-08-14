"""Shared collector primitives. Korean keywords live in keywords.json."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests

from store.db import Article


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "mc_cid", "mc_eid",
    "ref", "referrer", "source",
}


def normalize_url(url: str) -> str:
    p = urlparse(url)
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    return urlunparse((p.scheme, p.netloc.lower(), p.path,
                       p.params, urlencode(pairs), ""))


_KW_FILE = Path(__file__).parent / "keywords.json"
_kw = json.loads(_KW_FILE.read_text(encoding="utf-8-sig"))
CATEGORY_KEYWORDS: dict = _kw["categories"]
CATEGORY_PRIORITY: list = _kw["priority"]

# --- HLB 고임팩트 신호 스크리닝 (2026-08-14 신설) ---
# 그룹 종목의 '주가·해외투자자 직결' 신호를 수집 시점에 최우선 태깅(category='hlb_signal')
# 하여 other/시황에 묻히지 않게 등급화. 상세 로직·근거는 keywords.json _hlb_signal_note 참조.
HLB_SCOPE: list = _kw.get("hlb_scope", [])
HLB_SIGNALS: list = _kw.get("hlb_signals", [])
HLB_ENTITY_STRONG: list = _kw.get("hlb_entity_strong", [])


def is_hlb_signal(title: str, summary: str = "") -> bool:
    """HLB그룹 고임팩트 신호 여부.

    (A) 제목 스코프: HLB 계열명이 '제목'에 있고, hlb_signals 중 하나가 본문(제목+요약)에 있음.
    (B) 강신호 스코프: HLB 계열명이 본문 어디든 있고, hlb_entity_strong(오인 소지 적은
        강신호: 블랙록·공매도·대량보유·리보세라닙 등)이 '제목'에 있음.
    index류(MSCI·편입·제외)는 (B)에서 제외 — 코스피 지수 시황이 HLB를 요약에만 스치는
    경우까지 태깅되는 것을 막기 위함. 그런 건은 (A) 제목 스코프로만 잡힌다.
    """
    if not HLB_SCOPE:
        return False
    t = title or ""
    text = f"{t} {summary or ''}"
    tl = t.lower()
    textl = text.lower()
    scoped_title = any(n.lower() in tl for n in HLB_SCOPE)
    if scoped_title and any(s.lower() in textl for s in HLB_SIGNALS):
        return True
    scoped_text = any(n.lower() in textl for n in HLB_SCOPE)
    if scoped_text and any(s.lower() in tl for s in HLB_ENTITY_STRONG):
        return True
    return False


def infer_category(title: str, summary: str = "") -> str:
    # HLB 고임팩트 신호를 일반 priority 루프보다 먼저 검사(최우선).
    if is_hlb_signal(title, summary):
        return "hlb_signal"
    text = f"{title} {summary or ''}".lower()
    for cat in CATEGORY_PRIORITY:
        if any(k.lower() in text for k in CATEGORY_KEYWORDS[cat]):
            return cat
    return "other"


class BaseCollector(ABC):
    source: str
    source_label: str

    def __init__(self, config: dict, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        })

    @abstractmethod
    def collect(self) -> Iterable[Article]:
        ...

    def fetch(self, url: str, timeout: int = 20) -> requests.Response:
        last = None
        for i in range(2):
            try:
                r = self.session.get(url, timeout=timeout)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last = e
                self.logger.warning("fetch retry %d %s: %s", i, url, e)
        if last:
            raise last
        raise RuntimeError("fetch failed")
