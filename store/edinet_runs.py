"""edinet_runs テーブルへの読み書き。EDINET大量保有アラートの実行記録（technical_runs と同型）。"""
from __future__ import annotations

import json
from typing import List, Optional

from .db import execute


def record(
    target_date: str,
    status: str,
    pick_count: int = 0,
    line_ok: int = 0,
    line_error: int = 0,
    picks: Optional[list] = None,
    error_detail: Optional[str] = None,
) -> int:
    """EDINETアラートの実行結果を1行記録し、採番された id を返す。"""
    result = execute(
        """
        INSERT INTO edinet_runs
            (target_date, status, pick_count, line_ok, line_error, picks, error_detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target_date, status, pick_count, line_ok, line_error,
            json.dumps(picks or [], ensure_ascii=False), error_detail,
        ),
    )
    return result.last_row_id or 0


def get_recent(limit: int = 30) -> List[dict]:
    """最近の実行記録を新しい順に返す。picks は Python リストに戻す。"""
    rows = execute(
        """
        SELECT id, run_at, target_date, status, pick_count, line_ok, line_error, picks, error_detail
        FROM edinet_runs
        ORDER BY target_date DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).rows
    for row in rows:
        row["picks"] = json.loads(row["picks"]) if row.get("picks") else []
    return rows
