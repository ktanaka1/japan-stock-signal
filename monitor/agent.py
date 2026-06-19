"""
精査役エージェント
  - 未読記事をLLMでまとめて分析（ポジティブ/ネガティブ/中立 + 関連銘柄）
  - 銘柄が特定できたポジ/ネガ記事だけをシグナルとしてDBに蓄積する
  - 通知は行わない（通知役 monitor.digest が朝にまとめて配信）
  - 実行: python -m monitor.agent
"""
from __future__ import annotations

import logging
import re
import sys

from store.db import migrate
from store import repository, signals, analyses
from store import settings as cfg
from monitor.analyzer import analyze_batch
from monitor.mailer import send_analysis_report
from monitor import quotes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# 東証の証券コードは4文字（数字、または新コード体系の英数字）。
# 空コードや桁違い（例: 5桁の "89720"）はLLM由来の誤りなので登録時に除外する。
_CODE_RE = re.compile(r"^[0-9A-Za-z]{4}$")


def _clean_stocks(stocks: list) -> list:
    """銘柄リストから不正なコードの銘柄を除外し、コードを trim して返す。

    4文字の数字/英数コードのみ残す。空・桁違いの銘柄は捨てる。
    これにより銘柄ゼロのシグナル化や、株価が引けない不正コードの保存を防ぐ。
    """
    cleaned = []
    for s in stocks:
        code = str(s.get("code", "")).strip()
        if _CODE_RE.match(code):
            s["code"] = code
            cleaned.append(s)
        else:
            logger.info("Dropping stock with invalid code: name=%s code=%r", s.get("name"), code)
    return cleaned


def run() -> None:
    migrate()
    settings = cfg.get_all()

    # 1回の実行で処理する未読の上限。背景タスクとして許容できる件数に抑え、
    # 大量バックログは複数回の実行で漸減させる（仕様書§3）。
    articles = repository.get_unread(limit=int(settings["max_articles_per_run"]))
    if not articles:
        logger.info("No unread articles")
        return

    logger.info("Analyzing %d unread articles", len(articles))

    signal_sentiments = set(settings["signal_sentiments"].split(","))
    require_stocks = settings["signal_require_stocks"].lower() == "true"

    signal_count = 0
    skipped = 0
    failed = 0
    signal_article_ids: set = set()
    # 分析レポートは最後にまとめて送るため、全バッチの (article, result) を蓄積する。
    all_results: list = []

    # バッチが届くたびに保存・既読化する（逐次保存）。
    # 途中で中断・打ち切りされても、処理済みバッチは確定して未読から外れる。
    batches = analyze_batch(
        articles,
        system_prompt=settings["gemini_system_prompt"],
        batch_interval=int(settings["batch_interval_seconds"]),
        max_failures=int(settings["max_consecutive_failures"]),
        model=settings["gemini_model"],
        body_max_chars=int(settings["body_max_chars"]),
    )
    for chunk in batches:
        all_results.extend(chunk)
        processed_ids = []
        for article, result in chunk:
            if result is None:
                failed += 1
                continue  # 分析失敗分は未読のまま残し、次回再挑戦する
            # 不正コードの銘柄を登録前に除外する（空コード・桁違いを弾く）。
            result.stocks = _clean_stocks(result.stocks)
            is_signal = result.sentiment in signal_sentiments and (
                not require_stocks or bool(result.stocks)
            )
            if is_signal:
                # シグナル銘柄のみ株価（終値・前日比%・出来高）を付与する。
                # result.stocks をインプレースで更新するため、後続の analyses.save も価格付きになる。
                # 取得失敗は quotes 側で握りつぶされ、価格なしのまま進む。
                quotes.enrich(result.stocks)
                signals.add(
                    article.id, result.sentiment, result.summary, result.stocks, article.url,
                    impact=result.impact,
                )
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
                article.id,
                result.sentiment,
                result.summary,
                result.reason,
                result.stocks,
                is_signal,
                impact=result.impact,
            )
            processed_ids.append(article.id)
        # バッチ完了ごとに既読を確定する（次回はこの分を除いた未読から再開）。
        if processed_ids:
            repository.mark_many_as_read(processed_ids)

    logger.info(
        "Done: %d signals, %d skipped (neutral/no-stock), %d failed",
        signal_count,
        skipped,
        failed,
    )

    send_analysis_report(all_results, signal_article_ids)


if __name__ == "__main__":
    run()
