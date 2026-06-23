-- 捕捉率フィードバック: 市場の動意(値上がり率ランキング) vs 自システムの捕捉を
-- 毎営業日記録する。既存テーブルには一切触れない追加のみ・後方互換。
CREATE TABLE IF NOT EXISTS coverage_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT    NOT NULL DEFAULT (datetime('now')),  -- UTC
    ranking_date  TEXT    NOT NULL,        -- 対象営業日(JST, YYYY-MM-DD)
    ranking_type  TEXT    NOT NULL,        -- 'up' | 'volume'
    top_n         INTEGER NOT NULL,        -- ランキング上位何件を対象にしたか
    universe      INTEGER NOT NULL DEFAULT 0,  -- 母集団(ETF・大型株を除いた対象数)
    captured      INTEGER NOT NULL DEFAULT 0,  -- delivered(配信済み)件数
    signaled      INTEGER NOT NULL DEFAULT 0,  -- signaled_not_delivered(拾ったが未配信)件数
    neutral       INTEGER NOT NULL DEFAULT 0,  -- analyzed_neutral(中立で落とした)件数
    not_collected INTEGER NOT NULL DEFAULT 0,  -- 収集網の外
    capture_rate  REAL,                        -- captured / universe
    detail        TEXT,                        -- 各銘柄 [{rank,code,name,market,pct,status}] のJSON
    proposal      TEXT                         -- 支配クラス→推奨レバーのサマリ
);
CREATE INDEX IF NOT EXISTS idx_coverage_runs_date ON coverage_runs (ranking_date DESC);
