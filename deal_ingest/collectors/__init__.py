"""deal_ingest collectors package."""

from .core import BaseCollector, Article, normalize_url, infer_category
from .thebell_rss import TheBellRssCollector
from .thebell_html import TheBellHtmlCollector
from .hankyung_rss import HankyungRssCollector
from .dealsite_html import DealsiteHtmlCollector
from .investchosun_html import InvestChosunHtmlCollector
from .marketinsight_html import MarketInsightHtmlCollector
from .einfomax_html import EinfomaxHtmlCollector
from .naver_news_api import NaverNewsApiCollector

__all__ = [
    "BaseCollector", "Article", "normalize_url", "infer_category",
    "TheBellRssCollector", "TheBellHtmlCollector", "HankyungRssCollector",
    "DealsiteHtmlCollector", "InvestChosunHtmlCollector",
    "MarketInsightHtmlCollector", "EinfomaxHtmlCollector",
    "NaverNewsApiCollector",
]
