CREATE TABLE IF NOT EXISTS articles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    url        TEXT    NOT NULL UNIQUE,
    fetched_at TEXT    NOT NULL DEFAULT (datetime('now')),
    is_read    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_articles_is_read   ON articles (is_read);
CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles (fetched_at DESC);

-- LINE Botをフォローしたユーザーの受信者テーブル
CREATE TABLE IF NOT EXISTS recipients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id TEXT  NOT NULL UNIQUE,
    followed_at  TEXT  NOT NULL DEFAULT (datetime('now'))
);
