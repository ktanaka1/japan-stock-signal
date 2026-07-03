"""逆引き昇格（rescue）: signaled層の交差救済。

「impact閾値未満でAIには凡庸に見えたが、市場が激しく反応した（値上がり率ランキング
上位に入った）」シグナルを、翌朝のdigest配信へ昇格させる。3段構えの第3レーン:

  1. 本体（朝7:30）      impact4〜5のニュース・カタリストを先制配信
  2. テクニカル版（朝8:00） ニュース無しの需給・モメンタムを捕捉
  3. 逆引き昇格（夕方）    impact3で落としたがランキング上位に来た"本物"を翌朝へ昇格

feedback パッケージは原則「読み取り専用の計測器」だが、本モジュールだけは例外として
signals.promoted_at / promote_reason を書く（notified_at 等の配信状態には触れない）。
昇格したシグナルの配信は monitor/digest.py の通常フローが行う。

捕捉率FBでは delivered_rescue として別枠計上し capture_rate に含めない
（ランキングを見て昇格させたものをランキング捕捉と数えると指標が自己成就するため）。
"""
from __future__ import annotations

import logging
from typing import List

from store.db import execute
from store import signals
from feedback.matcher import STATUS_SIGNALED, _codes_from_stocks

logger = logging.getLogger(__name__)


def promote_rescues(
    detail: List[dict], since_utc: str, until_utc: str, ranking_date: str
) -> int:
    """snapshot の detail から signaled_not_delivered 銘柄のシグナルを昇格する。

    昇格条件（1銘柄1シグナル・冪等）:
      - detail の in_universe かつ status=signaled_not_delivered の銘柄コードを含む
      - 窓内・未配信・未昇格のシグナル
      - rescue_min_impact(既定3) <= impact < min_impact_for_notify
        （閾値以上は翌朝どうせ配信されるので昇格不要。min_impact=0=全件配信なら自然に空）
      - 候補複数なら impact最大→id最大（最新）の1件のみ（LINEノイズ抑制）

    戻り値は昇格した件数。rescue_enabled が "true" でなければ即 no-op。
    """
    from store import settings as cfg

    if cfg.get("rescue_enabled") != "true":
        return 0
    try:
        rescue_min = int(cfg.get("rescue_min_impact"))
    except (TypeError, ValueError):
        rescue_min = 3
    try:
        notify_min = int(cfg.get("min_impact_for_notify"))
    except (TypeError, ValueError):
        notify_min = 0

    targets = {
        str(d.get("code", "")).strip(): d
        for d in detail
        if d.get("in_universe") and d.get("status") == STATUS_SIGNALED
    }
    if not targets:
        return 0

    # 窓内の未配信・未昇格シグナルを1クエリで取り、Python側で銘柄照合する
    # （stocks は JSON 文字列のため SQL では安全に引けない。窓内は高々数百行）。
    rows = execute(
        "SELECT id, stocks, impact FROM signals "
        "WHERE created_at >= ? AND created_at < ? "
        "AND notified_at IS NULL AND promoted_at IS NULL",
        (since_utc, until_utc),
    ).rows

    promoted = 0
    for code, d in targets.items():
        cands = [
            r for r in rows
            if code in _codes_from_stocks(r["stocks"])
            and rescue_min <= (r.get("impact") or 0) < notify_min
        ]
        if not cands:
            continue
        best = max(cands, key=lambda r: ((r.get("impact") or 0), r["id"]))
        pct = d.get("pct")
        pct_s = f" {pct:+.1f}%" if isinstance(pct, (int, float)) else ""
        reason = f"{ranking_date} 値上がり率#{d.get('rank')}{pct_s}"
        signals.promote(best["id"], reason)
        promoted += 1
        logger.info(
            "Rescue promoted: signal id=%s code=%s impact=%s (%s)",
            best["id"], code, best.get("impact"), reason,
        )
    if promoted:
        logger.info("Rescue: %d signal(s) promoted for next morning digest", promoted)
    return promoted
