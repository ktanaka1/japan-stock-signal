"""データ保持・ローテーション役。

価値の低い記事（中立かつ銘柄なし）を保持期間経過後に削除し、DBの肥大化と
全期間スキャンの重さを抑える。signals（成果物）には一切触れない外付けの掃除役で、
収集・分析・配信ロジックには干渉しない。
実行: python -m maintenance.cleanup（週次・日曜深夜 JST 想定）
"""
from __future__ import annotations

import logging
import sys

from store.db import migrate
from store import analyses, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def run() -> None:
    migrate()
    try:
        retention_days = int(settings.get("retention_days"))
    except (TypeError, ValueError):
        retention_days = 90

    result = analyses.purge_disposable(retention_days)
    logger.info(
        "Cleanup done (retention=%dd): article_analyses=%d, articles=%d deleted",
        result["retention_days"],
        result["analyses_deleted"],
        result["articles_deleted"],
    )


if __name__ == "__main__":
    run()
