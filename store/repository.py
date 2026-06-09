from .db import get_connection
from .models import Article


def save(article: Article) -> tuple[Article, bool]:
    """記事を保存する。(article, is_new) を返す。URLが重複する場合は is_new=False。"""
    conn = get_connection()
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO articles (title, body, url) VALUES (?, ?, ?)",
            (article.title, article.body, article.url),
        )
        is_new = cur.rowcount > 0
        if is_new:
            article.id = cur.lastrowid
        else:
            row = conn.execute(
                "SELECT id, fetched_at, is_read FROM articles WHERE url = ?",
                (article.url,),
            ).fetchone()
            article.id = row["id"]
            article.fetched_at = row["fetched_at"]
            article.is_read = bool(row["is_read"])
    return article, is_new


def get_unread() -> list[Article]:
    """未読記事を取得日時の昇順で返す。"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, title, body, url, fetched_at, is_read
        FROM articles
        WHERE is_read = 0
        ORDER BY fetched_at ASC
        """
    ).fetchall()
    return [
        Article(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            url=row["url"],
            fetched_at=row["fetched_at"],
            is_read=bool(row["is_read"]),
        )
        for row in rows
    ]


def mark_as_read(article_id: int) -> None:
    """記事を既読にする。"""
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE articles SET is_read = 1 WHERE id = ?",
            (article_id,),
        )
