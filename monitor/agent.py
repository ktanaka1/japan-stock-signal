"""
精査役エージェント
  - 未読記事をLLMでまとめて分析（ポジティブ/ネガティブ/中立 + 関連銘柄）
  - 銘柄が特定できたポジ/ネガ記事だけをシグナルとしてDBに蓄積する
  - 通知は行わない（通知役 monitor.digest が朝にまとめて配信）
  - 実行: python -m monitor.agent
"""
from __future__ import annotations

import logging
import sys

from store.db import migrate
from store import repository, signals
from monitor.analyzer import analyze_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def run() -> None:
    migrate()
    articles = repository.get_unread()
    if not articles:
        logger.info("No unread articles")
        return

    logger.info("Analyzing %d unread articles", len(articles))
    results = analyze_batch(articles)

    signal_count = 0
    skipped = 0
    failed = 0
    processed_ids = []
    for article, result in results:
        if result is None:
            failed += 1
            continue  # 分析失敗分は未読のまま残し、次回再挑戦する
        if result.sentiment in ("positive", "negative") and result.stocks:
            signals.add(article.id, result.sentiment, result.summary, result.stocks, article.url)
            signal_count += 1
            logger.info(
                "Signal: article %d, %s, stocks=%s",
                article.id,
                result.sentiment,
                [s["code"] for s in result.stocks],
            )
        else:
            skipped += 1
        processed_ids.append(article.id)

    if processed_ids:
        repository.mark_many_as_read(processed_ids)
    logger.info(
        "Done: %d signals, %d skipped (neutral/no-stock), %d failed",
        signal_count,
        skipped,
        failed,
    )


if __name__ == "__main__":
    run()
