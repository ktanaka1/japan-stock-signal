"""
調査役エージェント
  - RSSフィード/TDnet JSONから記事を収集してストアに保存する
  - 系統A（数値系）開示は新規保存時に本文（XBRL/PDFテキスト）を取得して保存する
  - 実行: python -m collector.agent
"""
import logging
import sys
from datetime import datetime, timedelta, timezone

from store.db import migrate
from store import repository
from store import settings as cfg
from collector.fetcher import fetch_all, TDNET_META_ATTR
from collector import body_fetcher
from monitor import mailer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# TDnet取得の連続失敗を監視する状態キー（settingsテーブルに永続化。configではなく内部状態）。
_TDNET_FAIL_KEY = "tdnet_fail_streak"      # 連続失敗回数
_TDNET_ALERTED_KEY = "tdnet_alerted"       # 障害アラート送信済みフラグ（"1"=送信済み）


def run() -> None:
    migrate()
    run_at_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    articles, tdnet_ok = fetch_all()
    _check_tdnet_health(tdnet_ok, run_at_jst)

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


def _int_setting(key: str, default: int) -> int:
    """settings の値を int で読む。未設定・不正は default。"""
    try:
        return int(cfg.get(key))
    except (TypeError, ValueError):
        return default


def _check_tdnet_health(tdnet_ok: bool, run_at_jst: str) -> None:
    """TDnet取得の連続失敗を検知し、閾値到達で一度だけメール障害アラートを送る。

    収集源(TDnet)が落ちると開示がゼロになるが、従来は warning ログのみで気付けなかった。
    連続失敗回数を settings に永続化し、閾値(tdnet_fail_alert_threshold・既定3)到達時に1通、
    復旧時に1通だけ送る（毎回送らないよう alerted フラグで再送抑止）。
    無通知＝障害と区別する既存思想（digest）と同じ方針。
    """
    threshold = _int_setting("tdnet_fail_alert_threshold", 3)
    streak = _int_setting(_TDNET_FAIL_KEY, 0)
    alerted = cfg.get(_TDNET_ALERTED_KEY) == "1"

    if tdnet_ok:
        # 成功。失敗ストリークがあればリセットし、アラート済みなら復旧通知を1通送る。
        if streak > 0 or alerted:
            cfg.set_value(_TDNET_FAIL_KEY, "0")
            cfg.set_value(_TDNET_ALERTED_KEY, "0")
            if alerted:
                logger.info("TDnet fetch recovered after %d consecutive failures", streak)
                mailer.send_collector_recovery(fail_streak=streak, run_at_jst=run_at_jst)
        return

    # 失敗。ストリークを進め、閾値到達かつ未アラートなら1通だけ送る。
    streak += 1
    cfg.set_value(_TDNET_FAIL_KEY, str(streak))
    logger.warning("TDnet fetch failure streak=%d (alert threshold=%d)", streak, threshold)
    if streak >= threshold and not alerted:
        mailer.send_collector_alert(fail_streak=streak, run_at_jst=run_at_jst)
        cfg.set_value(_TDNET_ALERTED_KEY, "1")
        logger.error("TDnet fetch alert sent (streak=%d)", streak)


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
            company_name=meta.get("company_name"),
        )
        article.body_status = result.body_status
        article.full_body = result.full_body
        article.security_code = result.security_code
        article.security_name = result.security_name
        article.xbrl_metrics = result.xbrl_metrics
        article.correction_reason = result.correction_reason
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
