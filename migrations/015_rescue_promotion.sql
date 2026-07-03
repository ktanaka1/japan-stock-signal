-- 逆引き昇格（rescue）: impact閾値で落としたが市場が反応した（値上がり率ランキング上位）
-- シグナルを翌朝の配信に昇格させる。promoted_at/promote_reason は feedback snapshot が書く
-- （feedbackが既存テーブルへ書く唯一の例外。notified_at 等の配信状態には触れない）。
ALTER TABLE signals ADD COLUMN promoted_at TEXT;
ALTER TABLE signals ADD COLUMN promote_reason TEXT;
-- 捕捉率FBで「逆引き昇格で救済した」件数の別枠計上。capture_rate には含めない
-- （ランキングを見て昇格させたものをランキング捕捉と数えると指標が自己成就するため）。
ALTER TABLE coverage_runs ADD COLUMN rescued INTEGER NOT NULL DEFAULT 0;
