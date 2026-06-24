"""捕捉率フィードバック役エージェント。

市場の動意（値上がり率ランキング上位）と、自システムが拾えた銘柄を毎営業日引け後に
照合し、捕捉率と取りこぼしの内訳・改善提案を coverage_runs に記録する。
既存テーブルは読み取りのみ（収集・分析・配信ロジックには一切触れない＝外付けの計測器）。
実行: python -m feedback.agent（平日 引け後 JST 16:30 想定）
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from store.db import migrate
from store import coverage_runs
from feedback import ranking, matcher, proposals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# 対象は値上がり率ランキング上位30（中小型・非ETFを母集団とする）。
_RANKING_TYPE = "up"
_TOP_N = 30


def _window(now_jst: datetime) -> tuple[str, str]:
    """判定窓 [D-1 15:00 JST, D 15:00 JST) を UTC 文字列で返す。

    D（ランキング日）の動意に対し『事前/同時に把握していたか』を見る窓。
    DBの created_at / fetched_at は UTC 保存なので UTC に変換して比較する。
    """
    d0 = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)  # D 00:00 JST
    since = (d0 - timedelta(hours=9)).astimezone(timezone.utc)        # D-1 15:00 JST
    until = (d0 + timedelta(hours=15)).astimezone(timezone.utc)       # D 15:00 JST
    fmt = "%Y-%m-%d %H:%M:%S"
    return since.strftime(fmt), until.strftime(fmt)


def run() -> None:
    migrate()
    now = datetime.now(JST)
    ranking_date = now.strftime("%Y-%m-%d")

    items = ranking.fetch_ranking(_RANKING_TYPE, top_n=_TOP_N)
    if not items:
        # 取得・パース失敗（外部障害 or 構造変更）。記録せずスキップ（既存処理に影響なし）。
        logger.warning("Coverage %s: ranking fetch returned no items; skip", ranking_date)
        return

    since_utc, until_utc = _window(now)
    delivered, filtered, analyzed, tech_delivered = matcher.load_window(since_utc, until_utc)
    detail = matcher.classify(items, delivered, filtered, analyzed, tech_delivered)
    summary = matcher.count_universe(detail)
    counts = summary["counts"]
    proposal = proposals.build_proposal(counts, summary["capture_rate"])

    coverage_runs.record(
        ranking_date=ranking_date,
        ranking_type=_RANKING_TYPE,
        top_n=_TOP_N,
        universe=summary["universe"],
        captured=counts[matcher.STATUS_DELIVERED],
        captured_tech=counts[matcher.STATUS_DELIVERED_TECH],
        signaled=counts[matcher.STATUS_SIGNALED],
        neutral=counts[matcher.STATUS_NEUTRAL],
        not_collected=counts[matcher.STATUS_NOT_COLLECTED],
        capture_rate=summary["capture_rate"],
        detail=detail,
        proposal=proposal,
    )
    logger.info("Coverage %s recorded: %s", ranking_date, proposal)


if __name__ == "__main__":
    run()
