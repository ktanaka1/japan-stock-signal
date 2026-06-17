# CLAUDE.md

ニュースを起点に日本株のシグナルを検出し、平日朝7:30にLINEでデイリーダイジェスト配信するシステム。デイトレード向け。詳細は `docs/` と `SPEC.md` 参照。

## 技術スタック

- Python 3.9
- collector / monitor / digest: GitHub Actions schedule（cronはUTC指定、JST-9h）
- webhook / 管理画面: FastAPI + Uvicorn（Render Free）、管理画面は Jinja2
- DB: Cloudflare D1（SQLite互換・REST API）／ローカルは SQLite。ORMなし・SQL直書き
- LLM: Google Gemini（既定 gemini-2.5-flash、管理画面のsettingsで変更可。上位モデルはAPI課金が前提）
- 通知: LINE Messaging API + SMTP（分析レポートメール）

## 重要なルール

### 実行・テスト
- **ローカル実行は `.env` 経由でデフォルト本番D1に繋がる**（`store/db.py` が `load_dotenv()`）。
  テスト/ローカル実行では必ず **`FORCE_LOCAL_DB=1`** を立てる（鉄壁ガード。`.env` に
  `CLOUDFLARE_*` があっても D1 を完全バイパスしローカルSQLiteへ強制する）:
  ```
  FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db .venv/bin/python ...
  ```
  （`CLOUDFLARE_*=` を空にする方法は import 時の `load_dotenv` 再注入で破られるため不可。
  過去にテストデータが本番D1へ混入した事故が複数回あり、`FORCE_LOCAL_DB` はその恒久対策）
- `DRY_RUN=true` でGemini呼び出しをモック化できる
- 動作確認は FastAPI の `TestClient`（lifespanでmigrateを発火させるため `with TestClient(app) as c:` を使う）

### Git
- コミットは論理単位で分割。コミットは依頼時のみ、pushは明示指示時のみ
- リモートは **SSH**（PATにworkflowスコープがなくHTTPSではworkflowファイルをpushできない）
- コミットメッセージ末尾に `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

### D1の癖
- D1は1クエリ=1 HTTPリクエスト。複数行INSERTはVALUESでバッチ化してラウンドトリップを減らす
- マイグレーションは `migrations/*.sql` をファイル名順に冪等実行

## ディレクトリ構成

```
collector/   収集役（RSS→articles）
monitor/     精査役 agent.py / 通知役 digest.py / analyzer / notifier / mailer
webhook/     FastAPI（LINE Webhook + /admin マウント）
admin/       管理画面（routes.py + templates/、Cookie認証）
store/       データアクセス層（db.py で D1/SQLite 切替）
migrations/  *.sql
docs/        ドキュメント（下記）
.github/workflows/  collector/monitor/digest の cron
```

## ドキュメントの棲み分け

- `docs/service-overview.md` — 要件・なぜ作るか（永続）
- `docs/architecture.md` — 技術構成（永続）
- `docs/architecture-decisions/` — 判断理由 ADR（永続）
- `SPEC.md` — システム全体の基本設計（永続。完成機能の要点はここへ統合）
- `docs/specs/_wips/` — **機能ごとの詳細設計（一時）。実装＆テスト完了で削除し、要点をSPEC.mdへ反映**

新機能の設計はまず `docs/specs/_wips/` に書く。実装・テストが済んだら削除する。

## セキュリティ

- `.env` や認証情報は絶対にコミットしない（`.gitignore` 済み）
- APIキー・トークンはコードにハードコードせず、環境変数から取得する
- シークレットは GitHub Secrets と Render 環境変数の両方に設定
  （`CLOUDFLARE_*`, `GEMINI_API_KEY`, `LINE_*`, `SMTP_*`, `ADMIN_TOKEN`）
- 管理画面は `ADMIN_TOKEN` を HttpOnly Cookie で認証（URLにトークンを載せない）

## 運用上の注意

- GitHub Actions の cron は大幅遅延・スキップが常態。だから digest は収集→分析→配信を直列実行する（ADR-002）
- Gemini無料枠のレート制限に注意（未読を溜め過ぎない）。バッチ間隔・連続失敗上限・抽出条件は管理画面から変更可能（settingsテーブル）
- シグナルゼロの日も「該当なし」を1通送る（無通知＝障害と区別）
