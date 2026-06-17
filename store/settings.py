"""設定値のDB管理。コードに埋め込まれた条件を管理画面から変更できるようにする。"""
from __future__ import annotations

import logging

from .db import execute

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_DEFAULT = (
    "あなたは日本株デイトレーダーを支援するアナリストです。\n"
    "ニュース記事を読み、以下のフィールドを必ず埋めてください：\n"
    "sentiment: positive（株価上昇要因）/ negative（株価下落要因）/ neutral（株価影響なし）\n"
    "summary: 日本語で1行、見出しのように簡潔な要約\n"
    "reason: sentiment をそう判断した根拠を日本語で2〜3文で説明する。"
    "中立と判断した場合はその理由も記載する。\n"
    "stocks: 関連する日本株銘柄のリスト（証券コード4桁と銘柄名）。"
    "銘柄が特定できない場合は空リストにする。"
)

_RSS_FEEDS_DEFAULT = "\n".join([
    "https://news.yahoo.co.jp/rss/topics/business.xml",
    "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.rss",
    "https://www.nhk.or.jp/rss/news/cat6.xml",
])

DEFAULTS: dict[str, str] = {
    "gemini_model": "gemini-2.5-flash",
    "gemini_system_prompt": _SYSTEM_PROMPT_DEFAULT,
    "signal_sentiments": "positive,negative",
    "signal_require_stocks": "true",
    "rss_feeds": _RSS_FEEDS_DEFAULT,
    "batch_interval_seconds": "7",
    "max_consecutive_failures": "3",
}


def get(key: str) -> str:
    try:
        result = execute("SELECT value FROM settings WHERE key = ?", (key,))
        if result.rows:
            return result.rows[0]["value"]
    except Exception:
        # settings テーブル未作成（マイグレーション前）や一時的なDBエラー時は
        # デフォルトにフォールバックする。原因調査のため警告は残す。
        logger.warning("settings.get(%s) failed; using default", key, exc_info=True)
    return DEFAULTS.get(key, "")


def set_value(key: str, value: str) -> None:
    execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
        (key, value),
    )


def get_all() -> dict[str, str]:
    try:
        result = execute("SELECT key, value FROM settings")
        db_data = {r["key"]: r["value"] for r in result.rows}
    except Exception:
        logger.warning("settings.get_all() failed; using defaults", exc_info=True)
        db_data = {}
    return {k: db_data.get(k, v) for k, v in DEFAULTS.items()}


def save_all(data: dict[str, str]) -> None:
    for key, value in data.items():
        if key in DEFAULTS:
            set_value(key, value)
