import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "data/stock_signal.db")


def get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate() -> None:
    """マイグレーションSQLを実行してテーブルを初期化する。"""
    sql_path = Path(__file__).parent.parent / "migrations" / "001_init.sql"
    conn = get_connection()
    with conn:
        conn.executescript(sql_path.read_text())
