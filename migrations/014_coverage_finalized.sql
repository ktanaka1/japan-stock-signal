-- 捕捉率FBの「2フェーズ化」: D夕方のスナップショット(暫定)と、D+1朝の配信完了後の確定を
-- 区別するフラグ。snapshot は finalized=0 で記録し、finalize で配信状況を再判定して finalized=1 にする。
-- 追記のみ・後方互換。既存行は 0（暫定）扱い。ADD COLUMN の再実行重複は migrate() 側で無視。
ALTER TABLE coverage_runs ADD COLUMN finalized INTEGER NOT NULL DEFAULT 0;
