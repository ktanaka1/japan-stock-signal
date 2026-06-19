"""シグナル（銘柄が特定できたポジ/ネガ記事）の保存と通知管理。"""
from __future__ import annotations

import json

from .db import execute


def add(article_id: int, sentiment: str, summary: str, stocks: list, url: str) -> None:
    """シグナルを登録する。同一記事の重複は無視。"""
    execute(
        "INSERT OR IGNORE INTO signals (article_id, sentiment, summary, stocks, url)"
        " VALUES (?, ?, ?, ?, ?)",
        (article_id, sentiment, summary, json.dumps(stocks, ensure_ascii=False), url),
    )


def get_unnotified() -> list[dict]:
    """未通知のシグナルを古い順に返す。"""
    rows = execute(
        """
        SELECT id, article_id, sentiment, summary, stocks, url, created_at
        FROM signals
        WHERE notified_at IS NULL
        ORDER BY id ASC
        """
    ).rows
    for row in rows:
        row["stocks"] = json.loads(row["stocks"])
    return rows


def get_by_ids(signal_ids: list[int]) -> list[dict]:
    """指定IDのシグナルをID昇順で返す（配信ログから内容を復元する用途）。"""
    if not signal_ids:
        return []
    placeholders = ", ".join(["?"] * len(signal_ids))
    rows = execute(
        f"""
        SELECT id, article_id, sentiment, summary, stocks, url, created_at
        FROM signals
        WHERE id IN ({placeholders})
        ORDER BY id ASC
        """,
        tuple(signal_ids),
    ).rows
    for row in rows:
        row["stocks"] = json.loads(row["stocks"])
    return rows


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
