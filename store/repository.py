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


def save_many(articles: list[Article]) -> int:
    """記事をまとめて保存し、新規保存できた件数を返す。既存URLはスキップ。"""
    # D1のREST APIは1クエリ=1リクエストなので、複数行VALUESでラウンドトリップを減らす
    chunk_size = 30
    new_count = 0
    for i in range(0, len(articles), chunk_size):
        chunk = articles[i : i + chunk_size]
        placeholders = ", ".join(["(?, ?, ?)"] * len(chunk))
        params: list = []
        for a in chunk:
            params.extend([a.title, a.body, a.url])
        result = execute(
            f"INSERT OR IGNORE INTO articles (title, body, url) VALUES {placeholders}",
            tuple(params),
        )
        new_count += result.changes
    return new_count


def get_unread() -> list[Article]:
    """未読記事を取得日時の昇順で返す。"""
    result = execute(
        """
        SELECT id, title, body, url, fetched_at, is_read,
               security_code, xbrl_metrics, full_body, correction_reason, body_status
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
            security_code=row["security_code"],
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
