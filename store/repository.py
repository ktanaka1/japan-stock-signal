from __future__ import annotations

from .db import execute
from .models import Article


def save(article: Article) -> tuple[Article, bool]:
    """記事を保存する。(article, is_new) を返す。URLが重複する場合は is_new=False。"""
    result = execute(
        "INSERT OR IGNORE INTO articles (title, body, url) VALUES (?, ?, ?)",
        (article.title, article.body, article.url),
    )
    is_new = result.changes > 0
    if is_new:
        article.id = result.last_row_id
    else:
        row = execute(
            "SELECT id, fetched_at, is_read FROM articles WHERE url = ?",
            (article.url,),
        ).rows[0]
        article.id = row["id"]
        article.fetched_at = row["fetched_at"]
        article.is_read = bool(row["is_read"])
    return article, is_new


def save_with_body(article: Article) -> tuple[Article, bool]:
    """本文付き記事を保存する。(article, is_new) を返す。URL重複時は is_new=False。

    系統A該当・本文取得を試みた記事用。新規 INSERT 時のみ本文カラムも書き込む。
    既存URLは挿入をスキップし、本文取得を無駄に走らせない判断材料（is_new）を返す。
    """
    result = execute(
        """
        INSERT OR IGNORE INTO articles
            (title, body, url, security_code, security_name, xbrl_metrics, full_body,
             correction_reason, body_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article.title, article.body, article.url,
            article.security_code, article.security_name, article.xbrl_metrics,
            article.full_body, article.correction_reason, article.body_status,
        ),
    )
    is_new = result.changes > 0
    if is_new:
        article.id = result.last_row_id
    else:
        row = execute(
            "SELECT id, fetched_at, is_read FROM articles WHERE url = ?",
            (article.url,),
        ).rows[0]
        article.id = row["id"]
        article.fetched_at = row["fetched_at"]
        article.is_read = bool(row["is_read"])
    return article, is_new


def save_many(articles: list[Article]) -> int:
    """記事をまとめて保存し、新規保存できた件数を返す。既存URLはスキップ。

    TDnet開示は権威ある security_code / security_name を持つ（RSS由来は None）。
    本文取得をしない系統B・非該当でも同定情報は失わず保存する（銘柄同定の権威化）。
    """
    # D1のREST APIは1クエリ=1リクエストなので、複数行VALUESでラウンドトリップを減らす。
    # ただしD1は1クエリのバインド変数が約100個まで（ローカルSQLiteでは再現しない）。
    # 1行5列なので 18行=90個に抑える（5列化前は3列×30行=90個だった）。
    chunk_size = 18
    new_count = 0
    for i in range(0, len(articles), chunk_size):
        chunk = articles[i : i + chunk_size]
        placeholders = ", ".join(["(?, ?, ?, ?, ?)"] * len(chunk))
        params: list = []
        for a in chunk:
            params.extend([a.title, a.body, a.url, a.security_code, a.security_name])
        result = execute(
            "INSERT OR IGNORE INTO articles (title, body, url, security_code, security_name)"
            f" VALUES {placeholders}",
            tuple(params),
        )
        new_count += result.changes
    return new_count


def existing_urls(urls: list[str]) -> set[str]:
    """与えたURLのうち既にDBに在るものを返す（本文取得の重複実行を避ける事前チェック）。"""
    found: set[str] = set()
    if not urls:
        return found
    chunk_size = 50
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        placeholders = ", ".join(["?"] * len(chunk))
        rows = execute(
            f"SELECT url FROM articles WHERE url IN ({placeholders})",
            tuple(chunk),
        ).rows
        found.update(r["url"] for r in rows)
    return found


def get_unread(limit: int | None = None) -> list[Article]:
    """未読記事を取得日時の昇順で返す。

    limit を指定すると古い順に最大 limit 件だけ返す（1回の実行で処理する件数の上限）。
    """
    sql = """
        SELECT id, title, body, url, fetched_at, is_read,
               security_code, security_name, xbrl_metrics, full_body,
               correction_reason, body_status
        FROM articles
        WHERE is_read = 0
        ORDER BY fetched_at ASC
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    result = execute(sql, params)
    return [
        Article(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            url=row["url"],
            fetched_at=row["fetched_at"],
            is_read=bool(row["is_read"]),
            security_code=row["security_code"],
            security_name=row["security_name"],
            xbrl_metrics=row["xbrl_metrics"],
            full_body=row["full_body"],
            correction_reason=row["correction_reason"],
            body_status=row["body_status"],
        )
        for row in result.rows
    ]


def mark_as_read(article_id: int) -> None:
    """記事を既読にする。"""
    execute(
        "UPDATE articles SET is_read = 1 WHERE id = ?",
        (article_id,),
    )


def mark_many_as_read(article_ids: list[int]) -> None:
    """記事をまとめて既読にする。"""
    chunk_size = 50
    for i in range(0, len(article_ids), chunk_size):
        chunk = article_ids[i : i + chunk_size]
        placeholders = ", ".join(["?"] * len(chunk))
        execute(
            f"UPDATE articles SET is_read = 1 WHERE id IN ({placeholders})",
            tuple(chunk),
        )
