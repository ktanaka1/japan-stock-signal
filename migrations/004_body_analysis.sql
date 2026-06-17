-- 開示の動的本文分析（XBRL優先＋PDFテキストのハイブリッド）の抽出結果を articles に追記する。
-- 追記のみ・後方互換。既存行は新カラム NULL / body_status='title_only' で従来挙動を維持。
-- ALTER TABLE ADD COLUMN は D1/SQLite 両対応。再実行時の重複カラムエラーは migrate() 側で無視する。
ALTER TABLE articles ADD COLUMN security_code     TEXT;                                   -- 開示由来の証券コード（新形式英数字あり 例 376A0）
ALTER TABLE articles ADD COLUMN xbrl_metrics      TEXT;                                   -- XBRL経路で抽出した正規化数値＋機械方向ラベルのJSON文字列
ALTER TABLE articles ADD COLUMN full_body         TEXT;                                   -- PDF経路で抽出した本文テキスト
ALTER TABLE articles ADD COLUMN correction_reason TEXT;                                   -- XBRL定性タグの修正理由
ALTER TABLE articles ADD COLUMN body_status       TEXT NOT NULL DEFAULT 'title_only';     -- 処理ルート: 'xbrl' | 'pdf_text' | 'title_only' | 'error'
