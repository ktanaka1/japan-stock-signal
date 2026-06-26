"""EDINET 大量保有報告書アラート役（需給シグナル・別レーン）。

documents.json(v2) から docTypeCode=350（大量保有ファミリ）を取得し、issuerEdinetCode を
コードリストで4桁証券コードに解決して、未通知の新規だけを中立アラートとして LINE 配信する。
方向（買い/売り）はメタデータでは判定できないため v1 は中立（新規/変更タグのみ）。
既存テーブルは触れない（large_holdings / edinet_runs のみ）。キー未設定/無効化時は no-op。
実行: python -m edinet.agent（平日 寄り前 JST 7時台 想定）
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import List

import httpx

from store.db import migrate
from store import settings as cfg
from store import large_holdings, edinet_runs
from store.recipients import get_all as get_recipients
from monitor.notifier import send_text
from edinet import codelist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
_DOCS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
_VIEW_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx"
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_MESSAGE_CHARS = 4500

# 大量保有ファミリの docTypeCode。docDescription で新規/変更を区別する。
_DOC_TYPE = "350"
_NEW_DESC = "大量保有報告書"   # これ＝新規（初回5%超）。それ以外（変更報告書 等）は変更。


def _fetch_documents(date_str: str, key: str) -> list:
    """指定日の提出書類一覧(results)を返す。失敗時は空リスト。"""
    try:
        r = httpx.get(
            _DOCS_URL,
            params={"date": date_str, "type": "2"},
            headers={"Ocp-Apim-Subscription-Key": key},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning("EDINET documents %s: HTTP %s", date_str, r.status_code)
            return []
        return r.json().get("results") or []
    except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
        logger.warning("EDINET documents %s: failed (%s)", date_str, exc)
        return []


def _denylist() -> List[str]:
    raw = cfg.get("edinet_filer_denylist") or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _collect(docs: dict, code_map: dict, new_only: bool, denylist: List[str]) -> List[dict]:
    """書類群から配信候補（4桁解決済み・denylist除外済み）を抽出する。"""
    picks = []
    for doc in docs.values():
        if str(doc.get("docTypeCode")) != _DOC_TYPE:
            continue
        desc = (doc.get("docDescription") or "").strip()
        is_new = desc == _NEW_DESC
        if new_only and not is_new:
            continue
        iec = (doc.get("issuerEdinetCode") or "").strip()
        if not iec or iec not in code_map:
            continue
        sec4, iname = code_map[iec]
        if not sec4:                       # REIT/投資法人/非上場（証券コード空）はスキップ
            continue
        filer = (doc.get("filerName") or "").strip()
        if any(ng in filer for ng in denylist):
            logger.info("EDINET skip (denylist): %s / %s", filer, iname)
            continue
        picks.append({
            "doc_id": doc.get("docID"),
            "sec_code": sec4,
            "issuer_edinet_code": iec,
            "issuer_name": iname,
            "filer_name": filer,
            "doc_description": desc,
            "submit_at": doc.get("submitDateTime"),
            "is_new": is_new,
        })
    return picks


def _format_pick(p: dict) -> str:
    tag = "新規" if p.get("is_new") else "変更"
    lines = [f"📨 大量保有[{tag}] {p['issuer_name']}({p['sec_code']})"]
    if p.get("filer_name"):
        lines.append(f"　提出: {p['filer_name']}")
    lines.append(f"　https://finance.yahoo.co.jp/quote/{p['sec_code']}.T")
    return "\n".join(lines)


def _header(count: int) -> str:
    now = datetime.now(JST)
    return f"📨 {now.month}/{now.day} 大量保有報告 {count}件（需給・方向は要確認）"


def _build_messages(picks: List[dict]) -> List[str]:
    blocks = [_format_pick(p) for p in picks]
    messages = []
    current = _header(len(picks))
    for block in blocks:
        if len(current) + len(block) + 2 > _MAX_MESSAGE_CHARS:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    messages.append(current)
    return messages


def run() -> None:
    migrate()
    now = datetime.now(JST)
    target_date = now.strftime("%Y-%m-%d")

    if cfg.get("edinet_enabled").lower() != "true":
        logger.info("EDINET disabled (edinet_enabled != true); skip")
        return
    key = os.getenv("EDINET_API_KEY", "").strip()
    if not key:
        logger.warning("EDINET_API_KEY unset; skip (no-op)")
        return

    status = "ok"
    total_ok = total_error = 0
    error_detail = None
    fresh: List[dict] = []

    try:
        code_map = codelist.fetch_map()
        if not code_map:
            logger.warning("EDINET codelist empty; skip this run")
            edinet_runs.record(target_date=target_date, status="no_codelist")
            return

        new_only = cfg.get("edinet_new_only").lower() == "true"
        denylist = _denylist()

        # 当日＋前日（遅れ提出の取りこぼし防止。重複は docID で吸収）
        docs = {}
        for d in (now, now - timedelta(days=1)):
            for doc in _fetch_documents(d.strftime("%Y-%m-%d"), key):
                did = doc.get("docID")
                if did:
                    docs[did] = doc

        candidates = _collect(docs, code_map, new_only, denylist)
        logger.info("EDINET candidates: %d (new_only=%s, docs=%d)", len(candidates), new_only, len(docs))

        # docID 未通知のものだけを新規配信対象に
        seen = large_holdings.existing_ids([c["doc_id"] for c in candidates])
        fresh = [c for c in candidates if c["doc_id"] not in seen]

        recipients = get_recipients()
        if not fresh:
            status = "no_picks"
        elif not recipients:
            status = "no_recipients"
        else:
            for text in _build_messages(fresh):
                res = send_text(text, recipients)
                total_ok += res.ok
                total_error += res.error
            if total_error > 0 and total_ok == 0:
                status = "error"
            # 送信成否に関わらず記録（再通知防止）。
            for c in fresh:
                large_holdings.record(c)
    except Exception:
        status = "error"
        error_detail = traceback.format_exc()
        logger.exception("edinet.run() failed")

    edinet_runs.record(
        target_date=target_date,
        status=status,
        pick_count=len(fresh),
        line_ok=total_ok,
        line_error=total_error,
        picks=fresh,
        error_detail=error_detail,
    )
    logger.info("EDINET done: status=%s picks=%d ok=%d err=%d",
                status, len(fresh), total_ok, total_error)


if __name__ == "__main__":
    run()
