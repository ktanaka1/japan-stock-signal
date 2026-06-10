"""
通知役エージェント
  - 未通知のシグナルをリスト形式の1通にまとめてLINE配信する
  - シグナルゼロの日は何も送らない
  - 実行: python -m monitor.digest（平日 朝7:30 JST に起動される想定）
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from store.db import migrate
from store import signals
from store.recipients import get_all as get_recipients
from monitor.notifier import send_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

# LINEのテキスト上限は5,000字。余裕をみて分割する
_MAX_MESSAGE_CHARS = 4500


def run() -> None:
    migrate()
    items = signals.get_unnotified()
    if not items:
        logger.info("No signals to notify")
        return

    recipients = get_recipients()
    if not recipients:
        logger.info("No recipients registered, keeping %d signals unnotified", len(items))
        return

    messages = _build_messages(items)
    logger.info("Sending digest: %d signals in %d message(s)", len(items), len(messages))
    for text in messages:
        send_text(text, recipients)

    signals.mark_notified([s["id"] for s in items])
    logger.info("Done: %d signals notified", len(items))


def _build_messages(items: list) -> list:
    now = datetime.now(JST)
    header = f"📋 {now.month}/{now.day}({_WEEKDAYS[now.weekday()]}) 朝のシグナル {len(items)}件"

    blocks = [_format_item(s) for s in items]
    messages = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > _MAX_MESSAGE_CHARS:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    messages.append(current)
    return messages


def _format_item(signal: dict) -> str:
    icon = "✅" if signal["sentiment"] == "positive" else "❌"
    stocks_str = "、".join(f"{s['name']}({s['code']})" for s in signal["stocks"])
    return f"{icon} {stocks_str}\n　{signal['summary']}\n　{signal['url']}"


if __name__ == "__main__":
    run()
