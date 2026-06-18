"""
監視役エージェント - LINE通知モジュール
分析結果を登録済み受信者にLINE Multicastで送信する。
"""
from __future__ import annotations

import logging
import os
from typing import List, NamedTuple

import httpx

from monitor.analyzer import AnalysisResult
from store.recipients import is_valid_user_id

logger = logging.getLogger(__name__)

_LINE_MULTICAST_URL = "https://api.line.me/v2/bot/message/multicast"
_MAX_RECIPIENTS_PER_CALL = 500


class SendResult(NamedTuple):
    """LINE送信結果。

    ok / error はチャンク数（後方互換: `ok, err, *_ = result` で従来の使い方も可）。
    error_details は失敗チャンクごとの "LINE {status}: {body}" 文字列リスト。
    digest側が error_detail に実エラー本文を保存するために使う。
    """

    ok: int
    error: int
    error_details: List[str] = []


def send(result: AnalysisResult, url: str, recipients: List[str]) -> SendResult:
    """分析結果をLINEで送信する。受信者がいない場合はスキップ。"""
    return send_text(_format_message(result, url), recipients)


def send_text(text: str, recipients: List[str]) -> SendResult:
    """テキストをLINE Multicastで送信する。受信者がいない場合はスキップ。

    Returns:
        SendResult(ok, error, error_details)
    """
    if not recipients:
        logger.info("No recipients registered, skipping notification")
        return SendResult(0, 0, [])

    if os.getenv("DRY_RUN", "").lower() == "true":
        logger.info("[DRY RUN] Would send LINE message to %d recipients:\n%s", len(recipients), text)
        return SendResult(1, 0, [])

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


def _multicast(text: str, recipients: List[str]) -> SendResult:
    """LINEにMulticast送信し SendResult を返す。

    不正な形式のuserId（例: 過去に混入した U_test_dummy）は送信前に除外する。
    LINEのmulticastは to[] に1件でも不正IDがあると 400 で全員未達になるため、
    1件の不正IDで全配信が落ちないようにする。全員除外で送信先ゼロなら (0,0) を返す。
    """
    valid = [r for r in recipients if is_valid_user_id(r)]
    rejected = len(recipients) - len(valid)
    if rejected:
        logger.warning(
            "Excluded %d invalid LINE userId(s) before multicast", rejected
        )
    if not valid:
        logger.warning("No valid recipients after filtering; skipping multicast")
        return SendResult(0, 0, [])

    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    ok_count = 0
    error_count = 0
    error_details: List[str] = []
    # LINEのmulticastは1回500件まで
    for i in range(0, len(valid), _MAX_RECIPIENTS_PER_CALL):
        chunk = valid[i : i + _MAX_RECIPIENTS_PER_CALL]
        body = {
            "to": chunk,
            "messages": [{"type": "text", "text": text}],
        }
        resp = httpx.post(_LINE_MULTICAST_URL, headers=headers, json=body, timeout=10)
        if resp.status_code != 200:
            logger.error("LINE multicast failed: %s %s", resp.status_code, resp.text)
            error_count += 1
            error_details.append(f"LINE {resp.status_code}: {resp.text}")
        else:
            logger.info("LINE multicast sent to %d recipients", len(chunk))
            ok_count += 1
    return SendResult(ok_count, error_count, error_details)
