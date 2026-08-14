"""Einfomax bond-market collector. Fetches body for '마감/시황' articles."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from store.db import Article
from .core import BaseCollector, normalize_url, infer_category


ARTICLE_HREF_RE = re.compile(
    r"/news/articleView\.html\?idxno=\d+", re.IGNORECASE
)
DATE_RE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")
DEEP_TITLE_RE = re.compile(
    r"채권.*(마감|시황|메모)|국고채.*마감|시장.*마감"
)


class EinfomaxHtmlCollector(BaseCollector):
    source = "einfomax"
    source_label = "연합인포맥스"

    def collect(self) -> Iterable[Article]:
        urls = self.config.get("list_urls", [])
        if not urls:
            self.logger.warning("einfomax_html: no list_urls")
            return
        seen: set[str] = set()
        for list_url in urls:
            self.logger.info("einfomax_html: fetching %s", list_url)
            try:
                resp = self.fetch(list_url)
            except Exception as e:
                self.logger.error("einfomax_html: fetch failed %s: %s",
                                  list_url, e)
                continue
            soup = BeautifulSoup(resp.text, "lxml")
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
                deep = bool(DEEP_TITLE_RE.search(title))
                summary = self._fetch_body(norm) if deep else None
                cat = "bond_market" if deep else infer_category(title)
                yield Article(
                    source=self.source,
                    source_label=self.source_label,
                    url=norm,
                    title=title,
                    summary=summary,
                    published_at=pub or datetime.now(timezone.utc).isoformat(),
                    category=cat,
                    paywalled=False,
                    raw_meta={"list_url": list_url,
                              "deep": deep,
                              "body_fetched": bool(summary)},
                )
                count += 1
            self.logger.info("einfomax_html: %d links from %s",
                             count, list_url)

    def _fetch_body(self, url):
        try:
            resp = self.fetch(url)
        except Exception as e:
            self.logger.warning("einfomax_html: body fetch failed %s: %s",
                                url, e)
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        div = (soup.find("div", id="article-view-content-div") or
               soup.find("div", class_="article-body") or
               soup.find("article"))
        if not div:
            return None
        text = div.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        return text[:2000]

    @staticmethod
    def _guess_date_near(a):
        cands = []
        if a.parent:
            cands.append(a.parent.get_text(" ", strip=True))
        nxt = a.find_next(string=True)
        if nxt:
            cands.append(str(nxt))
        for text in cands:
            m = DATE_RE.search(text)
            if m:
                y, mo, d = (int(x) for x in m.groups())
                try:
                    return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
                except ValueError:
                    pass
        return None
