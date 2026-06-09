import logging
import re

import feedparser

from store.models import Article

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    # Yahoo!ニュース ビジネス
    "https://news.yahoo.co.jp/rss/topics/business.xml",
    # TDnet 適時開示（やのしんWEB-API経由）
    "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.rss",
    # NHK 経済
    "https://www.nhk.or.jp/rss/news/cat6.xml",
]

_TAG_RE = re.compile(r"<[^>]+>")


def fetch_all() -> list[Article]:
    articles = []
    for url in RSS_FEEDS:
        try:
            fetched = _fetch_feed(url)
            logger.info("Fetched %d entries from %s", len(fetched), url)
            articles.extend(fetched)
        except Exception:
            logger.warning("Failed to fetch %s", url, exc_info=True)
    return articles


def _fetch_feed(url: str) -> list[Article]:
    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        body = _strip_tags(entry.get("summary") or entry.get("description") or "")
        link = entry.get("link") or ""
        if title and link:
            articles.append(Article(title=title, body=body, url=link))
    return articles


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()
