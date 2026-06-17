# ADR-001: データベースに Cloudflare D1 を採用

## 状況

ニュース記事・シグナル・受信者を保存する永続ストアが必要。完全無料で運用したい。
定期実行は GitHub Actions（サーバーレス）から行うため、常設DBサーバーを持ちたくない。

## 選択肢

- **Cloudflare D1**: SQLite互換、無料枠あり、REST APIでHTTP越しにクエリ可能
- Supabase / Neon (PostgreSQL): 高機能だが、接続管理が必要・無料枠の制約
- ローカルSQLiteファイル: Actionsの実行は使い捨て環境なので永続化できない

## 決定

**Cloudflare D1** を採用。

## 理由

- 無料枠（ストレージ5GB・読み書きの日次上限）で本用途には十分
- **REST API で叩ける**ため、GitHub Actions の使い捨てコンテナからHTTPだけで読み書きできる（DBドライバ・コネクション管理が不要）
- SQLite互換なので、ローカル開発は素のSQLiteで再現できる（→ ADR-004）
- マイグレーションは `migrations/*.sql` をファイル名順に流すだけ（冪等）

## 補足

- store層は `CLOUDFLARE_*` 環境変数の有無でD1/ローカルSQLiteを切り替える（ADR-004）
- D1は1クエリ=1 HTTPリクエストなので、collectorのINSERTは複数行VALUESでバッチ化しラウンドトリップを削減している
