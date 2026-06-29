"""digest_runs テーブルへの読み書き。LINE配信の実行記録を残す。"""
from __future__ import annotations

import json
from typing import List, Optional

from .db import execute


def record(
    status: str,
    signal_count: int = 0,
    line_ok: int = 0,
    line_error: int = 0,
    error_detail: Optional[str] = None,
    notified_ids: Optional[List[int]] = None,
) -> int:
    """配信実行結果を1行記録し、採番された id を返す。

    status:
        'ok'            -- 全受信者への送信が成功
        'partial'       -- 一部受信者への送信が失敗
        'error'         -- 例外が発生し送信できなかった
        'no_recipients' -- 受信者ゼロのためスキップ
    """
    ids_json = json.dumps(notified_ids) if notified_ids is not None else None
    result = execute(
        """
        INSERT INTO digest_runs (status, signal_count, line_ok, line_error, error_detail, notified_ids)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (status, signal_count, line_ok, line_error, error_detail, ids_json),
    )
    return result.last_row_id or 0


def _restore_notified_ids(rows: List[dict]) -> List[dict]:
    """notified_ids カラムの JSON 文字列を Python リストに戻す（in-place）。"""
    for row in rows:
        if row.get("notified_ids"):
            row["notified_ids"] = json.loads(row["notified_ids"])
        else:
            row["notified_ids"] = []
    return rows


def get_recent(limit: int = 20) -> List[dict]:
    """最近の実行記録を新しい順に返す。notified_ids は Python リストに戻す。"""
    rows = execute(
        """
        SELECT id, run_at, status, signal_count, line_ok, line_error, error_detail, notified_ids
        FROM digest_runs
        ORDER BY run_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).rows
    return _restore_notified_ids(rows)


def get_page(limit: int, offset: int) -> tuple[list[dict], bool]:
    """配信ログを新しい順に1ページ分返す。

    limit+1 件取得して has_next を判定し、表示用に rows[:limit] を返す。
    戻り値は (rows, has_next)。notified_ids は Python リストに戻す。
    """
    rows = execute(
        """
        SELECT id, run_at, status, signal_count, line_ok, line_error, error_detail, notified_ids
        FROM digest_runs
        ORDER BY run_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (limit + 1, offset),
    ).rows
    has_next = len(rows) > limit
    rows = rows[:limit]
    return _restore_notified_ids(rows), has_next
