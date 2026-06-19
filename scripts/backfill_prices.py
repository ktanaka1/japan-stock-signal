"""既存シグナルへの株価バックフィル（一回限りの保守スクリプト）

各シグナルに「生成日（signals.created_at の JST 日付）時点の終値・前日比%・出来高」を
Yahoo Finance の履歴から取得して付与する。signals.stocks と、対応する
article_analyses.stocks の両方を更新する（管理画面の表示・絞り込みが aa.stocks を見るため）。

  - 既定はドライラン（更新内容を出力するだけ・DB書き込みなし）
  - --apply で実際に書き込む
  - --force で close 済みの銘柄も取り直す（既定は close 未設定の銘柄のみ）
  - 冪等: 既に close がある銘柄は既定でスキップするため再実行安全

本番D1に対して実行する場合は FORCE_LOCAL_DB を立てない（.env の CLOUDFLARE_* で D1 に繋がる）。
ローカル検証時は CLAUDE.md のルールどおり FORCE_LOCAL_DB=1 DB_PATH=... を指定する。

実行:
  # ドライラン（本番D1の対象を確認）
  python -m scripts.backfill_prices
  # 実書き込み
  python -m scripts.backfill_prices --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from store.db import migrate, execute
from monitor import quotes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("backfill_prices")

JST = timezone(timedelta(hours=9))

# Yahoo は非公式エンドポイントのため、連続アクセスで 429/403（IPブロック）を招かないよう
# 1リクエストごとに十分なディレイを挟む（既定1.5秒、--sleep で調整可能）。
_SLEEP_BETWEEN_CALLS = 1.5
# 進捗ログとチャンク境界（処理対象シグナル単位）。
_CHUNK_SIZE = 15


def _jst_date(created_at: str) -> str:
    """signals.created_at（UTC 'YYYY-MM-DD HH:MM:SS'）を JST 日付 'YYYY-MM-DD' にする。"""
    try:
        dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        # フォーマット不明時は先頭10文字をそのまま日付として使う
        return (created_at or "")[:10]


def run(apply: bool = False, force: bool = False, sleep: float = _SLEEP_BETWEEN_CALLS,
        limit: int | None = None) -> None:
    migrate()
    sql = "SELECT id, article_id, stocks, created_at FROM signals ORDER BY id ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = execute(sql).rows
    logger.info("Loaded %d signals (sleep=%.1fs/req, chunk=%d)", len(rows), sleep, _CHUNK_SIZE)

    updated = 0
    filled_stocks = 0
    skipped_no_stock = 0
    failed_lookups = 0

    for processed, row in enumerate(rows):
        # チャンク境界ごとに進捗を出す（小分け処理・中断後の再開の目安）。
        if processed and processed % _CHUNK_SIZE == 0:
            logger.info("... progress %d/%d signals (%d 銘柄に付与済み)", processed, len(rows), filled_stocks)
        try:
            stocks = json.loads(row["stocks"]) if row["stocks"] else []
        except (ValueError, TypeError):
            logger.warning("signal %s: stocks JSON 不正、スキップ", row["id"])
            continue
        if not stocks:
            skipped_no_stock += 1
            continue

        target_date = _jst_date(row["created_at"])
        changed = False
        for stock in stocks:
            if stock.get("close") is not None and not force:
                continue  # 既に株価あり（冪等）
            code = stock.get("code", "")
            q = quotes.get_quote_asof(code, target_date)
            time.sleep(sleep)
            if q is None:
                failed_lookups += 1
                logger.warning("signal %s: %s(%s) @%s 取得不可", row["id"], stock.get("name"), code, target_date)
                continue
            stock["close"] = q.close
            if q.change_pct is not None:
                stock["change_pct"] = q.change_pct
            if q.volume is not None:
                stock["volume"] = q.volume
            stock["asof"] = q.asof
            changed = True
            filled_stocks += 1
            logger.info(
                "signal %s: %s(%s) @%s -> %s",
                row["id"], stock.get("name"), code, target_date, quotes.format_price(stock),
            )

        if not changed:
            continue
        updated += 1
        new_json = json.dumps(stocks, ensure_ascii=False)
        if apply:
            execute("UPDATE signals SET stocks = ? WHERE id = ?", (new_json, row["id"]))
            execute(
                "UPDATE article_analyses SET stocks = ? WHERE article_id = ?",
                (new_json, row["article_id"]),
            )

    mode = "APPLIED" if apply else "DRY-RUN (書き込みなし)"
    logger.info(
        "%s: %d signals 更新対象 / %d 銘柄に株価付与 / %d 取得失敗 / %d 銘柄なし",
        mode, updated, filled_stocks, failed_lookups, skipped_no_stock,
    )
    if not apply and updated:
        logger.info("実書き込みは --apply を付けて再実行してください")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="既存シグナルに生成日の株価をバックフィルする")
    parser.add_argument("--apply", action="store_true", help="実際にDBへ書き込む（既定はドライラン）")
    parser.add_argument("--force", action="store_true", help="close 済みの銘柄も取り直す")
    parser.add_argument("--sleep", type=float, default=_SLEEP_BETWEEN_CALLS,
                        help="1リクエストごとの待機秒（既定 %(default)s）")
    parser.add_argument("--limit", type=int, default=None,
                        help="処理するシグナル件数の上限（小分け実行用）")
    args = parser.parse_args()
    run(apply=args.apply, force=args.force, sleep=args.sleep, limit=args.limit)
