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
    impact: int = 3,
) -> None:
    execute(
        "INSERT OR IGNORE INTO article_analyses"
        " (article_id, sentiment, summary, reason, stocks, became_signal, impact)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            article_id,
            sentiment,
            summary,
            reason,
            json.dumps(stocks, ensure_ascii=False),
            1 if became_signal else 0,
            impact,
        ),
    )


def get_available_years() -> list[str]:
    """記事が存在する年（JST）を新しい順に返す。"""
    rows = execute(
        "SELECT DISTINCT strftime('%Y', fetched_at, '+9 hours') AS y "
        "FROM articles ORDER BY y DESC"
    ).rows
    return [r["y"] for r in rows if r["y"]]


def _build_article_filter(
    date_str: str | None,
    year: str | None,
    sentiment: str | None,
    code: str | None,
    notified: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
) -> tuple[str, list]:
    """記事一覧のフィルタから WHERE句と paramsを組み立てる。

    get_articles / count_articles で共通利用し、件数と一覧の条件ズレを防ぐ。
    （JOIN は a=articles / aa=article_analyses / s=signals 前提）
    notified="yes" で LINE配信済み（s.notified_at IS NOT NULL）のみに絞る。
    price_min/price_max は終値（値ごろ）。いずれかの銘柄が範囲に収まる記事を抽出する。
    （前日比%での絞り込みは『旬になる直前の値＝ノイズ』のため廃止した）
    """
    where = []
    params: list = []
    if date_str:
        where.append("date(a.fetched_at, '+9 hours') = ?")
        params.append(date_str)
    elif year:
        where.append("strftime('%Y', a.fetched_at, '+9 hours') = ?")
        params.append(year)
    if sentiment == "none":
        where.append("aa.sentiment IS NULL")
    elif sentiment:
        where.append("aa.sentiment = ?")
        params.append(sentiment)
    if code:
        where.append(
            "aa.stocks IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM json_each(aa.stocks) je "
            "WHERE json_extract(je.value, '$.code') LIKE ?)"
        )
        params.append(code + "%")
    if notified == "yes":
        where.append("s.notified_at IS NOT NULL")
    # 価格・前日比%は stocks JSON 内のいずれかの銘柄が範囲に収まればヒット。
    # 価格未付与の銘柄（close/change_pct なし）は CAST が NULL になり範囲比較で除外される。
    if price_min is not None or price_max is not None:
        lo = price_min if price_min is not None else -1e18
        hi = price_max if price_max is not None else 1e18
        where.append(
            "aa.stocks IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM json_each(aa.stocks) je "
            "WHERE CAST(json_extract(je.value, '$.close') AS REAL) BETWEEN ? AND ?)"
        )
        params.extend([lo, hi])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def get_articles(
    date_str: str | None = None,
    year: str | None = None,
    sentiment: str | None = None,
    code: str | None = None,
    notified: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], bool]:
    """記事を分析結果・シグナル情報付きで返す。未分析記事も含む。

    既定の並び順は『インパクトスコア降順』（旬度の高い主役候補が上）。同点は新着順、
    impact 未設定(NULL/未分析)は末尾。date_str / year / sentiment / code / notified /
    price_min/max は任意のフィルタ。sentiment="none" は未分析記事のみ、notified="yes" は配信済み。
    次ページの有無を判定するため limit+1 件取得し、(rows[:limit], has_next) を返す。
    """
    where_sql, params = _build_article_filter(
        date_str, year, sentiment, code, notified, price_min, price_max
    )

    sql = f"""
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
            aa.impact,
            s.notified_at
        FROM articles a
        LEFT JOIN article_analyses aa ON a.id = aa.article_id
        LEFT JOIN signals s ON a.id = s.article_id
        {where_sql}
        ORDER BY (aa.impact IS NULL), aa.impact DESC, a.fetched_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit + 1, offset])
    rows = execute(sql, tuple(params)).rows

    has_next = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["stocks"] = json.loads(row["stocks"]) if row.get("stocks") else []
    return rows, has_next


def count_articles(
    date_str: str | None = None,
    year: str | None = None,
    sentiment: str | None = None,
    code: str | None = None,
    notified: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
) -> int:
    """get_articles と同一フィルタに一致する記事の総件数を返す。

    WHERE句は get_articles と共通の _build_article_filter を使うため条件は完全一致。
    """
    where_sql, params = _build_article_filter(
        date_str, year, sentiment, code, notified, price_min, price_max
    )
    sql = f"""
        SELECT COUNT(*) AS cnt
        FROM articles a
        LEFT JOIN article_analyses aa ON a.id = aa.article_id
        LEFT JOIN signals s ON a.id = s.article_id
        {where_sql}
    """
    return execute(sql, tuple(params)).rows[0]["cnt"]


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


def get_stats_recent_days(days: int = 3, page: int = 0) -> tuple[list[dict], bool]:
    """収集記事がある日(JST)を新しい順に days 日分、パイプライン統計付きで返す（ページング対応）。

    page は0始まり（0=最新days日、1=その前のdays日…）。記事がある日(JST)を新しい順に並べ、
    page*days 日分スキップして次の days 日分を対象に集計する。
    集計定義は get_stats_by_date と完全に同一にする
    （analyzed=article_analyses / signaled=signals / notified=signals.notified_at IS NOT NULL、
     いずれも記事の fetched_at JST基準）。
    D1往復を抑えるため日付でGROUP BYした集計を articles/analyzed/signaled/notified の
    計4クエリで取得し、Python側で日付キーにマージする。
    さらに過去があるか(has_older)は対象日付の取得時に days+1 件取れるかで判定する
    （記事一覧の has_next と同じ手法）。
    戻り値: (list[dict], has_older)。各dict = {date, articles, analyzed, signaled, notified}（新しい順）。
    """
    if days <= 0:
        return [], False
    page = max(0, page)
    offset = page * days

    # 対象日: 記事がある日(JST)を新しい順に offset スキップして days+1 件取得。
    # +1 件取れたら「さらに過去がある」(has_older)。これを基準に他カウントを左マージする。
    target_rows = execute(
        "SELECT date(fetched_at, '+9 hours') AS d, COUNT(*) AS cnt "
        "FROM articles "
        "GROUP BY date(fetched_at, '+9 hours') "
        "ORDER BY d DESC LIMIT ? OFFSET ?",
        (days + 1, offset),
    ).rows

    has_older = len(target_rows) > days
    target_rows = target_rows[:days]
    if not target_rows:
        return [], has_older

    dates = [r["d"] for r in target_rows]
    articles_map = {r["d"]: r["cnt"] for r in target_rows}

    placeholders = ",".join(["?"] * len(dates))

    analyzed_map = {
        r["d"]: r["cnt"]
        for r in execute(
            f"""
            SELECT date(a.fetched_at, '+9 hours') AS d, COUNT(*) AS cnt
            FROM article_analyses aa
            JOIN articles a ON a.id = aa.article_id
            WHERE date(a.fetched_at, '+9 hours') IN ({placeholders})
            GROUP BY date(a.fetched_at, '+9 hours')
            """,
            tuple(dates),
        ).rows
    }

    signaled_map = {
        r["d"]: r["cnt"]
        for r in execute(
            f"""
            SELECT date(a.fetched_at, '+9 hours') AS d, COUNT(*) AS cnt
            FROM signals s
            JOIN articles a ON a.id = s.article_id
            WHERE date(a.fetched_at, '+9 hours') IN ({placeholders})
            GROUP BY date(a.fetched_at, '+9 hours')
            """,
            tuple(dates),
        ).rows
    }

    notified_map = {
        r["d"]: r["cnt"]
        for r in execute(
            f"""
            SELECT date(a.fetched_at, '+9 hours') AS d, COUNT(*) AS cnt
            FROM signals s
            JOIN articles a ON a.id = s.article_id
            WHERE date(a.fetched_at, '+9 hours') IN ({placeholders})
              AND s.notified_at IS NOT NULL
            GROUP BY date(a.fetched_at, '+9 hours')
            """,
            tuple(dates),
        ).rows
    }

    stats = [
        {
            "date": d,
            "articles": articles_map.get(d, 0),
            "analyzed": analyzed_map.get(d, 0),
            "signaled": signaled_map.get(d, 0),
            "notified": notified_map.get(d, 0),
        }
        for d in dates
    ]
    return stats, has_older


def purge_disposable(retention_days: int = 90) -> dict:
    """価値の低い記事を保持期間経過後に削除する（データ保持・ローテーション）。

    削除対象は「sentiment='neutral' かつ 銘柄なし('[]'/'') かつ fetched_at が
    retention_days より古い」記事のみ。signals に紐づくもの（成果物）は二重防御で除外する。
    まず該当する article_analyses を消し、続いて分析もシグナルも無い古い既読記事を消す。
    silent な削除を避けるため削除件数を返す（呼び出し側でログ化する）。
    """
    cutoff = f"-{int(retention_days)} days"
    analyses_deleted = execute(
        """
        DELETE FROM article_analyses
        WHERE sentiment = 'neutral'
          AND stocks IN ('[]', '')
          AND article_id NOT IN (SELECT article_id FROM signals)
          AND article_id IN (
              SELECT id FROM articles WHERE fetched_at < datetime('now', ?)
          )
        """,
        (cutoff,),
    ).changes
    articles_deleted = execute(
        """
        DELETE FROM articles
        WHERE fetched_at < datetime('now', ?)
          AND is_read = 1
          AND id NOT IN (SELECT article_id FROM signals)
          AND id NOT IN (SELECT article_id FROM article_analyses)
        """,
        (cutoff,),
    ).changes
    return {
        "retention_days": int(retention_days),
        "analyses_deleted": analyses_deleted,
        "articles_deleted": articles_deleted,
    }
