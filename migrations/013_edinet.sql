-- EDINET 大量保有報告書アラート（需給シグナル・別レーン）。既存テーブルには触れない追加のみ。
-- large_holdings: 通知済み管理（docIDで冪等）。sec_code は issuerEdinetCode をコードリストで4桁解決した値。
CREATE TABLE IF NOT EXISTS large_holdings (
    doc_id             TEXT PRIMARY KEY,    -- EDINET docID
    sec_code           TEXT,                -- 対象銘柄4桁
    issuer_edinet_code TEXT,                -- 解決元のEDINETコード（監査用）
    issuer_name        TEXT,                -- 対象銘柄名（コードリスト由来）
    filer_name         TEXT,                -- 提出者(保有者)
    doc_description     TEXT,               -- 大量保有報告書(新規) | 変更報告書 等
    submit_at          TEXT,
    holding_ratio      REAL,                -- Phase2用。Phase1はNULL
    notified_at        TEXT,
    created_at         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_large_holdings_code ON large_holdings (sec_code);

-- edinet_runs: technical_runs と同型の実行記録
CREATE TABLE IF NOT EXISTS edinet_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    target_date  TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    pick_count   INTEGER NOT NULL DEFAULT 0,
    line_ok      INTEGER NOT NULL DEFAULT 0,
    line_error   INTEGER NOT NULL DEFAULT 0,
    picks        TEXT,
    error_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_edinet_runs_date ON edinet_runs (target_date DESC);
