CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS article_analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id    INTEGER NOT NULL UNIQUE,
    sentiment     TEXT    NOT NULL,
    summary       TEXT    NOT NULL,
    reason        TEXT    NOT NULL DEFAULT '',
    stocks        TEXT    NOT NULL DEFAULT '[]',
    became_signal INTEGER NOT NULL DEFAULT 0,
    analyzed_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_analyses_article ON article_analyses (article_id);
CREATE INDEX IF NOT EXISTS idx_analyses_date    ON article_analyses (analyzed_at DESC);
