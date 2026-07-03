"""捕捉率フィードバック役エージェント。

市場の動意（値上がり率ランキング上位）と、自システムが拾えた銘柄を照合し、捕捉率と
取りこぼしの内訳・改善提案を coverage_runs に記録する。既存テーブルは読み取りのみ
（収集・分析・配信ロジックには一切触れない＝外付けの計測器）。

D当日に拾ったシグナルは翌営業日(D+1)朝に配信されるため、計測を2フェーズに分ける:
  - snapshot（D夕方・JST 16:30 想定）: ランキングはライブ取得で過去日を遡れないので
    D当日に取り、その時点の配信状況で暫定記録する（finalized=0）。
  - finalize（D+1朝・配信完了直後）: 前営業日以前の暫定行を、最新の notified_at を
    反映して再判定し確定する（finalized=1）。配信前に測って過小評価する問題の解消。

実行:
  python -m feedback.agent             # snapshot（既定）
  python -m feedback.agent --finalize  # finalize（配信完了後）
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from store.db import migrate
from store import coverage_runs
from feedback import ranking, matcher, proposals, rescue

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


def _window(base_jst: datetime) -> tuple[str, str]:
    """基準JST日付 D の判定窓 [D-1 15:00 JST, D 15:00 JST) を UTC 文字列で返す。

    D（ランキング日）の動意に対し『事前/同時に把握していたか』を見る窓。base_jst の
    日付部分だけを使う（時刻は無視）。DBの created_at / fetched_at は UTC 保存なので
    UTC に変換して比較する。
    """
    d0 = base_jst.replace(hour=0, minute=0, second=0, microsecond=0)  # D 00:00 JST
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
    delivered, filtered, analyzed, tech_delivered, rescued = matcher.load_window(
        since_utc, until_utc
    )
    detail = matcher.classify(items, delivered, filtered, analyzed, tech_delivered, rescued)
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
        rescued=counts[matcher.STATUS_RESCUED],
    )
    logger.info("Coverage %s snapshot recorded (provisional): %s", ranking_date, proposal)

    # 逆引き昇格（signaled層の交差救済）: impact閾値落ち×ランキング上位を翌朝配信へ昇格。
    # 計測（上のrecord）の後に行う＝snapshot自体は昇格前の素の状態を記録する。
    rescue.promote_rescues(detail, since_utc, until_utc, ranking_date)


def finalize() -> None:
    """前営業日以前の暫定行(finalized=0)を、配信完了後の最新状況で確定する。

    snapshot は D夕方の配信前に測るため、D当日に拾って D+1朝に配信されたシグナルを
    『未配信』と過小評価する。本処理を D+1朝の配信完了直後に走らせ、各暫定行の窓を
    その行の ranking_date 基準で再計算→最新の notified_at で再判定→確定する。
    """
    migrate()
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    rows = coverage_runs.get_unfinalized_before(today)
    if not rows:
        logger.info("Coverage finalize: no unfinalized rows before %s; nothing to do", today)
        return

    logger.info("Coverage finalize: %d business day(s) to finalize", len(rows))
    for row in rows:
        rd = row["ranking_date"]                       # 'YYYY-MM-DD'(JST)
        base = datetime.strptime(rd, "%Y-%m-%d").replace(tzinfo=JST)
        since_utc, until_utc = _window(base)
        delivered, filtered, analyzed, tech_delivered, rescued = matcher.load_window(
            since_utc, until_utc
        )
        new_detail = matcher.reclassify_detail(
            row["detail"], delivered, filtered, analyzed, tech_delivered, rescued
        )
        summary = matcher.count_universe(new_detail)
        counts = summary["counts"]
        proposal = proposals.build_proposal(counts, summary["capture_rate"])

        old_rate = row.get("capture_rate")
        old_s = f"{old_rate * 100:.0f}%" if old_rate is not None else "—"
        new_rate = summary["capture_rate"]
        new_s = f"{new_rate * 100:.0f}%" if new_rate is not None else "—"

        coverage_runs.update_finalize(
            row_id=row["id"],
            captured=counts[matcher.STATUS_DELIVERED],
            captured_tech=counts[matcher.STATUS_DELIVERED_TECH],
            signaled=counts[matcher.STATUS_SIGNALED],
            neutral=counts[matcher.STATUS_NEUTRAL],
            not_collected=counts[matcher.STATUS_NOT_COLLECTED],
            capture_rate=new_rate,
            detail=new_detail,
            proposal=proposal,
            rescued=counts[matcher.STATUS_RESCUED],
        )
        logger.info(
            "Coverage %s finalized: capture_rate %s -> %s (row id=%d)",
            rd, old_s, new_s, row["id"],
        )


if __name__ == "__main__":
    if "--finalize" in sys.argv[1:]:
        finalize()
    else:
        run()
