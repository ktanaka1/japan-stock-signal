# Japan Stock Signal

日本株の関連ニュースをRSSで収集し、LLMで分析してLINEに通知するシステム。

## 機能

- 毎日6:00〜23:00にRSSフィードを1時間ごとに収集（週末も稼働）
- Google Gemini でニュースをまとめて分析し、銘柄が特定できたポジ/ネガ記事だけをシグナルとして蓄積
- **平日朝に、前回配信以降のシグナルをリスト形式の1通でLINE配信**（デイリーダイジェスト）
  - 朝のジョブは **収集→分析→配信を直列実行** するため、配信時点の最新ニュースまで含まれる
  - 7:30(JST)起動設定だが、GitHub Actionsのcron遅延により実際の配信は7:30〜9:00ごろ
- フォロー/アンフォローで受信者を自己管理

## 通知フォーマット

```
📋 6/12(金) 朝のシグナル 8件

✅ トヨタ自動車(7203)
　通期営業利益を上方修正
　https://...

❌ ソニーG(6758)
　主力事業で減損計上へ
　https://...
```

---

## セットアップ

### 1. 依存パッケージのインストール

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境変数の設定

```bash
cp .env.example .env
```

`.env` を編集して以下の値を設定します。

| 変数名 | 取得元 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) | Gemini API キー（無料枠あり） |
| `LINE_CHANNEL_ACCESS_TOKEN` | [LINE Developers](https://developers.line.biz/) → チャネル → Messaging API タブ → チャネルアクセストークン（長期）→「発行」 | LINE送信に使うトークン |
| `LINE_CHANNEL_SECRET` | [LINE Developers](https://developers.line.biz/) → チャネル → チャネル基本設定タブ → チャネルシークレット | Webhookの署名検証に使う |
| `DB_PATH` | 変更不要 | ローカルSQLiteファイルのパス（デフォルト: `data/stock_signal.db`） |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflareダッシュボード | 本番DB（D1）用。3つ揃うとD1を使用 |
| `CLOUDFLARE_D1_DATABASE_ID` | D1のデータベース詳細ページ | 〃 |
| `CLOUDFLARE_API_TOKEN` | My Profile → API Tokens | 〃（権限: Account / D1 / Edit） |
| `JOB_TRIGGER_TOKEN` | 任意の文字列 | `/jobs/*` エンドポイントの認証（未設定なら無効） |

DBは環境変数で自動的に切り替わります。`CLOUDFLARE_*` 3つが揃っていれば **Cloudflare D1**（本番）、なければ **ローカルSQLite**（開発）。D1はSQLite互換なのでSQLは共通です。

---

## 実行

### 調査役（RSS収集）

```bash
python -m collector.agent
```

### 精査役（LLM分析 → シグナル蓄積）

```bash
python -m monitor.agent
```

### 通知役（朝のダイジェスト配信）

```bash
python -m monitor.digest
```

動作確認（APIキーなし・LINE送信なし）:

```bash
DRY_RUN=true python -m monitor.agent
DRY_RUN=true python -m monitor.digest
```

### Webhookサーバー（LINE フォロー/アンフォロー受信）

```bash
uvicorn webhook.app:app --host 0.0.0.0 --port 8000
```

---

## デプロイ（無料構成）

| 役割 | サービス | 備考 |
|---|---|---|
| DB | Cloudflare D1 | 無料枠: 5GB / 読み取り500万行・書き込み10万行/日 |
| Webhookサーバー | Render (Free Web Service) | `render.yaml` で構築 |
| collector / monitor | GitHub Actions (schedule) | `.github/workflows/` 参照 |

### 1. Cloudflare D1 の作成

1. [Cloudflare](https://dash.cloudflare.com/) にサインアップ（無料、カード不要）
2. ダッシュボード → **Storage & Databases → D1** → 「Create database」（名前例: `stock-signal`）
3. 作成したDBの **Console** タブで `migrations/001_init.sql` の中身を貼り付けて実行
4. 以下の3つを控える:
   - **Account ID**: ダッシュボードのURL `dash.cloudflare.com/<これ>` または概要ページ右側
   - **Database ID**: D1のデータベース詳細ページに表示
   - **API Token**: My Profile → API Tokens → Create Token → Custom Token で
     権限 `Account / D1 / Edit` を付与して発行

### 2. GitHub Actions（収集・分析の定期実行）

リポジトリの **Settings → Secrets and variables → Actions** に以下を登録:

`CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_D1_DATABASE_ID` / `CLOUDFLARE_API_TOKEN` / `GEMINI_API_KEY` / `LINE_CHANNEL_ACCESS_TOKEN`

- スケジュール（すべてJST）: collector = 毎日6:00〜23:00の1時間ごと / monitor = 毎日9:10〜23:10の2時間ごと / digest = 平日7:30（収集→分析→配信を直列実行）
- GitHub Actionsのscheduleは時刻保証がなく、遅延・スキップが頻発します。digestは配信直前に収集・分析を自前で行うため、発火が遅れても最新ニュースを取りこぼしません
- 手動実行: Actionsタブ → 各workflow → 「Run workflow」で動作確認可能

> **注意（プライベートリポジトリの場合）**: 無料枠は月2,000分。この頻度だと月3,000分超で不足します。
> リポジトリをpublicにする（Actionsが無制限無料）か、下記の代替を使ってください。
>
> **代替**: Webhookサーバーの `/jobs/collect`・`/jobs/monitor` を [cron-job.org](https://cron-job.org)（無料）から
> 5分ごとに叩く方式。`JOB_TRIGGER_TOKEN` を設定し、リクエストヘッダー `X-Job-Token: <トークン>` を付ける。
> この方式はRenderの無料インスタンスがスリープするのも防げます。

### 3. Render（Webhookサーバー）

1. [Render](https://render.com/) でこのリポジトリを Blueprint としてデプロイ（`render.yaml` が使われる）
2. 環境変数（`CLOUDFLARE_*`、`LINE_CHANNEL_SECRET` など）をダッシュボードで設定
3. 発行されたURLを控える（例: `https://japan-stock-signal-webhook.onrender.com`）

> Render無料プランは15分アイドルでスリープし、復帰に1分弱かかります。
> その間のLINE followイベントを取りこぼさないよう、LINE Developersの **Webhook再送** を有効にしてください。

### 4. LINE Webhook URL の登録

LINE Developers → チャネル → Messaging API タブ → Webhook URL に
`https://<RenderのURL>/webhook` を設定し、「Webhookの利用」をオンにする。

---

## ディレクトリ構成

```
.
├── collector/          # 調査役エージェント（RSS収集）
│   ├── agent.py
│   └── fetcher.py
├── monitor/            # 監視役エージェント（LLM分析 + LINE通知）
│   ├── agent.py
│   ├── analyzer.py
│   └── notifier.py
├── webhook/            # FastAPI Webhookサーバー
│   └── app.py
├── store/              # データ層（Cloudflare D1 / ローカルSQLite）
│   ├── db.py
│   ├── models.py
│   ├── repository.py
│   └── recipients.py
├── migrations/
│   └── 001_init.sql
├── .env.example
└── requirements.txt
```

---

## RSSフィード

| フィード | URL |
|---|---|
| Yahoo!ニュース ビジネス | https://news.yahoo.co.jp/rss/topics/business.xml |
| TDnet 適時開示 | https://webapi.yanoshin.jp/webapi/tdnet/list/recent.rss |
| NHK 経済 | https://www.nhk.or.jp/rss/news/cat6.xml |
