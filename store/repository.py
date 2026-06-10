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


def get_unread() -> list[Article]:
    """未読記事を取得日時の昇順で返す。"""
    result = execute(
        """
        SELECT id, title, body, url, fetched_at, is_read
        FROM articles
        WHERE is_read = 0
        ORDER BY fetched_at ASC
        """
    )
    return [
        Article(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            url=row["url"],
            fetched_at=row["fetched_at"],
            is_read=bool(row["is_read"]),
        )
        for row in result.rows
    ]


def mark_as_read(article_id: int) -> None:
    """記事を既読にする。"""
    execute(
        "UPDATE articles SET is_read = 1 WHERE id = ?",
        (article_id,),
    )
