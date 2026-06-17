"""分析結果（article_analyses）の保存と照会。"""
from __future__ import annotations

import json

from .db import execute


def save(
    article_id: int,
    sentiment: str,
    summary: str,
    reason: str,
    stocks: list,
    became_signal: bool,
) -> None:
    execute(
        "INSERT OR IGNORE INTO article_analyses"
        " (article_id, sentiment, summary, reason, stocks, became_signal)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            article_id,
            sentiment,
            summary,
            reason,
            json.dumps(stocks, ensure_ascii=False),
            1 if became_signal else 0,
        ),
    )


def get_all_by_date(date_str: str, limit: int = 500) -> list[dict]:
    """指定日(JST)の全記事を分析結果・シグナル情報付きで返す。未分析記事も含む。"""
    rows = execute(
        """
        SELECT
            a.id          AS article_id,
            a.title,
            a.url,
            a.fetched_at,
            a.is_read,
            aa.sentiment,
            aa.summary,
            aa.reason,
            aa.stocks,
            aa.became_signal,
            s.notified_at
        FROM articles a
        LEFT JOIN article_analyses aa ON a.id = aa.article_id
        LEFT JOIN signals s ON a.id = s.article_id
        WHERE date(a.fetched_at, '+9 hours') = ?
        ORDER BY a.fetched_at DESC
        LIMIT ?
        """,
        (date_str, limit),
    ).rows
    for row in rows:
        row["stocks"] = json.loads(row["stocks"]) if row.get("stocks") else []
    return rows


def get_stats_by_date(date_str: str) -> dict:
    """指定日(JST)のパイプライン統計を返す。"""
    articles = execute(
        "SELECT COUNT(*) AS cnt FROM articles WHERE date(fetched_at, '+9 hours') = ?",
        (date_str,),
    ).rows[0]["cnt"]

    # 記事一覧の判定列と一致させるため article_analyses から数える
    # （この機能以前の過去データは行がないため 0 になる）
    analyzed = execute(
        """
        SELECT COUNT(*) AS cnt FROM article_analyses aa
        JOIN articles a ON a.id = aa.article_id
        WHERE date(a.fetched_at, '+9 hours') = ?
        """,
        (date_str,),
    ).rows[0]["cnt"]

    signaled = execute(
        """
        SELECT COUNT(*) AS cnt FROM signals s
        JOIN articles a ON a.id = s.article_id
        WHERE date(a.fetched_at, '+9 hours') = ?
        """,
        (date_str,),
    ).rows[0]["cnt"]

    notified = execute(
        """
        SELECT COUNT(*) AS cnt FROM signals s
        JOIN articles a ON a.id = s.article_id
        WHERE date(a.fetched_at, '+9 hours') = ? AND s.notified_at IS NOT NULL
        """,
        (date_str,),
    ).rows[0]["cnt"]

    return {
        "articles": articles,
        "analyzed": analyzed,
        "signaled": signaled,
        "notified": notified,
    }
