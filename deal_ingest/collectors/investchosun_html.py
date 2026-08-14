"""InvestChosun HTML collector. Article URLs: /site/data/html_dir/..."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from store.db import Article
from .core import BaseCollector, normalize_url, infer_category


# /site/data/html_dir/2026/05/12/2026051280123.html
ARTICLE_HREF_RE = re.compile(
    r"/site/data/html_dir/\d{4}/\d{2}/\d{2}/", re.IGNORECASE
)
URL_DATE_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/")


class InvestChosunHtmlCollector(BaseCollector):
    source = "investchosun"
    source_label = "인베스트조선"

    def collect(self) -> Iterable[Article]:
        urls = self.config.get("list_urls", [])
        if not urls:
            self.logger.warning("investchosun_html: no list_urls")
            return
        seen: set[str] = set()
        for list_url in urls:
            self.logger.info("investchosun_html: fetching %s", list_url)
            try:
                resp = self.fetch(list_url)
            except Exception as e:
                self.logger.error("investchosun_html: fetch failed %s: %s",
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
                pub = self._date_from_url(norm)
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
            self.logger.info("investchosun_html: %d links from %s",
                             count, list_url)

    @staticmethod
    def _date_from_url(url):
        m = URL_DATE_RE.search(url)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            try:
                return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
        return None
