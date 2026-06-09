"""
監視役エージェント
  - 未読記事をLLMで分析（ポジティブ/ネガティブ/中立）
  - ポジティブ/ネガティブのみLINEで一斉通知
  - 実行: python -m monitor.agent
"""
from __future__ import annotations

import logging
import os
import sys

from store.db import migrate
from store import repository
from store.recipients import get_all as get_recipients
from monitor.analyzer import analyze
from monitor.notifier import send

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

    recipients = get_recipients()
    logger.info("Processing %d unread articles for %d recipients", len(articles), len(recipients))

    notified = 0
    skipped = 0
    for article in articles:
        try:
            result = analyze(article.title, article.body)
            logger.info(
                "Article %d: sentiment=%s, stocks=%s",
                article.id,
                result.sentiment,
                [s["code"] for s in result.stocks],
            )
            if result.sentiment in ("positive", "negative"):
                send(result, article.url, recipients)
                notified += 1
            else:
                skipped += 1
            repository.mark_as_read(article.id)
        except Exception:
            logger.warning("Failed to process article %d", article.id, exc_info=True)

    logger.info("Done: %d notified, %d skipped (neutral)", notified, skipped)


if __name__ == "__main__":
    run()
