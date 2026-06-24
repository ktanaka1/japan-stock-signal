"""ランキング上位銘柄を、自システムの捕捉状況に分類する（既存テーブルは読み取りのみ）。

status:
  delivered              -- ニュース版で配信済み（signals.notified_at あり）＝捕捉成功
  delivered_technical    -- テクニカル版(technical_runs)で配信済み＝捕捉成功（別チャンネル）
  signaled_not_delivered -- シグナル化したが未配信（impact閾値/大型株フィルタ等で除外）
  analyzed_neutral       -- 分析はしたがシグナル化せず（中立/銘柄なし）
  not_collected          -- 自システムのどこにも無い（収集網の外）

捕捉率はニュース版・テクニカル版の2チャンネル合算で測る（delivered + delivered_technical）。
テクニカル版は寄り前(JST 8:00頃)に実行され FB窓の内側に入るため、その run の picks を
「配信済み」として合算する（run_at が窓内の technical_runs を読む）。
"""
from __future__ import annotations

import json
from typing import List, Set, Tuple

from store.db import execute
from feedback.ranking import RankItem

STATUS_DELIVERED = "delivered"
STATUS_DELIVERED_TECH = "delivered_technical"
STATUS_SIGNALED = "signaled_not_delivered"
STATUS_NEUTRAL = "analyzed_neutral"
STATUS_NOT_COLLECTED = "not_collected"


def _codes_from_stocks(raw: str) -> list:
    try:
        stocks = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    return [str(s.get("code", "")).strip() for s in stocks if s.get("code")]


def _codes_from_picks(raw: str) -> list:
    """technical_runs.picks(JSON [{code,...}]) からコードを取り出す。"""
    try:
        picks = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    return [str(p.get("code", "")).strip() for p in picks if p.get("code")]


def load_window(since_utc: str, until_utc: str) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    """判定窓内の (配信済み, 未配信シグナル, 分析済み, テクニカル配信済み) コードを返す。

    既存テーブルは SELECT のみ。窓は signals.created_at / articles.fetched_at /
    technical_runs.run_at(UTC) で絞る。テクニカル版は寄り前に実行され窓の内側に入るため、
    その run の picks を「配信済み(別チャンネル)」として拾い、捕捉率の合算に使う。
    """
    delivered: Set[str] = set()
    filtered: Set[str] = set()
    analyzed: Set[str] = set()
    tech_delivered: Set[str] = set()

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

    # テクニカル版の配信記録（run_at が窓内・実際に配信できた run のみ）。
    tech_rows = execute(
        "SELECT picks FROM technical_runs "
        "WHERE run_at >= ? AND run_at < ? AND line_ok > 0",
        (since_utc, until_utc),
    ).rows
    for r in tech_rows:
        tech_delivered.update(_codes_from_picks(r["picks"]))

    return delivered, filtered, analyzed, tech_delivered


def classify(
    items: List[RankItem],
    delivered: Set[str],
    filtered: Set[str],
    analyzed: Set[str],
    tech_delivered: Set[str] = frozenset(),
) -> List[dict]:
    """ランキング各銘柄に status を付けた detail を返す。

    優先度: ニュース配信 > テクニカル配信 > 未配信 > 中立 > 未収集
    （実際に配信できた2チャンネルを捕捉成功として最優先）。
    """
    detail = []
    for it in items:
        if it.code in delivered:
            status = STATUS_DELIVERED
        elif it.code in tech_delivered:
            status = STATUS_DELIVERED_TECH
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
    """母集団(in_universe=True)の status 別件数と捕捉率(2チャンネル合算)を集計する。"""
    uni = [d for d in detail if d["in_universe"]]
    counts = {
        STATUS_DELIVERED: 0, STATUS_DELIVERED_TECH: 0,
        STATUS_SIGNALED: 0, STATUS_NEUTRAL: 0, STATUS_NOT_COLLECTED: 0,
    }
    for d in uni:
        counts[d["status"]] += 1
    total = len(uni)
    captured = counts[STATUS_DELIVERED] + counts[STATUS_DELIVERED_TECH]
    capture_rate = round(captured / total, 4) if total else None
    return {"universe": total, "counts": counts, "capture_rate": capture_rate}
