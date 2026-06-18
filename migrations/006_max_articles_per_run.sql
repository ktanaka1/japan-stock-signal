-- 1回の monitor 実行で処理する未読の上限（既定200）。
-- 逐次保存と組み合わせ、大量バックログを複数回の実行で漸減させる。
-- INSERT OR IGNORE: 既に管理画面から設定済みの値は上書きしない（冪等）。
INSERT OR IGNORE INTO settings (key, value, updated_at)
VALUES ('max_articles_per_run', '200', datetime('now'));
