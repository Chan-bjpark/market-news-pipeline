"""
더벨 RSS 수집기 (Phase 1).

config.json의 thebell_rss.rss_urls에 설정된 모든 RSS 피드를 순회하며 기사를 수집한다.

⚠️ 주의 — 더벨 RSS 실제 구조는 미검증 상태:
- 이 코드를 처음 실행할 때 'RSS validation' 단계에서 실제 응답을 자동 검증함
- 응답이 RSS 2.0이 아니거나 entry가 비어 있으면 logs/에 진단 정보 저장 + 운영자에게 알림
- 더벨이 RSS를 폐지·이전했을 경우 HTML 파싱 fallback이 필요 (Phase 1.5)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import feedparser

from store.db import Article
from .core import BaseCollector, normalize_url, infer_category


class TheBellRssCollector(BaseCollector):
    source = "thebell"
    source_label = "더벨"

    def collect(self) -> Iterable[Article]:
        rss_urls = self.config.get("rss_urls", [])
        if not rss_urls:
            self.logger.warning("thebell_rss: no RSS URLs configured")
            return

        for url in rss_urls:
            self.logger.info("thebell_rss: fetching %s", url)
            try:
                resp = self.fetch(url)
            except Exception as e:
                self.logger.error("thebell_rss: fetch failed for %s: %s", url, e)
                continue

            parsed = feedparser.parse(resp.content)

            # 진단 — 응답 유효성 체크
            if parsed.bozo and not parsed.entries:
                self.logger.error(
                    "thebell_rss: parse failure (bozo=1, no entries) for %s. "
                    "exception: %s. Response sample: %r",
                    url, parsed.bozo_exception, resp.text[:500]
                )
                continue

            if not parsed.entries:
                self.logger.warning(
                    "thebell_rss: 0 entries from %s. Check if RSS endpoint still active.",
                    url,
                )
                continue

            self.logger.info("thebell_rss: %d entries from %s", len(parsed.entries), url)

            for entry in parsed.entries:
                article = self._parse_entry(entry)
                if article:
                    yield article

    def _parse_entry(self, entry) -> Article | None:
        link = entry.get("link")
        title = entry.get("title", "").strip()
        if not link or not title:
            return None

        # 발행일자 파싱
        published_at = self._parse_date(entry)
        if not published_at:
            self.logger.warning("thebell_rss: skipping entry w/o date: %s", title)
            return None

        # 요약 — RSS description 또는 summary 사용
        summary = entry.get("summary", "") or entry.get("description", "")
        # HTML 태그 단순 제거
        if summary:
            import re
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(summary) > 500:
                summary = summary[:500] + "…"

        raw_meta = {
            "rss_categories": [t.term for t in entry.get("tags", [])] if entry.get("tags") else [],
            "author": entry.get("author"),
            "guid": entry.get("id"),
        }

        return Article(
            source=self.source,
            source_label=self.source_label,
            url=normalize_url(link),
            title=title,
            summary=summary or None,
            published_at=published_at,
            category=infer_category(title, summary),
            paywalled=False,  # 더벨 무료 기사는 RSS에 노출됨. 유료는 별도 처리 필요시 추가.
            raw_meta=raw_meta,
        )

    @staticmethod
    def _parse_date(entry) -> str | None:
        # feedparser가 RFC 822 / ISO 8601 모두 published_parsed로 제공
        for key in ("published_parsed", "updated_parsed"):
            parsed_time = entry.get(key)
            if parsed_time:
                # parsed_time은 UTC time.struct_time
                dt = datetime(*parsed_time[:6], tzinfo=timezone.utc)
                return dt.isoformat()

        # fallback: published 문자열 직접 파싱
        for key in ("published", "updated"):
            date_str = entry.get(key)
            if date_str:
                try:
                    dt = parsedate_to_datetime(date_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.isoformat()
                except (TypeError, ValueError):
                    pass

        return None
