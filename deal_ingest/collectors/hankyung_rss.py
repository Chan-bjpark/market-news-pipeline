"""Hankyung (한국경제) RSS collector. Covers Market Leaders / Industry / Stock."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import feedparser

from store.db import Article
from .core import BaseCollector, normalize_url, infer_category


# rss_url -> display label
DEFAULT_LABEL_MAP = {
    "rss.hankyung.com/leaders.xml": "마켓리더스",
    "rss.hankyung.com/stock.xml": "한국경제(증권)",
    "rss.hankyung.com/industry.xml": "한국경제(산업)",
    "rss.hankyung.com/economy.xml": "한국경제(경제)",
}


class HankyungRssCollector(BaseCollector):
    source = "hankyung"
    source_label = "한국경제"

    def collect(self) -> Iterable[Article]:
        urls = self.config.get("rss_urls", [])
        if not urls:
            self.logger.warning("hankyung_rss: no rss_urls configured")
            return
        for url in urls:
            self.logger.info("hankyung_rss: fetching %s", url)
            try:
                resp = self.fetch(url)
            except Exception as e:
                self.logger.error("hankyung_rss: fetch failed %s: %s", url, e)
                continue
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                self.logger.warning("hankyung_rss: 0 entries from %s (bozo=%s)",
                                    url, parsed.bozo)
                continue
            self.logger.info("hankyung_rss: %d entries from %s",
                             len(parsed.entries), url)
            label = self._label_for(url)
            for entry in parsed.entries:
                art = self._parse(entry, label)
                if art:
                    yield art

    def _label_for(self, url: str) -> str:
        for key, lab in DEFAULT_LABEL_MAP.items():
            if key in url:
                return lab
        return self.source_label

    def _parse(self, entry, source_label: str) -> Article | None:
        link = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            return None
        published = self._parse_date(entry)
        if not published:
            self.logger.warning("hankyung_rss: skip no-date: %s", title)
            return None
        summary = entry.get("summary") or entry.get("description") or ""
        if summary:
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(summary) > 500:
                summary = summary[:500] + "…"
        return Article(
            source=self.source,
            source_label=source_label,
            url=normalize_url(link),
            title=title,
            summary=summary or None,
            published_at=published,
            category=infer_category(title, summary),
            paywalled=False,
            raw_meta={"feed_url": entry.get("source", {}).get("href", "")},
        )

    @staticmethod
    def _parse_date(entry) -> str | None:
        for key in ("published_parsed", "updated_parsed"):
            t = entry.get(key)
            if t:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
        for key in ("published", "updated"):
            s = entry.get(key)
            if s:
                try:
                    dt = parsedate_to_datetime(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.isoformat()
                except Exception:
                    pass
        return None
