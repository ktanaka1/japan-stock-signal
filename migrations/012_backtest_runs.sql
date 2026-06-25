-- 元本シミュレーション(バックテスト): 実配信シグナルを各取引ルールで約定したと仮定し、
-- 引け後cronで元本の日次推移を再計算してスナップショットを記録する。
-- 既存テーブルには触れない追加のみ・後方互換（外付けの計測器）。
CREATE TABLE IF NOT EXISTS backtest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT    NOT NULL DEFAULT (datetime('now')),  -- UTC
    as_of         TEXT    NOT NULL,        -- 計算時点の最新確定営業日(JST, YYYY-MM-DD)
    start_capital INTEGER NOT NULL,        -- 元本(円)
    snapshot      TEXT    NOT NULL          -- {axis, rules:[...], ledger, universe} のJSON
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_id ON backtest_runs (id DESC);
