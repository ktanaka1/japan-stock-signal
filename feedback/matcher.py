"""ランキング上位銘柄を、自システムの捕捉状況に分類する（既存テーブルは読み取りのみ）。

status:
  delivered              -- 配信済み（signals.notified_at あり）＝捕捉成功
  signaled_not_delivered -- シグナル化したが未配信（impact閾値/大型株フィルタ等で除外）
  analyzed_neutral       -- 分析はしたがシグナル化せず（中立/銘柄なし）
  not_collected          -- 自システムのどこにも無い（収集網の外）
"""
from __future__ import annotations

import json
from typing import List, Set, Tuple

from store.db import execute
from feedback.ranking import RankItem

STATUS_DELIVERED = "delivered"
STATUS_SIGNALED = "signaled_not_delivered"
STATUS_NEUTRAL = "analyzed_neutral"
STATUS_NOT_COLLECTED = "not_collected"


def _codes_from_stocks(raw: str) -> list:
    try:
        stocks = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    return [str(s.get("code", "")).strip() for s in stocks if s.get("code")]


def load_window(since_utc: str, until_utc: str) -> Tuple[Set[str], Set[str], Set[str]]:
    """判定窓内の (配信済みコード, 未配信シグナルコード, 分析済みコード) を返す。

    既存テーブルは SELECT のみ。窓は signals.created_at / articles.fetched_at(UTC) で絞る。
    """
    delivered: Set[str] = set()
    filtered: Set[str] = set()
    analyzed: Set[str] = set()

    sig_rows = execute(
        "SELECT stocks, notified_at FROM signals WHERE created_at >= ? AND created_at < ?",
        (since_utc, until_utc),
    ).rows
    for r in sig_rows:
        target = delivered if r.get("notified_at") else filtered
        target.update(_codes_from_stocks(r["stocks"]))

    aa_rows = execute(
        """
        SELECT aa.stocks AS stocks
        FROM article_analyses aa
        JOIN articles a ON a.id = aa.article_id
        WHERE a.fetched_at >= ? AND a.fetched_at < ?
        """,
        (since_utc, until_utc),
    ).rows
    for r in aa_rows:
        analyzed.update(_codes_from_stocks(r["stocks"]))

    return delivered, filtered, analyzed


def classify(
    items: List[RankItem],
    delivered: Set[str],
    filtered: Set[str],
    analyzed: Set[str],
) -> List[dict]:
    """ランキング各銘柄に status を付けた detail リストを返す（優先度: 配信>未配信>分析>未収集）。"""
    detail = []
    for it in items:
        if it.code in delivered:
            status = STATUS_DELIVERED
        elif it.code in filtered:
            status = STATUS_SIGNALED
        elif it.code in analyzed:
            status = STATUS_NEUTRAL
        else:
            status = STATUS_NOT_COLLECTED
        detail.append({
            "rank": it.rank,
            "code": it.code,
            "name": it.name,
            "market": it.market,
            "pct": it.change_pct,
            "in_universe": it.in_universe,
            "status": status,
        })
    return detail


def count_universe(detail: List[dict]) -> dict:
    """母集団(in_universe=True)の status 別件数と捕捉率を集計する。"""
    uni = [d for d in detail if d["in_universe"]]
    counts = {STATUS_DELIVERED: 0, STATUS_SIGNALED: 0, STATUS_NEUTRAL: 0, STATUS_NOT_COLLECTED: 0}
    for d in uni:
        counts[d["status"]] += 1
    total = len(uni)
    capture_rate = round(counts[STATUS_DELIVERED] / total, 4) if total else None
    return {"universe": total, "counts": counts, "capture_rate": capture_rate}
