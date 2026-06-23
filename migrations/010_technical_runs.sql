-- テクニカル版 朝のシグナル（価格/出来高駆動の別系統スキャナ）の配信記録。
-- 既存テーブルには触れない追加のみ。将来「テクニカル版の的中率」をFB同様に測る土台にもなる。
CREATE TABLE IF NOT EXISTS technical_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT    NOT NULL DEFAULT (datetime('now')),  -- UTC
    target_date  TEXT    NOT NULL,        -- 動意の対象営業日(JST, YYYY-MM-DD)＝前営業日
    status       TEXT    NOT NULL,        -- 'ok' | 'no_picks' | 'no_recipients' | 'error'
    pick_count   INTEGER NOT NULL DEFAULT 0,
    line_ok      INTEGER NOT NULL DEFAULT 0,
    line_error   INTEGER NOT NULL DEFAULT 0,
    picks        TEXT,                     -- [{code,name,change_pct,surge,close}] のJSON
    error_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_technical_runs_date ON technical_runs (target_date DESC);
