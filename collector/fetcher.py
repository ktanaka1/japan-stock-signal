import logging
import re
import time

import feedparser
import httpx

from store.models import Article

logger = logging.getLogger(__name__)

# やのしんWEB-API（TDnet）。RSS→JSON に切替（document_url/company_code/url_xbrl を得るため）。
# limit は settings.tdnet_fetch_limit（既定600）で可変。引け後(15時台)は開示が集中し、
# cronスキップで実行間隔が数時間空くと「直近300件」窓からも溢れる実害が出た
# （2026-06-26: 開示649件/日×TDnet取得1回失敗→15:30分の開示2件が窓外に流出）。
# 600 なら重い日の1日分をほぼ1回でカバーできる（limit=600/1000 の応答は実測確認済み）。
_TDNET_JSON_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit={limit}"
_TDNET_LIMIT_DEFAULT = 600
# 従来RSS（系統B扱い・本文取得しない）
_DEFAULT_RSS_FEEDS = [
    "https://news.yahoo.co.jp/rss/topics/business.xml",
    "https://www.nhk.or.jp/rss/news/cat6.xml",
]

_TAG_RE = re.compile(r"<[^>]+>")

# 系統A該当の TDnet 開示に付与する取得メタ（DBには保存しない transient 情報）を
# Article インスタンスに横付けする属性名。collector が body_fetcher に渡す。
TDNET_META_ATTR = "tdnet_meta"


def fetch_all() -> tuple[list[Article], bool]:
    """全ソースから記事を収集する。戻り値は (articles, tdnet_ok)。

    - TDnet: やのしんJSONから取得。系統A該当開示には取得メタ（document_url/url_xbrl/
      company_code）を Article.<TDNET_META_ATTR> に横付けする（collector が本文取得に使う）。
    - Yahoo!/NHK: 従来RSSのまま（系統B扱い・本文取得しない）。

    tdnet_ok は TDnet（唯一の適時開示ソース）の取得に成功したか。落ちると開示が
    ゼロになるため、collector 側で連続失敗を監視して障害アラートを出す判断材料にする。
    """
    from store import settings as cfg

    articles: list[Article] = []
    tdnet_ok = False

    # --- TDnet（JSON） ---
    try:
        tdnet = _fetch_tdnet_json()
        articles.extend(tdnet)
        if tdnet:
            logger.info("Fetched %d entries from TDnet JSON", len(tdnet))
            tdnet_ok = True
        else:
            # recent窓は過去分も含むため0件は正常時にあり得ない。HTTP 200の空応答を
            # 成功扱いすると連続失敗の監視をすり抜ける（2026-06-26に空応答→無検知→
            # 窓溢れで開示2件を取りこぼした実害）。失敗として障害ストリークに乗せる。
            logger.warning("TDnet JSON returned 0 entries; treating as fetch failure")
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

    # --- PR TIMES（RDF・上場辞書で名寄せ一致のみ取込。件数ログは _fetch_prtimes 内） ---
    try:
        articles.extend(_fetch_prtimes())
    except Exception:
        logger.warning("Failed to fetch PR TIMES", exc_info=True)

    return articles, tdnet_ok


def _fetch_tdnet_json() -> list[Article]:
    """やのしん recent.json から Article を生成する。

    各 item は {"Tdnet": {...}} 形式。title / document_url(PDF) / company_code / url_xbrl を得る。
    Article(title, body, url) は従来どおり生成（url は開示PDFページ）。
    系統A該当開示には取得メタを横付けする。
    """
    from store import settings as cfg

    try:
        limit = int(cfg.get("tdnet_fetch_limit"))
    except (TypeError, ValueError):
        limit = _TDNET_LIMIT_DEFAULT
    data = _http_get_json(_TDNET_JSON_URL.format(limit=limit))
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

        # 全TDnet開示に権威ある同定情報（証券コード＋社名）を付与する。
        # 銘柄の同定をLLM生成任せにせず、開示会社をここで確定する（社名ハルシネーション対策）。
        # ※company_code は5桁（証券コード4桁＋チェック桁）。4桁への正規化は利用側(monitor)で行う。
        article.security_code = (t.get("company_code") or "").strip() or None
        article.security_name = (t.get("company_name") or "").strip() or None

        if is_numeric_disclosure(title):
            # 系統A: 本文取得メタを横付け（DBには保存しない transient 情報）
            setattr(article, TDNET_META_ATTR, {
                "document_url": document_url,
                "url_xbrl": (t.get("url_xbrl") or "").strip() or None,
                "company_code": (t.get("company_code") or "").strip() or None,
                "company_name": (t.get("company_name") or "").strip() or None,
            })
        articles.append(article)
    return articles


def _fetch_prtimes() -> list[Article]:
    """PR TIMES の RDF を取得し、上場企業の発表分だけを Article 化する。

    - prtimes_enabled が "true" でなければ即 no-op（既存挙動に一切影響を出さない）。
    - 各 entry の dc_corp（発表企業名）を上場辞書で名寄せし、**完全一致したものだけ**取込む。
      非上場・突合不可は破棄（収集すらしない＝Gemini枠を浪費しない）。
    - 一致時は TDnet と同じ流儀で権威ある証券コード/社名を Article に付与する
      （LLMに推測させない）。body はタイトルのみ（本文取得しない）。
    """
    from store import settings as cfg
    from store import listed_companies as lc

    if cfg.get("prtimes_enabled") != "true":
        return []

    url = cfg.get("prtimes_rss_url") or "https://prtimes.jp/index.rdf"
    feed = feedparser.parse(url)
    # feedparser はネットワーク失敗でも例外を投げず空を返すため、無音だと
    # 「0一致」と「フィード死亡」が運用上区別できない。bozo で明示的に警告する。
    if getattr(feed, "bozo", False) and not feed.entries:
        logger.warning("PR TIMES feed unavailable or malformed: %s",
                       getattr(feed, "bozo_exception", None))

    articles: list[Article] = []
    for entry in feed.entries:
        corp = (entry.get("dc_corp") or "").strip()
        if not corp:
            continue
        matched = lc.lookup(corp)
        if not matched:
            continue  # 非上場 or 表記ゆれ → 破棄
        code, name = matched
        title = (entry.get("title") or "").strip()
        link = entry.get("link") or ""
        if not title or not link:
            continue
        article = Article(title=title, body=title, url=link)
        # 権威ある同定情報（辞書由来の4桁コード＋正式社名）を付与する。
        article.security_code = code
        article.security_name = name
        articles.append(article)
    # 有効時は0一致でも件数を残す（「無効」「0一致」「フィード死亡」を切り分ける手掛かり）
    logger.info("PR TIMES: %d entries fetched, %d matched listed companies",
                len(feed.entries), len(articles))
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
            # 5xx/429 は一過性とみなしリトライ。枯渇後は raise_for_status() で伝播。
            if (status == 429 or 500 <= status < 600) and i < len(backoffs):
                time.sleep(backoffs[i])
                continue
            # 4xx（404/401/400 等）の恒久エラー、および枯渇後の 5xx/429 は即時伝播。
            resp.raise_for_status()
        except httpx.RequestError as exc:
            # 通信エラー（ConnectTimeout/ReadTimeout/ConnectError 等）のみリトライ。
            # HTTPStatusError は RequestError に該当しないため、ここでは捕捉されず即時伝播する。
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
