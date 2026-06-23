-- 開示由来の権威ある社名（yanoshin company_name）を articles に追記する。
-- 目的: 銘柄の同定をLLM生成任せにせず、TDnet開示会社は「権威コード＋社名」で確定し、
--       社名ハルシネーション（コードは正でも社名が別会社）を防ぐ。
-- 追記のみ・後方互換。既存行は NULL（従来どおりLLM由来の社名のまま）。
ALTER TABLE articles ADD COLUMN security_name TEXT;   -- 開示由来の権威ある社名（例: ＮＴＴ）
