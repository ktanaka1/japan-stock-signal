-- 精査役が検出したシグナル（銘柄が特定できたポジ/ネガ記事）。
-- 通知役が朝のダイジェストで未通知分をまとめて配信し、notified_at を打つ。
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  INTEGER NOT NULL UNIQUE,
    sentiment   TEXT    NOT NULL,             -- positive | negative
    summary     TEXT    NOT NULL,
    stocks      TEXT    NOT NULL,             -- JSON: [{"name": "...", "code": "..."}]
    url         TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    notified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_notified ON signals (notified_at);
