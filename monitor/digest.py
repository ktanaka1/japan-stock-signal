"""
通知役エージェント
  - 未通知のシグナルをリスト形式の1通にまとめてLINE配信する
  - シグナルゼロの日もその旨を1通送る（無通知と障害を区別できるようにする）
  - 実行: python -m monitor.digest（平日 朝7:30 JST に起動される想定）
"""
from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from store.db import migrate
from store import signals
from store import digest_runs
from store import settings as cfg
from store.recipients import get_all as get_recipients
from monitor.notifier import send_text
from monitor import mailer
from monitor import quotes

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

    now_jst = datetime.now(JST)
    run_at_jst = now_jst.strftime("%Y-%m-%d %H:%M JST")

    # 集計変数
    total_ok: int = 0
    total_error: int = 0
    line_error_details: List[str] = []  # LINEの実エラー本文（"LINE 400: {body}"）を集約
    sent_attempted: bool = False        # 1通でも送信を試みたか（不正ID全除外の検知用）
    error_detail: Optional[str] = None
    notified_ids: List[int] = []
    status: str = "ok"

    try:
        all_unnotified = signals.get_unnotified()
        recipients = get_recipients()

        # インパクトスコアが閾値未満のシグナルは通知しない（旬の選別・柱3）。
        # min_impact=0 で全件通知（従来挙動）。閾値未満は notified_at を打たず未配信のまま残す。
        try:
            min_impact = int(cfg.get("min_impact_for_notify"))
        except (TypeError, ValueError):
            min_impact = 0
        items = [s for s in all_unnotified if (s.get("impact") or 0) >= min_impact]
        if len(items) != len(all_unnotified):
            logger.info("Impact filter (>=%d): %d/%d signals eligible",
                        min_impact, len(items), len(all_unnotified))

        if not recipients:
            logger.info("No recipients registered, keeping %d signals unnotified", len(items))
            status = "no_recipients"
            # no_recipients は正常終了扱い。記録だけして終わる（finallyで記録する）
        elif not items:
            logger.info("No eligible signals; sending zero-signal notice")
            sent_attempted = True
            res = send_text(_zero_signal_message(), recipients)
            total_ok += res.ok
            total_error += res.error
            line_error_details.extend(res.error_details)
        else:
            messages = _build_messages(items)
            logger.info("Sending digest: %d signals in %d message(s)", len(items), len(messages))
            sent_attempted = True
            for text in messages:
                res = send_text(text, recipients)
                total_ok += res.ok
                total_error += res.error
                line_error_details.extend(res.error_details)

            if total_error == 0 and total_ok > 0:
                # 全送信成功時のみ通知済みにする（配信した eligible のみ）
                notified_ids = [s["id"] for s in items]
                signals.mark_notified(notified_ids)
                logger.info("Done: %d signals notified", len(items))
            else:
                logger.warning(
                    "LINE send not fully successful: ok=%d error=%d; signals NOT marked notified",
                    total_ok, total_error,
                )

        # ステータス判定（no_recipients は上で設定済みなのでスキップ）
        if status != "no_recipients":
            # 受信者はいたが全員が不正IDで除外され送信先ゼロになったケース。
            # send_text/_multicast が (0,0) を返すため no_recipients 相当に扱う。
            if sent_attempted and total_ok == 0 and total_error == 0:
                status = "no_recipients"
                logger.warning("All recipients were invalid; treated as no_recipients")
            elif total_error > 0 and total_ok > 0:
                status = "partial"
                error_detail = _build_line_error_detail(
                    f"LINE multicast: {total_ok} チャンク成功 / {total_error} チャンク失敗",
                    line_error_details,
                )
            elif total_error > 0:
                status = "error"
                error_detail = _build_line_error_detail(
                    f"LINE multicast: 全 {total_error} チャンクが失敗",
                    line_error_details,
                )

    except Exception:
        status = "error"
        error_detail = traceback.format_exc()
        logger.exception("digest.run() failed with unexpected error")

    finally:
        # 成否にかかわらず常に記録する
        digest_runs.record(
            status=status,
            signal_count=len(notified_ids) if notified_ids else 0,
            line_ok=total_ok,
            line_error=total_error,
            error_detail=error_detail,
            notified_ids=notified_ids if notified_ids else None,
        )

    # LINE送信失敗時はメールアラート
    if error_detail:
        mailer.send_digest_alert(error_detail=error_detail, run_at_jst=run_at_jst)


def _build_line_error_detail(summary: str, line_error_details: List[str]) -> str:
    """件数サマリにLINEの実エラー本文（HTTPステータス＋レスポンス本文）を付与する。

    Renderログを見なくても digest_runs.error_detail だけで原因が分かるようにする。
    """
    if not line_error_details:
        return summary
    # 同一エラーが複数チャンクで重複しがちなので一意化（順序は保持）
    seen = []
    for d in line_error_details:
        if d not in seen:
            seen.append(d)
    return summary + "\n" + "\n".join(seen)


def _header(count: int) -> str:
    now = datetime.now(JST)
    return f"📋 {now.month}/{now.day}({_WEEKDAYS[now.weekday()]}) 朝のシグナル {count}件"


def _zero_signal_message() -> str:
    return f"{_header(0)}\n\n昨日からのニュースに該当する銘柄はありませんでした。"


def _build_messages(items: list) -> list:
    header = _header(len(items))

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
    impact = signal.get("impact")
    fire = f" 🔥{impact}" if impact else ""
    lines = []
    for i, s in enumerate(signal["stocks"]):
        # インパクトスコアは先頭銘柄の行末にだけ付ける（シグナル単位の属性のため）
        suffix = fire if i == 0 else ""
        lines.append(f"{icon} {s['name']}({s['code']}){suffix}")
        price = quotes.format_price(s)
        if price:
            lines.append(f"　📊 {price}")
    lines.append(f"　{signal['summary']}")
    lines.append(f"　{signal['url']}")
    return "\n".join(lines)


if __name__ == "__main__":
    run()
