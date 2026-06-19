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
    "銘柄が特定できない場合は空リストにする。\n"
    "\n"
    "【数値が与えられた開示の判定ルール（厳守）】\n"
    "業績予想の修正・決算・配当の開示で、数値（XBRL確定値 or 本文）が与えられている場合、"
    "安易に neutral にしてはならない。\n"
    "今回値 > 前回予想（上方修正・増配・増益）= positive、"
    "今回値 < 前回予想（下方修正・減配・減益）= negative。\n"
    "数値や機械方向ラベルがある場合はそれを根拠に方向を確定し、"
    "reason に前回予想比/前期比を必ず明記せよ。\n"
    "neutral は『株価に影響しない』と確信できる時のみ。"
    "数値があるのに情報不足を理由に neutral にしてはならない。\n"
    "\n"
    "【最重要・絶対厳守】\n"
    "渡されたデータが『XBRL抽出数値（確定値）』である場合、それは100%正確な事実である。"
    "その数値の比較結果（上昇/下落/据え置き）のみを絶対の根拠として方向を判定せよ。"
    "XBRL数値が与えられているときに、テキスト（修正理由など）から別の要因を推測して"
    "判定を覆してはならない。\n"
    "本文テキスト（PDF抽出）のみが与えられた場合は、本文中の前回予想と今回予想"
    "（または前期実績）を見つけ、数値を比較して方向を判定せよ。"
)

_RSS_FEEDS_DEFAULT = "\n".join([
    "https://news.yahoo.co.jp/rss/topics/business.xml",
    "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.rss",
    "https://www.nhk.or.jp/rss/news/cat6.xml",
])

# 本文取得の一次フィルタ語彙（タイトル部分一致・rss_feedsと同パターンで改行区切り）。
# A: 数値系（業績・配当）= 本文取得対象。B: 非数値系（提携・分割等）= 取得しない。
_BODY_FETCH_TRIGGERS_NUMERIC_DEFAULT = "\n".join([
    "業績予想", "通期業績", "決算短信", "決算", "四半期",
    "業績修正", "上方修正", "下方修正", "配当予想", "増配", "減配",
    "剰余金の処分", "配当",
])
_BODY_FETCH_TRIGGERS_CATALYST_DEFAULT = "\n".join([
    "株式分割", "株式併合", "新株予約権", "第三者割当", "自己株式",
    "業務提携", "資本提携", "M&A", "合併", "TOB", "公開買付",
    "子会社化", "株式交換", "主要株主の異動", "減損", "特別損失",
])

DEFAULTS: dict[str, str] = {
    "gemini_model": "gemini-2.5-flash",
    "gemini_system_prompt": _SYSTEM_PROMPT_DEFAULT,
    "signal_sentiments": "positive,negative",
    "signal_require_stocks": "true",
    "rss_feeds": _RSS_FEEDS_DEFAULT,
    "batch_interval_seconds": "7",
    "max_consecutive_failures": "3",
    "max_articles_per_run": "200",
    "body_max_chars": "4000",
    "body_fetch_triggers_numeric": _BODY_FETCH_TRIGGERS_NUMERIC_DEFAULT,
    "body_fetch_triggers_catalyst": _BODY_FETCH_TRIGGERS_CATALYST_DEFAULT,
    # インパクトスコア(1〜5) がこの値以上のシグナルのみ LINE 配信する（旬の選別）。
    # 0 にすると全件配信（従来挙動）。既定は 4（サプライズ度4〜5を最優先）。
    "min_impact_for_notify": "4",
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
