"""
監視役エージェント - LINE通知モジュール
分析結果を登録済み受信者にLINE Multicastで送信する。
"""
from __future__ import annotations

import logging
import os
from typing import List

import httpx

from monitor.analyzer import AnalysisResult

logger = logging.getLogger(__name__)

_LINE_MULTICAST_URL = "https://api.line.me/v2/bot/message/multicast"
_MAX_RECIPIENTS_PER_CALL = 500


def send(result: AnalysisResult, url: str, recipients: List[str]) -> tuple:
    """分析結果をLINEで送信する。受信者がいない場合はスキップ。(ok, error) を返す。"""
    return send_text(_format_message(result, url), recipients)


def send_text(text: str, recipients: List[str]) -> tuple:
    """テキストをLINE Multicastで送信する。受信者がいない場合はスキップ。

    Returns:
        (ok_count, error_count): 成功チャンク数と失敗チャンク数のタプル
    """
    if not recipients:
        logger.info("No recipients registered, skipping notification")
        return (0, 0)

    if os.getenv("DRY_RUN", "").lower() == "true":
        logger.info("[DRY RUN] Would send LINE message to %d recipients:\n%s", len(recipients), text)
        return (1, 0)

    return _multicast(text, recipients)


def _format_message(result: AnalysisResult, url: str) -> str:
    if result.sentiment == "positive":
        sentiment_line = "✅ ポジティブ"
    elif result.sentiment == "negative":
        sentiment_line = "❌ ネガティブ"
    else:
        sentiment_line = "⬜ 中立"

    if result.stocks:
        stocks_str = "、".join(
            f"{s['name']}({s['code']})" for s in result.stocks
        )
        stocks_line = f"🎯 関連銘柄：{stocks_str}"
    else:
        stocks_line = "🎯 関連銘柄：なし"

    return f"📰 {result.summary}\n{sentiment_line}\n{stocks_line}\n🔗 {url}"


def _multicast(text: str, recipients: List[str]) -> tuple:
    """LINEにMulticast送信し (ok_count, error_count) を返す。"""
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    ok_count = 0
    error_count = 0
    # LINEのmulticastは1回500件まで
    for i in range(0, len(recipients), _MAX_RECIPIENTS_PER_CALL):
        chunk = recipients[i : i + _MAX_RECIPIENTS_PER_CALL]
        body = {
            "to": chunk,
            "messages": [{"type": "text", "text": text}],
        }
        resp = httpx.post(_LINE_MULTICAST_URL, headers=headers, json=body, timeout=10)
        if resp.status_code != 200:
            logger.error("LINE multicast failed: %s %s", resp.status_code, resp.text)
            error_count += 1
        else:
            logger.info("LINE multicast sent to %d recipients", len(chunk))
            ok_count += 1
    return (ok_count, error_count)
