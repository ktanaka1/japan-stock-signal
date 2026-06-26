"""large_holdings テーブルへの読み書き。大量保有報告書アラートの通知済み管理（docIDで冪等）。"""
from __future__ import annotations

from typing import List, Set

from .db import execute

# D1は1クエリ約100バインドまで。IN(?,...) は50件チャンクで投げる（本番D1の制限）。
_CHUNK = 50


def existing_ids(doc_ids: List[str]) -> Set[str]:
    """既に記録済み（通知済み）の docID 集合を返す。重複通知の防止に使う。"""
    found: Set[str] = set()
    for i in range(0, len(doc_ids), _CHUNK):
        chunk = [d for d in doc_ids[i:i + _CHUNK] if d]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = execute(
            f"SELECT doc_id FROM large_holdings WHERE doc_id IN ({placeholders})",
            tuple(chunk),
        ).rows
        found.update(r["doc_id"] for r in rows)
    return found


def record(h: dict) -> None:
    """1件の大量保有を通知済みとして記録する（docID重複は無視＝二重通知防止）。"""
    execute(
        """
        INSERT OR IGNORE INTO large_holdings
            (doc_id, sec_code, issuer_edinet_code, issuer_name, filer_name,
             doc_description, submit_at, notified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            h.get("doc_id"), h.get("sec_code"), h.get("issuer_edinet_code"),
            h.get("issuer_name"), h.get("filer_name"),
            h.get("doc_description"), h.get("submit_at"),
        ),
    )
