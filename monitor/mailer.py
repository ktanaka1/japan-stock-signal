"""
分析レポートメール送信モジュール
  - 環境変数 REPORT_EMAIL が未設定のときは何もしない（安定稼働後に無効化しやすい設計）
  - SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD で接続先を指定
  - ポート 465 の場合 SMTP_SSL=true（デフォルト）、587 の場合 SMTP_SSL=false に設定する
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import List, Optional, Tuple

from monitor.analyzer import AnalysisResult

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_SENTIMENT_LABEL = {
    "positive": "✅ ポジティブ",
    "negative": "❌ ネガティブ",
    "neutral":  "⬜ 中立",
}


def send_digest_alert(error_detail: str, run_at_jst: str) -> None:
    """LINE配信エラーをメールで通知する。REPORT_EMAIL 未設定の場合はスキップ。

    Args:
        error_detail: エラーの詳細テキスト
        run_at_jst:   配信実行日時（JST）の文字列表現
    """
    to_addr = os.getenv("REPORT_EMAIL", "").strip()
    if not to_addr:
        return

    subject = f"[株シグナル] LINE配信エラー {run_at_jst}"
    body = "\n".join([
        f"LINE配信中にエラーが発生しました。",
        f"実行日時: {run_at_jst}",
        "",
        "エラー詳細:",
        error_detail,
    ])
    _send(to_addr, subject, body)


def send_analysis_report(
    results: List[Tuple[object, Optional[AnalysisResult]]],
    signal_ids: set,
) -> None:
    """分析結果をメールで送信する。REPORT_EMAIL 未設定の場合はスキップ。

    Args:
        results: analyze_batch の戻り値 (article, result) のリスト
        signal_ids: シグナルとして登録された article_id の集合
    """
    to_addr = os.getenv("REPORT_EMAIL", "").strip()
    if not to_addr:
        return

    subject, body = _build_report(results, signal_ids)
    _send(to_addr, subject, body)


def _build_report(
    results: List[Tuple[object, Optional[AnalysisResult]]],
    signal_ids: set,
) -> Tuple[str, str]:
    now = datetime.now(JST)
    total = len(results)
    signals = [(a, r) for a, r in results if r and a.id in signal_ids]
    skipped = [(a, r) for a, r in results if r and a.id not in signal_ids]
    failed  = [(a, r) for a, r in results if r is None]

    subject = (
        f"[株シグナル分析] {now.month}/{now.day} {now.hour:02d}:{now.minute:02d} "
        f"— {total}件分析 / シグナル{len(signals)}件"
    )

    lines: List[str] = [
        f"分析日時: {now.strftime('%Y-%m-%d %H:%M')} JST",
        f"分析記事: {total}件（シグナル: {len(signals)}件 / スキップ: {len(skipped)}件 / 失敗: {len(failed)}件）",
        "",
    ]

    if signals:
        lines += ["=" * 50, f"■ シグナル {len(signals)}件", "=" * 50, ""]
        for i, (article, result) in enumerate(signals, 1):
            lines += _format_entry(i, article, result)

    if skipped:
        lines += ["=" * 50, f"■ スキップ {len(skipped)}件（中立・銘柄未特定）", "=" * 50, ""]
        for i, (article, result) in enumerate(skipped, 1):
            lines += _format_entry(i, article, result)

    if failed:
        lines += ["=" * 50, f"■ 分析失敗 {len(failed)}件", "=" * 50, ""]
        for i, (article, _) in enumerate(failed, 1):
            lines.append(f"[{i}] {article.title}")
            lines.append(f"    URL: {article.url}")
            lines.append("")

    return subject, "\n".join(lines)


def _format_entry(idx: int, article: object, result: AnalysisResult) -> List[str]:
    label = _SENTIMENT_LABEL.get(result.sentiment, result.sentiment)
    stocks_str = (
        "、".join(f"{s['name']}({s['code']})" for s in result.stocks)
        if result.stocks else "なし"
    )
    return [
        f"[{idx}] {label}",
        f"銘柄: {stocks_str}",
        f"要約: {result.summary}",
        f"根拠: {result.reason}",
        f"タイトル: {article.title}",
        f"本文抜粋: {article.body[:200].strip()}",
        f"URL: {article.url}",
        "",
    ]


def _send(to_addr: str, subject: str, body: str) -> None:
    host     = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port     = int(os.getenv("SMTP_PORT", "465"))
    user     = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    use_ssl  = os.getenv("SMTP_SSL", "true").lower() != "false"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = to_addr

    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx) as smtp:
                smtp.login(user, password)
                smtp.sendmail(user, [to_addr], msg.as_bytes())
        else:
            with smtplib.SMTP(host, port) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, password)
                smtp.sendmail(user, [to_addr], msg.as_bytes())
        logger.info("Analysis report sent to %s", to_addr)
    except Exception:
        logger.exception("Failed to send analysis report email")
