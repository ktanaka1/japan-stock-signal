# Japan Stock Signal

日本株の関連ニュースをRSSで収集し、LLMで分析してLINEに通知するシステム。

## 機能

- 市場時間中（9:00〜15:30）にRSSフィードを定期収集
- Google Gemini でニュースをポジティブ/ネガティブ/中立に分類し、関連銘柄を特定
- LINEボットをフォローした人全員に自動通知
- フォロー/アンフォローで受信者を自己管理

## 通知フォーマット

```
📰 トヨタ自動車の営業利益が過去最高を更新しました。
✅ ポジティブ
🎯 関連銘柄：トヨタ自動車(7203)
🔗 https://...
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
| `DB_PATH` | 変更不要 | SQLiteファイルのパス（デフォルト: `data/stock_signal.db`） |

---

## 実行

### 調査役（RSS収集）

```bash
python -m collector.agent
```

### 監視役（LLM分析 + LINE通知）

```bash
python -m monitor.agent
```

動作確認（APIキーなし）:

```bash
DRY_RUN=true python -m monitor.agent
```

### Webhookサーバー（LINE フォロー/アンフォロー受信）

```bash
uvicorn webhook.app:app --host 0.0.0.0 --port 8000
```

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
├── store/              # データ層（SQLite）
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
