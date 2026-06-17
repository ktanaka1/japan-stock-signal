import logging
import re
import time

import feedparser
import httpx

from store.models import Article

logger = logging.getLogger(__name__)

# やのしんWEB-API（TDnet）。RSS→JSON に切替（document_url/company_code/url_xbrl を得るため）。
_TDNET_JSON_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit=100"
# 従来RSS（系統B扱い・本文取得しない）
_DEFAULT_RSS_FEEDS = [
    "https://news.yahoo.co.jp/rss/topics/business.xml",
    "https://www.nhk.or.jp/rss/news/cat6.xml",
]

_TAG_RE = re.compile(r"<[^>]+>")

# 系統A該当の TDnet 開示に付与する取得メタ（DBには保存しない transient 情報）を
# Article インスタンスに横付けする属性名。collector が body_fetcher に渡す。
TDNET_META_ATTR = "tdnet_meta"


def fetch_all() -> list[Article]:
    """全ソースから記事を収集する。

    - TDnet: やのしんJSONから取得。系統A該当開示には取得メタ（document_url/url_xbrl/
      company_code）を Article.<TDNET_META_ATTR> に横付けする（collector が本文取得に使う）。
    - Yahoo!/NHK: 従来RSSのまま（系統B扱い・本文取得しない）。
    """
    from store import settings as cfg

    articles: list[Article] = []

    # --- TDnet（JSON） ---
    try:
        tdnet = _fetch_tdnet_json()
        logger.info("Fetched %d entries from TDnet JSON", len(tdnet))
        articles.extend(tdnet)
    except Exception:
        logger.warning("Failed to fetch TDnet JSON", exc_info=True)

    # --- Yahoo!/NHK（RSS） ---
    feeds_str = cfg.get("rss_feeds")
    feeds = [f.strip() for f in feeds_str.strip().splitlines() if f.strip()]
    # rss_feeds に旧TDnet RSS が残っていても二重取得しないよう除外
    feeds = [f for f in feeds if "yanoshin" not in f]
    if not feeds:
        feeds = _DEFAULT_RSS_FEEDS

    for url in feeds:
        try:
            fetched = _fetch_feed(url)
            logger.info("Fetched %d entries from %s", len(fetched), url)
            articles.extend(fetched)
        except Exception:
            logger.warning("Failed to fetch %s", url, exc_info=True)
    return articles


def _fetch_tdnet_json() -> list[Article]:
    """やのしん recent.json から Article を生成する。

    各 item は {"Tdnet": {...}} 形式。title / document_url(PDF) / company_code / url_xbrl を得る。
    Article(title, body, url) は従来どおり生成（url は開示PDFページ）。
    系統A該当開示には取得メタを横付けする。
    """
    data = _http_get_json(_TDNET_JSON_URL)
    items = data.get("items") or [] if isinstance(data, dict) else []

    articles: list[Article] = []
    for item in items:
        t = item.get("Tdnet") if isinstance(item, dict) else None
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        document_url = (t.get("document_url") or "").strip()
        if not title or not document_url:
            continue

        # RSSサマリー相当が無いため body は空（従来も実測平均36字の薄いサマリー）
        article = Article(title=title, body="", url=document_url)

        if is_numeric_disclosure(title):
            # 系統A: 本文取得メタを横付け（DBには保存しない transient 情報）
            setattr(article, TDNET_META_ATTR, {
                "document_url": document_url,
                "url_xbrl": (t.get("url_xbrl") or "").strip() or None,
                "company_code": (t.get("company_code") or "").strip() or None,
            })
        articles.append(article)
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


def is_numeric_disclosure(title: str) -> bool:
    """タイトル部分一致で系統A（数値系・本文取得対象）かどうかを判定する。

    語彙は settings から取得（無ければ DEFAULTS）。A数値系の語を含めば系統A。
    取りこぼし回避を優先し広めに取る（過剰ヒットのコストは軽微）。
    """
    from store import settings as cfg

    triggers = _split_triggers(cfg.get("body_fetch_triggers_numeric"))
    return any(word in title for word in triggers)


def _split_triggers(value: str) -> list[str]:
    return [w.strip() for w in (value or "").splitlines() if w.strip()]


def _http_get_json(url: str) -> dict:
    """JSON GET。一過性失敗（ネットワーク例外・タイムアウト・5xx・429）のみ計3試行。"""
    backoffs = (1.0, 3.0)
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +collector)"}
    attempts = len(backoffs) + 1
    last_exc = None
    for i in range(attempts):
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            status = resp.status_code
            if status == 200:
                return resp.json()
            if status == 429 or 500 <= status < 600:
                if i < len(backoffs):
                    time.sleep(backoffs[i])
                    continue
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            last_exc = exc
            if i < len(backoffs):
                time.sleep(backoffs[i])
                continue
            raise
    if last_exc:
        raise last_exc
    return {}


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()
