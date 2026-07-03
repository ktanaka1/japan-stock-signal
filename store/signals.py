"""シグナル（銘柄が特定できたポジ/ネガ記事）の保存と通知管理。"""
from __future__ import annotations

import json

from .db import execute


def add(article_id: int, sentiment: str, summary: str, stocks: list, url: str,
        impact: int = 3) -> None:
    """シグナルを登録する。同一記事の重複は無視。impact は旬度(1〜5)。"""
    execute(
        "INSERT OR IGNORE INTO signals (article_id, sentiment, summary, stocks, url, impact)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (article_id, sentiment, summary, json.dumps(stocks, ensure_ascii=False), url, impact),
    )


def get_unnotified() -> list[dict]:
    """未通知のシグナルを古い順に返す。"""
    rows = execute(
        """
        SELECT id, article_id, sentiment, summary, stocks, url, impact, created_at,
               promoted_at, promote_reason
        FROM signals
        WHERE notified_at IS NULL
        ORDER BY id ASC
        """
    ).rows
    for row in rows:
        row["stocks"] = json.loads(row["stocks"])
    return rows


def get_by_ids(signal_ids: list[int]) -> list[dict]:
    """指定IDのシグナルをID昇順で返す（配信ログから内容を復元する用途）。

    D1 は 1クエリのバインド変数が約100個までのため、IN句を50件ずつに分割して問い合わせる
    （一括配信で notified_ids が数百件になると IN(?,...) が上限超過で 400 になる）。
    """
    if not signal_ids:
        return []
    chunk_size = 50
    rows: list[dict] = []
    for i in range(0, len(signal_ids), chunk_size):
        chunk = signal_ids[i : i + chunk_size]
        placeholders = ", ".join(["?"] * len(chunk))
        rows.extend(execute(
            f"""
            SELECT id, article_id, sentiment, summary, stocks, url, impact, created_at,
                   promoted_at, promote_reason
            FROM signals
            WHERE id IN ({placeholders})
            """,
            tuple(chunk),
        ).rows)
    rows.sort(key=lambda r: r["id"])
    for row in rows:
        row["stocks"] = json.loads(row["stocks"])
    return rows


def promote(signal_id: int, reason: str) -> None:
    """シグナルを逆引き昇格する（impact閾値未満でも翌朝のdigestで配信される）。

    未配信・未昇格の行だけを更新する（冪等。配信済み・昇格済みは触らない）。
    """
    execute(
        "UPDATE signals SET promoted_at = datetime('now'), promote_reason = ? "
        "WHERE id = ? AND notified_at IS NULL AND promoted_at IS NULL",
        (reason, signal_id),
    )


def mark_notified(signal_ids: list[int]) -> None:
    """シグナルを通知済みにする。"""
    chunk_size = 50
    for i in range(0, len(signal_ids), chunk_size):
        chunk = signal_ids[i : i + chunk_size]
        placeholders = ", ".join(["?"] * len(chunk))
        execute(
            f"UPDATE signals SET notified_at = datetime('now') WHERE id IN ({placeholders})",
            tuple(chunk),
        )
