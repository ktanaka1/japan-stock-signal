"""
調査役エージェント
  - RSSフィードから記事を収集してストアに保存する
  - 実行: python -m collector.agent
"""
import logging
import sys

from store.db import migrate
from store import repository
from collector.fetcher import fetch_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def run() -> None:
    migrate()
    articles = fetch_all()
    new_count = 0
    for article in articles:
        _, is_new = repository.save(article)
        if is_new:
            new_count += 1
    logger.info("Collected %d articles, %d new", len(articles), new_count)


if __name__ == "__main__":
    run()
