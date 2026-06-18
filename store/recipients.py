import logging
import re

from .db import execute

logger = logging.getLogger(__name__)

# LINEのuserIdは "U" + 32桁の16進小文字。不正IDのmulticast混入で
# 全受信者への配信が落ちる事故があったため、登録時点で弾く。
_LINE_USER_ID_RE = re.compile(r"^U[0-9a-f]{32}$")


def is_valid_user_id(line_user_id: str) -> bool:
    """LINE userId が正しい形式（U + 32桁16進小文字）かを返す。"""
    return bool(line_user_id) and bool(_LINE_USER_ID_RE.match(line_user_id))


def add(line_user_id: str) -> None:
    """受信者を追加する。重複は無視。

    形式が不正なIDはINSERTせず警告ログのみ出す（例外は投げない＝
    webhookハンドラを巻き込まないため）。
    """
    if not is_valid_user_id(line_user_id):
        logger.warning("invalid LINE userId rejected: %r", line_user_id)
        return
    execute(
        "INSERT OR IGNORE INTO recipients (line_user_id) VALUES (?)",
        (line_user_id,),
    )


def remove(line_user_id: str) -> None:
    """受信者を削除する（アンフォロー時）。"""
    execute(
        "DELETE FROM recipients WHERE line_user_id = ?",
        (line_user_id,),
    )


def get_all() -> list[str]:
    """全受信者のLINEユーザーIDを返す。"""
    result = execute("SELECT line_user_id FROM recipients")
    return [row["line_user_id"] for row in result.rows]
