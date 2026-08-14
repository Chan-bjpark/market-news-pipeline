"""TheBell HTML collector — best-effort scraping of free article lists.

Notes:
- TheBell has no public RSS (verified). We scrape the public free section.
- If TheBell renders content via JavaScript, this collector returns 0 entries
  and logs a diagnostic. Fallback would require a headless browser.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from store.db import Article
from .core import BaseCollector, normalize_url, infer_category


# Heuristic: news article URLs on thebell typically look like
#   /free/content/ArticleView.asp?key=...
#   /front/newsview.asp?key=...
ARTICLE_HREF_RE = re.compile(
    r"(ArticleView\.asp|newsview\.asp)", re.IGNORECASE
)

DATE_RE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")


class TheBellHtmlCollector(BaseCollector):
    source = "thebell"
    source_label = "더벨"

    def collect(self) -> Iterable[Article]:
        urls = self.config.get("list_urls", [])
        if not urls:
            self.logger.warning("thebell_html: no list_urls configured")
            return
        seen: set[str] = set()
        for list_url in urls:
            self.logger.info("thebell_html: fetching %s", list_url)
            try:
                resp = self.fetch(list_url)
            except Exception as e:
                self.logger.error("thebell_html: fetch failed %s: %s",
                                  list_url, e)
                continue
            html = resp.text
            if "ArticleView" not in html and "newsview" not in html:
                self.logger.warning(
                    "thebell_html: no article links in %s — page may be JS-rendered. "
                    "Snippet: %r", list_url, html[:300]
                )
                continue
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
                art = Article(
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
                yield art
            self.logger.info("thebell_html: %d article links from %s",
                             count, list_url)

    @staticmethod
    def _guess_date_near(a_tag) -> str | None:
        """Look for a YYYY-MM-DD style date in nearby text (parent / next siblings)."""
        candidates: list[str] = []
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
                    return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
                except ValueError:
                    pass
        return None
