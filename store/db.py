"""
ストア層のDBアクセス。バックエンドを環境変数で切り替える。
  - Cloudflare D1: CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_D1_DATABASE_ID / CLOUDFLARE_API_TOKEN が揃っている場合
  - ローカルSQLite: 上記が未設定の場合（開発用、DB_PATH に保存）
D1はSQLite互換なので、SQLは共通・プレースホルダは `?` で統一。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "data/stock_signal.db")

_MIGRATION_SQL = Path(__file__).parent.parent / "migrations" / "001_init.sql"
_D1_ENDPOINT = (
    "https://api.cloudflare.com/client/v4"
    "/accounts/{account_id}/d1/database/{database_id}/query"
)


@dataclass
class QueryResult:
    rows: list[dict] = field(default_factory=list)
    changes: int = 0
    last_row_id: Optional[int] = None


def execute(sql: str, params: tuple = ()) -> QueryResult:
    """SQLを1文実行する。SELECTは rows、書き込みは changes / last_row_id が入る。"""
    if _use_d1():
        return _d1_query(sql, list(params))
    return _sqlite_execute(sql, params)


def migrate() -> None:
    """マイグレーションSQLを実行してテーブルを初期化する。"""
    script = _MIGRATION_SQL.read_text()
    if _use_d1():
        _d1_query(script, [])
    else:
        conn = _sqlite_connection()
        try:
            with conn:
                conn.executescript(script)
        finally:
            conn.close()


def _use_d1() -> bool:
    return bool(
        os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        and os.environ.get("CLOUDFLARE_D1_DATABASE_ID")
        and os.environ.get("CLOUDFLARE_API_TOKEN")
    )


# --- Cloudflare D1 (REST API) ---


def _d1_query(sql: str, params: list) -> QueryResult:
    url = _D1_ENDPOINT.format(
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        database_id=os.environ["CLOUDFLARE_D1_DATABASE_ID"],
    )
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"},
        json={"sql": sql, "params": params},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 query failed: {data.get('errors')}")
    # 複数文を投げた場合は文ごとの結果が並ぶ。単文実行が前提なので最後の結果を使う。
    last = (data.get("result") or [{}])[-1]
    meta = last.get("meta") or {}
    return QueryResult(
        rows=last.get("results") or [],
        changes=meta.get("changes") or 0,
        last_row_id=meta.get("last_row_id"),
    )


# --- ローカルSQLite ---


def _sqlite_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_execute(sql: str, params: tuple) -> QueryResult:
    conn = _sqlite_connection()
    try:
        with conn:
            cur = conn.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]
            return QueryResult(
                rows=rows,
                changes=max(cur.rowcount, 0),
                last_row_id=cur.lastrowid,
            )
    finally:
        conn.close()
