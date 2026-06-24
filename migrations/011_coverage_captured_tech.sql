-- 捕捉率FBの「2チャンネル合算」: ニュース版の配信(captured)に加え、テクニカル版
-- (technical_runs)で配信した銘柄数を別カラムで記録する。capture_rate は両者の合算/母集団。
-- 追記のみ・後方互換。既存行は NULL（テクニカル合算前の記録）。ADD COLUMN の再実行重複は migrate() 側で無視。
ALTER TABLE coverage_runs ADD COLUMN captured_tech INTEGER NOT NULL DEFAULT 0;
