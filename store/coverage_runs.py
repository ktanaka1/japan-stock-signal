"""coverage_runs テーブルへの読み書き。捕捉率フィードバックの実行記録を残す。"""
from __future__ import annotations

import json
from typing import List, Optional

from .db import execute


def record(
    ranking_date: str,
    ranking_type: str,
    top_n: int,
    universe: int,
    captured: int,
    signaled: int,
    neutral: int,
    not_collected: int,
    capture_rate: Optional[float],
    detail: list,
    proposal: str,
    captured_tech: int = 0,
) -> int:
    """捕捉率の計測結果を1行記録し、採番された id を返す。

    captured はニュース版の配信件数、captured_tech はテクニカル版の配信件数。
    capture_rate は両者の合算/母集団。
    """
    result = execute(
        """
        INSERT INTO coverage_runs
            (ranking_date, ranking_type, top_n, universe, captured, captured_tech,
             signaled, neutral, not_collected, capture_rate, detail, proposal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ranking_date, ranking_type, top_n, universe, captured, captured_tech,
            signaled, neutral, not_collected, capture_rate,
            json.dumps(detail, ensure_ascii=False), proposal,
        ),
    )
    return result.last_row_id or 0


def get_recent(limit: int = 30) -> List[dict]:
    """最近の計測記録を新しい順に返す。detail は Python リストに戻す。"""
    rows = execute(
        """
        SELECT id, run_at, ranking_date, ranking_type, top_n, universe,
               captured, captured_tech, signaled, neutral, not_collected,
               capture_rate, detail, proposal
        FROM coverage_runs
        ORDER BY ranking_date DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).rows
    for row in rows:
        row["detail"] = json.loads(row["detail"]) if row.get("detail") else []
    return rows


def get_page(limit: int, offset: int) -> tuple[list[dict], bool]:
    """捕捉率の記録を営業日ごと最新1件に畳んで新しい順に1ページ分返す。

    同一営業日に feedback が複数回走ると行が重複するため、ranking_date ごと
    最大 id（＝最新計測）だけを採用する。limit+1 件取得して has_next を判定し、
    rows[:limit] を返す。戻り値は (rows, has_next)。detail は Python リストに戻す。
    """
    rows = execute(
        """
        SELECT id, run_at, ranking_date, ranking_type, top_n, universe,
               captured, captured_tech, signaled, neutral, not_collected,
               capture_rate, detail, proposal
        FROM coverage_runs
        WHERE id IN (SELECT MAX(id) FROM coverage_runs GROUP BY ranking_date)
        ORDER BY ranking_date DESC
        LIMIT ? OFFSET ?
        """,
        (limit + 1, offset),
    ).rows
    has_next = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["detail"] = json.loads(row["detail"]) if row.get("detail") else []
    return rows, has_next
