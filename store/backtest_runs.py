"""backtest_runs テーブルへの読み書き。元本シミュレーションの実行スナップショットを残す。"""
from __future__ import annotations

import json
from typing import Optional

from .db import execute


def record(as_of: str, start_capital: int, snapshot: dict) -> int:
    """1回分の計算結果(snapshot)を1行記録し、採番された id を返す。"""
    result = execute(
        """
        INSERT INTO backtest_runs (as_of, start_capital, snapshot)
        VALUES (?, ?, ?)
        """,
        (as_of, start_capital, json.dumps(snapshot, ensure_ascii=False)),
    )
    return result.last_row_id or 0


def get_latest() -> Optional[dict]:
    """最新の実行記録を返す。snapshot は dict に戻す。無ければ None。"""
    rows = execute(
        """
        SELECT id, run_at, as_of, start_capital, snapshot
        FROM backtest_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).rows
    if not rows:
        return None
    row = rows[0]
    row["snapshot"] = json.loads(row["snapshot"]) if row.get("snapshot") else {}
    return row
