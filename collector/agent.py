"""
調査役エージェント
  - RSSフィード/TDnet JSONから記事を収集してストアに保存する
  - 系統A（数値系）開示は新規保存時に本文（XBRL/PDFテキスト）を取得して保存する
  - 実行: python -m collector.agent
"""
import logging
import sys

from store.db import migrate
from store import repository
from collector.fetcher import fetch_all, TDNET_META_ATTR
from collector import body_fetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def run() -> None:
    migrate()
    articles = fetch_all()

    # 系統A（本文取得メタ付き）とそれ以外（系統B・非該当・他ソース）に振り分ける
    body_targets = [a for a in articles if getattr(a, TDNET_META_ATTR, None)]
    plain = [a for a in articles if not getattr(a, TDNET_META_ATTR, None)]

    # 系統B・非該当・他ソースは従来どおりバッチ保存（body_status は既定 'title_only'）
    new_plain = repository.save_many(plain)

    # 系統A: 新規URLのみ本文取得（既存URLは取得を走らせない＝1回だけ）
    new_body = _process_body_targets(body_targets)

    logger.info(
        "Collected %d articles (%d body-targets); new: %d plain, %d with body",
        len(articles), len(body_targets), new_plain, new_body,
    )


def _process_body_targets(targets: list) -> int:
    """系統A該当記事を1件ずつ本文取得して保存し、新規保存件数を返す。

    1件の取得失敗は body_status='error' で記録して continue（run全体を止めない）。
    """
    if not targets:
        return 0
    already = repository.existing_urls([a.url for a in targets])
    new_count = 0
    for article in targets:
        if article.url in already:
            continue  # 既存URL＝本文取得済み or 取得不要。重複DLを避ける。
        meta = getattr(article, TDNET_META_ATTR, None) or {}
        result = body_fetcher.fetch_body(
            document_url=meta.get("document_url"),
            url_xbrl=meta.get("url_xbrl"),
            company_code=meta.get("company_code"),
        )
        article.body_status = result.body_status
        article.full_body = result.full_body
        article.security_code = result.security_code
        try:
            _, is_new = repository.save_with_body(article)
            if is_new:
                new_count += 1
        except Exception:
            # DB保存の予期せぬ失敗は warning（想定外）。1件で run を落とさない。
            logger.warning("Failed to save body article %s", article.url, exc_info=True)
    return new_count


if __name__ == "__main__":
    run()
