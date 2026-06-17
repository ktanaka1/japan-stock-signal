# ADR-004: store層を D1 / ローカルSQLite の二重バックエンドにする

## 状況

本番は Cloudflare D1（REST API）を使う（ADR-001）が、開発・テスト時に
毎回 D1 へ接続するのは遅く、ネットワーク・認証情報が必要で不便。
D1 は SQLite 互換なので、ローカルでは素の SQLite で同じSQLを動かせる。

## 選択肢

- **環境変数で D1 / ローカルSQLite を自動切替**する単一の store 層
- 本番もローカルも常に D1 を使う
- ORM（SQLAlchemy等）で抽象化する

## 決定

`store/db.py` の `execute()` が、`CLOUDFLARE_ACCOUNT_ID` /
`CLOUDFLARE_D1_DATABASE_ID` / `CLOUDFLARE_API_TOKEN` の3つが揃っていれば
D1 REST API、なければローカルSQLite（`DB_PATH`）を使う。SQLは共通・プレースホルダは `?` で統一。

## 理由

- SQLite互換のため、SQLを1本書けば両方で動く（ORM不要で軽量）
- ローカルは高速・オフライン・認証不要でテストできる
- 本番切替は環境変数だけ（コード変更不要）

## 注意（重要な落とし穴）

- `store/db.py` が `load_dotenv()` で `.env` を読むため、**ローカル実行はデフォルトで本番D1に繋がる**。
  ローカルSQLiteでテストしたい場合は `CLOUDFLARE_*` を空にするだけでなく、.env の読み込みにも注意する
  （過去にテストデータが本番D1へ混入した事故あり）
- テスト時は `CLOUDFLARE_ACCOUNT_ID= CLOUDFLARE_D1_DATABASE_ID= CLOUDFLARE_API_TOKEN= DB_PATH=/tmp/xxx.db` を明示する
