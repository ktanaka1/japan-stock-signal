"""既存シグナルへのインパクトスコア・バックフィル（効果測定の前準備）

旧プロンプトで生成された既存シグナルは impact=NULL のため、効果測定（impact4〜5の
デイトレ成績がベースラインを上回るか）ができない。そこで該当記事を新プロンプトで
**再分析して impact だけを付与**する（sentiment/summary/stocks は元のまま）。

  - impact が未設定のシグナルのみ対象（冪等・再実行安全）
  - signals.impact と article_analyses.impact の両方を更新
  - Gemini を呼ぶ（課金発生）。analyze_batch がバッチ化＋batch_interval でレート制御
  - 既定はドライラン（再分析してスコアを表示するが書き込まない）。--apply で書き込み

実行:
  python -m scripts.backfill_impact --limit 15        # 少量で確認（書き込みなし）
  python -m scripts.backfill_impact --apply           # 全件書き込み
"""
from __future__ import annotations

import argparse
import logging
import sys

from store.db import migrate, execute
from store import settings as cfg
from store.models import Article
from monitor.analyzer import analyze_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("backfill_impact")


def _load_targets(limit):
    sql = """
        SELECT s.id AS sig_id, a.id, a.title, a.body, a.url, a.fetched_at, a.is_read,
               a.security_code, a.xbrl_metrics, a.full_body, a.correction_reason, a.body_status
        FROM signals s JOIN articles a ON a.id = s.article_id
        WHERE s.impact IS NULL
        ORDER BY s.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = execute(sql).rows
    arts = [Article(
        id=r["id"], title=r["title"], body=r["body"], url=r["url"],
        fetched_at=r["fetched_at"], is_read=bool(r["is_read"]),
        security_code=r["security_code"], xbrl_metrics=r["xbrl_metrics"],
        full_body=r["full_body"], correction_reason=r["correction_reason"],
        body_status=r["body_status"] or "title_only",
    ) for r in rows]
    return arts


def run(apply: bool = False, limit=None) -> None:
    migrate()
    settings = cfg.get_all()
    arts = _load_targets(limit)
    logger.info("impact未設定シグナル %d件を再分析（apply=%s）", len(arts), apply)
    if not arts:
        return

    updated = 0
    dist = {}
    batches = analyze_batch(
        arts,
        system_prompt=settings["gemini_system_prompt"],
        batch_interval=int(settings["batch_interval_seconds"]),
        max_failures=int(settings["max_consecutive_failures"]),
        model=settings["gemini_model"],
        body_max_chars=int(settings["body_max_chars"]),
    )
    for chunk in batches:
        for article, result in chunk:
            if result is None:
                continue
            imp = result.impact
            dist[imp] = dist.get(imp, 0) + 1
            logger.info("article %s: impact=%d (%s) %s", article.id, imp, result.sentiment, article.title[:36])
            if apply:
                execute("UPDATE signals SET impact = ? WHERE article_id = ?", (imp, article.id))
                execute("UPDATE article_analyses SET impact = ? WHERE article_id = ?", (imp, article.id))
                updated += 1

    logger.info("分布: %s", {k: dist[k] for k in sorted(dist)})
    logger.info("%s: %d件 更新", "APPLIED" if apply else "DRY-RUN(書き込みなし)", updated)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="既存シグナルに impact を再分析でバックフィル")
    ap.add_argument("--apply", action="store_true", help="DBへ書き込む（既定はドライラン）")
    ap.add_argument("--limit", type=int, default=None, help="処理件数の上限")
    args = ap.parse_args()
    run(apply=args.apply, limit=args.limit)
