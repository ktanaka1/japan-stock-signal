from .db import get_connection


def add(line_user_id: str) -> None:
    """受信者を追加する。重複は無視。"""
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO recipients (line_user_id) VALUES (?)",
            (line_user_id,),
        )


def remove(line_user_id: str) -> None:
    """受信者を削除する（アンフォロー時）。"""
    conn = get_connection()
    with conn:
        conn.execute(
            "DELETE FROM recipients WHERE line_user_id = ?",
            (line_user_id,),
        )


def get_all() -> list[str]:
    """全受信者のLINEユーザーIDを返す。"""
    conn = get_connection()
    rows = conn.execute("SELECT line_user_id FROM recipients").fetchall()
    return [row["line_user_id"] for row in rows]
