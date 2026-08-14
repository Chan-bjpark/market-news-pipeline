"""Naver Search API (news) collector with media whitelist."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urlparse

import requests

from store.db import Article
from .core import BaseCollector, normalize_url, infer_category


HOST_MAP = {
    "thebell.co.kr": "더벨", "dealsite.co.kr": "딜인사이트",
    "investchosun.com": "인베스트조선", "einfomax.co.kr": "연합인포맥스",
    "newspim.com": "뉴스핌", "yna.co.kr": "연합뉴스",
    "yonhapnews.co.kr": "연합뉴스", "chosun.com": "조선일보",
    "chosunbiz.com": "조선비즈", "biz.chosun.com": "조선비즈",
    "joongang.co.kr": "중앙일보", "donga.com": "동아일보",
    "hani.co.kr": "한겨레", "khan.co.kr": "경향신문",
    "mk.co.kr": "매일경제", "pulse.mk.co.kr": "Pulse(매경)",
    "hankyung.com": "한국경제", "sedaily.com": "서울경제",
    "edaily.co.kr": "이데일리", "mt.co.kr": "머니투데이",
    "fnnews.com": "파이낸셜뉴스", "news1.kr": "뉴스1",
    "newsis.com": "뉴시스", "biz.heraldcorp.com": "헤럴드경제",
    "fntimes.com": "한국금융신문", "bizwatch.co.kr": "비즈워치",
    "ajunews.com": "아주경제", "asiae.co.kr": "아시아경제",
    "etoday.co.kr": "이투데이", "moneys.co.kr": "머니S",
    "g-enews.com": "글로벌이코노믹",
}


class NaverNewsApiCollector(BaseCollector):
    source = "naver_api"
    source_label = "네이버검색"

    def collect(self) -> Iterable[Article]:
        cid = self.config.get("client_id")
        csec = self.config.get("client_secret")
        if not cid or not csec:
            self.logger.warning("naver_news_api: client_id/secret missing")
            return
        queries = self.config.get("queries", [])
        if not queries:
            self.logger.warning("naver_news_api: no queries")
            return
        endpoint = "https://openapi.naver.com/v1/search/news.json"
        headers = {
            "X-Naver-Client-Id": cid,
            "X-Naver-Client-Secret": csec,
        }
        seen: set[str] = set()
        for q in queries:
            self.logger.info("naver_news_api: query=%r", q)
            try:
                resp = requests.get(
                    endpoint, headers=headers,
                    params={"query": q, "display": 100, "sort": "date"},
                    timeout=15,
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                self.logger.error("naver_news_api: %r failed: %s", q, e)
                continue
            items = resp.json().get("items", [])
            kept = skipped = 0
            for it in items:
                art = self._parse(it, q)
                if art is None:
                    skipped += 1
                    continue
                if art.url in seen:
                    continue
                seen.add(art.url)
                kept += 1
                yield art
            self.logger.info("naver_news_api: %r -> kept=%d skipped=%d (whitelist)",
                             q, kept, skipped)

    def _parse(self, it, query):
        link = it.get("originallink") or it.get("link")
        title = _strip_html(it.get("title", "")).strip()
        if not link or not title:
            return None
        pub = _parse_date(it.get("pubDate"))
        if not pub:
            return None
        host = urlparse(link).netloc.lower()
        matched = next(((k, v) for k, v in HOST_MAP.items() if k in host), None)
        if not matched:
            return None
        label = matched[1]
        summary = _strip_html(it.get("description", "")).strip() or None
        if summary and len(summary) > 500:
            summary = summary[:500] + "…"
        return Article(
            source=self.source,
            source_label=label,
            url=normalize_url(link),
            title=title,
            summary=summary,
            published_at=pub,
            category=infer_category(title, summary or ""),
            paywalled=False,
            raw_meta={"query": query, "host": host},
        )


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "")


def _parse_date(s):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None
