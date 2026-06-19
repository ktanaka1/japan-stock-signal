-- インパクトスコア(1〜5): 方向(ポジ/ネガ)に加え「翌営業日の出来高爆発＝旬になる可能性」を評価する。
-- 5=完全なサプライズ / 3=通常 / 1=織り込み済み。digest は min_impact_for_notify 以上を最優先通知。
-- 追記のみ・後方互換。既存行は NULL（未評価）。ADD COLUMN の再実行重複は migrate() 側で無視する。
ALTER TABLE signals ADD COLUMN impact INTEGER;
ALTER TABLE article_analyses ADD COLUMN impact INTEGER;
