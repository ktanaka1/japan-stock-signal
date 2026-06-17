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
from store import repository, signals, analyses
from store import settings as cfg
from monitor.analyzer import analyze_batch
from monitor.mailer import send_analysis_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def run() -> None:
    migrate()
    settings = cfg.get_all()

    articles = repository.get_unread()
    if not articles:
        logger.info("No unread articles")
        return

    logger.info("Analyzing %d unread articles", len(articles))
    results = analyze_batch(
        articles,
        system_prompt=settings["gemini_system_prompt"],
        batch_interval=int(settings["batch_interval_seconds"]),
        max_failures=int(settings["max_consecutive_failures"]),
        model=settings["gemini_model"],
        body_max_chars=int(settings["body_max_chars"]),
    )

    signal_sentiments = set(settings["signal_sentiments"].split(","))
    require_stocks = settings["signal_require_stocks"].lower() == "true"

    signal_count = 0
    skipped = 0
    failed = 0
    processed_ids = []
    signal_article_ids: set = set()
    for article, result in results:
        if result is None:
            failed += 1
            continue  # 分析失敗分は未読のまま残し、次回再挑戦する
        is_signal = result.sentiment in signal_sentiments and (not require_stocks or bool(result.stocks))
        if is_signal:
            signals.add(article.id, result.sentiment, result.summary, result.stocks, article.url)
            signal_article_ids.add(article.id)
            signal_count += 1
            logger.info(
                "Signal: article %d, %s, stocks=%s",
                article.id,
                result.sentiment,
                [s["code"] for s in result.stocks],
            )
        else:
            skipped += 1
        analyses.save(
            article.id, result.sentiment, result.summary, result.reason, result.stocks, is_signal
        )
        processed_ids.append(article.id)

    if processed_ids:
        repository.mark_many_as_read(processed_ids)
    logger.info(
        "Done: %d signals, %d skipped (neutral/no-stock), %d failed",
        signal_count,
        skipped,
        failed,
    )

    send_analysis_report(results, signal_article_ids)


if __name__ == "__main__":
    run()
