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
) -> int:
    """捕捉率の計測結果を1行記録し、採番された id を返す。"""
    result = execute(
        """
        INSERT INTO coverage_runs
            (ranking_date, ranking_type, top_n, universe, captured, signaled,
             neutral, not_collected, capture_rate, detail, proposal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ranking_date, ranking_type, top_n, universe, captured, signaled,
            neutral, not_collected, capture_rate,
            json.dumps(detail, ensure_ascii=False), proposal,
        ),
    )
    return result.last_row_id or 0


def get_recent(limit: int = 30) -> List[dict]:
    """最近の計測記録を新しい順に返す。detail は Python リストに戻す。"""
    rows = execute(
        """
        SELECT id, run_at, ranking_date, ranking_type, top_n, universe,
               captured, signaled, neutral, not_collected, capture_rate, detail, proposal
        FROM coverage_runs
        ORDER BY ranking_date DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).rows
    for row in rows:
        row["detail"] = json.loads(row["detail"]) if row.get("detail") else []
    return rows
