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
    rescued: int = 0,
) -> int:
    """捕捉率の計測結果を1行記録し、採番された id を返す。

    captured はニュース版の配信件数、captured_tech はテクニカル版の配信件数。
    capture_rate は両者の合算/母集団。rescued（逆引き昇格で救済配信）は
    capture_rate に含めない別枠計上（自己言及防止）。
    """
    result = execute(
        """
        INSERT INTO coverage_runs
            (ranking_date, ranking_type, top_n, universe, captured, captured_tech,
             signaled, neutral, not_collected, capture_rate, detail, proposal,
             finalized, rescued)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            ranking_date, ranking_type, top_n, universe, captured, captured_tech,
            signaled, neutral, not_collected, capture_rate,
            json.dumps(detail, ensure_ascii=False), proposal, rescued,
        ),
    )
    return result.last_row_id or 0


def update_finalize(
    row_id: int,
    captured: int,
    captured_tech: int,
    signaled: int,
    neutral: int,
    not_collected: int,
    capture_rate: Optional[float],
    detail: list,
    proposal: str,
    rescued: int = 0,
) -> None:
    """暫定記録(finalized=0)を配信完了後の確定値で更新し finalized=1 にする。

    ranking_date / ranking_type / top_n / universe（母集団）は不変なので触らない。
    配信状況に依存する captured 系・rescued・capture_rate・detail・proposal だけ上書きする。
    """
    execute(
        """
        UPDATE coverage_runs
        SET captured = ?, captured_tech = ?, signaled = ?, neutral = ?,
            not_collected = ?, capture_rate = ?, detail = ?, proposal = ?,
            rescued = ?, finalized = 1
        WHERE id = ?
        """,
        (
            captured, captured_tech, signaled, neutral, not_collected, capture_rate,
            json.dumps(detail, ensure_ascii=False), proposal, rescued, row_id,
        ),
    )


def get_unfinalized_before(ranking_date: str) -> List[dict]:
    """ranking_date より前で未確定(finalized=0)の暫定行を、対象日ごと最新1件だけ返す。

    同一営業日に snapshot が複数回走ると暫定行が重複するため、ranking_date ごと
    最大 id（＝最新の暫定計測）だけを確定対象とする。detail は Python リストに戻す。

    内側の MAX(id) は finalized を問わず全行から取る。未確定行に限定すると、最新行を
    確定した翌日に同日の旧い暫定行が「未確定の中の最大id」として浮上し、二重確定される
    （旧暫定行は最新行の確定後も finalized=0 のまま残るが、二度と選ばれない死蔵行になる）。
    """
    rows = execute(
        """
        SELECT id, run_at, ranking_date, ranking_type, top_n, universe,
               captured, captured_tech, signaled, neutral, not_collected,
               capture_rate, detail, proposal, finalized, rescued
        FROM coverage_runs
        WHERE finalized = 0 AND ranking_date < ?
          AND id IN (
              SELECT MAX(id) FROM coverage_runs
              WHERE ranking_date < ?
              GROUP BY ranking_date
          )
        ORDER BY ranking_date ASC
        """,
        (ranking_date, ranking_date),
    ).rows
    for row in rows:
        row["detail"] = json.loads(row["detail"]) if row.get("detail") else []
    return rows


def get_recent(limit: int = 30) -> List[dict]:
    """最近の計測記録を新しい順に返す。detail は Python リストに戻す。"""
    rows = execute(
        """
        SELECT id, run_at, ranking_date, ranking_type, top_n, universe,
               captured, captured_tech, signaled, neutral, not_collected,
               capture_rate, detail, proposal, finalized, rescued
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
               capture_rate, detail, proposal, finalized, rescued
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
