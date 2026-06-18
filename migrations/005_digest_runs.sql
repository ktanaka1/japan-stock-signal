CREATE TABLE IF NOT EXISTS digest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    status       TEXT    NOT NULL,
    signal_count INTEGER NOT NULL DEFAULT 0,
    line_ok      INTEGER NOT NULL DEFAULT 0,
    line_error   INTEGER NOT NULL DEFAULT 0,
    error_detail TEXT,
    notified_ids TEXT
);

CREATE INDEX IF NOT EXISTS idx_digest_runs_run_at ON digest_runs (run_at DESC);
