"""Hankyung Market Insight HTML collector.

Hankyung article URLs vary; we match /article/{id} and require the listing page
to be a marketinsight section page.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from store.db import Article
from .core import BaseCollector, normalize_url, infer_category


ARTICLE_HREF_RE = re.compile(r"/article/\d{8,}", re.IGNORECASE)
DATE_RE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")


class MarketInsightHtmlCollector(BaseCollector):
    source = "marketinsight"
    source_label = "마켓인사이트"

    def collect(self) -> Iterable[Article]:
        urls = self.config.get("list_urls", [])
        if not urls:
            self.logger.warning("marketinsight_html: no list_urls")
            return
        seen: set[str] = set()
        for list_url in urls:
            self.logger.info("marketinsight_html: fetching %s", list_url)
            try:
                resp = self.fetch(list_url)
            except Exception as e:
                self.logger.error("marketinsight_html: fetch failed %s: %s",
                                  list_url, e)
                continue
            html = resp.text
            soup = BeautifulSoup(html, "lxml")
            count = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not ARTICLE_HREF_RE.search(href):
                    continue
                full = urljoin(list_url, href)
                norm = normalize_url(full)
                if norm in seen:
                    continue
                seen.add(norm)
                title = a.get_text(strip=True)
                if not title or len(title) < 6:
                    continue
                pub = self._guess_date_near(a)
                yield Article(
                    source=self.source,
                    source_label=self.source_label,
                    url=norm,
                    title=title,
                    summary=None,
                    published_at=pub or datetime.now(timezone.utc).isoformat(),
                    category=infer_category(title),
                    paywalled=False,
                    raw_meta={"list_url": list_url,
                              "date_found": bool(pub)},
                )
                count += 1
            self.logger.info("marketinsight_html: %d links from %s",
                             count, list_url)

    @staticmethod
    def _guess_date_near(a_tag):
        candidates = []
        if a_tag.parent:
            candidates.append(a_tag.parent.get_text(" ", strip=True))
        nxt = a_tag.find_next(string=True)
        if nxt:
            candidates.append(str(nxt))
        for text in candidates:
            m = DATE_RE.search(text)
            if m:
                y, mo, d = (int(x) for x in m.groups())
                try:
                    return datetime(y, mo, d,
                                    tzinfo=timezone.utc).isoformat()
                except ValueError:
                    pass
        return None
