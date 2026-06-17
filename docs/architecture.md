# アーキテクチャ

> 技術スタックは稼働中の構成を記録したもの。判断理由は `docs/architecture-decisions/`（ADR）参照。

## 技術スタック

| レイヤー | 採用技術 |
|---------|---------|
| 言語 | Python 3.9 |
| 定期実行（収集・精査・通知） | GitHub Actions schedule（cron, UTC指定） |
| Webhookサーバー | FastAPI + Uvicorn（Render Free Web Service） |
| 管理画面 | FastAPI + Jinja2（サーバーサイドレンダリング、/admin） |
| DB | Cloudflare D1（SQLite互換、REST API）／ローカルはSQLite |
| データアクセス | 自前の薄いstore層（ORMなし、SQL直書き・`?`プレースホルダ） |
| LLM | Google Gemini API（既定 gemini-2.5-flash、管理画面で変更可。上位は要API課金） |
| 通知 | LINE Messaging API（Multicast）+ SMTP（分析レポートメール） |
| HTTP | httpx |

## 選定理由（要約）

- **Python**: RSS解析・LLM SDK・軽量Webが揃い、記述量が少ない
- **GitHub Actions schedule**: publicリポジトリは無料無制限。常設サーバー不要（→ ADR-002で遅延対策）
- **Cloudflare D1**: 無料・SQLite互換・REST APIでActionsから叩ける（→ ADR-001）
- **ORMなし**: テーブル3〜5個の小規模。SQL直書きで十分、D1/SQLite両対応も楽（→ ADR-004）
- **Render Free**: LINE Webhook受信のために常時起動のHTTPエンドポイントが要る部分だけ担当

## システム構成（3エージェント + ストア + Web）

```
[collector] 毎日6:00〜23:00 JST 1時間ごと（週末も）
  RSS収集 → 重複排除して articles に保存
       ↓ articles（未読）
[monitor]   毎日7:10〜23:10 JST 2時間ごと
  未読をGeminiバッチ分析 → pos/neg+銘柄特定+要約
  銘柄が特定できたpos/neg だけ signals に追加（全分析結果は article_analyses に保存）
       ↓ signals（未通知）
[digest]    平日7:30 JST（収集→分析→配信を直列実行 / ADR-002）
  未通知signalsを1通にまとめLINE配信 → notified_at を打つ
  ゼロ件の日も「該当なし」を送信

[webhook]   Render常時起動: LINE follow/unfollow→recipients管理、/admin管理画面
```

## ディレクトリ構成

```
japan-stock-signal/
├── collector/          # 収集役（RSS取得・保存）
│   ├── agent.py        #   エントリ: python -m collector.agent
│   └── fetcher.py      #   RSS取得（フィードはsettingsから読む）
├── monitor/            # 精査役・通知役
│   ├── agent.py        #   分析エントリ: python -m monitor.agent
│   ├── analyzer.py     #   Geminiバッチ分析
│   ├── digest.py       #   通知エントリ: python -m monitor.digest
│   ├── notifier.py     #   LINE送信
│   └── mailer.py       #   分析レポートメール
├── webhook/
│   └── app.py          # FastAPI（LINE Webhook + /admin マウント）
├── admin/              # 管理画面
│   ├── routes.py       #   /admin ルート（Cookie認証）
│   └── templates/      #   Jinja2テンプレート
├── store/              # データアクセス層
│   ├── db.py           #   D1/SQLite切替・execute・migrate
│   ├── repository.py   #   articles
│   ├── signals.py      #   signals
│   ├── analyses.py     #   article_analyses（分析結果・一覧照会）
│   ├── settings.py     #   抽出条件（settingsテーブル）
│   ├── recipients.py   #   LINE受信者
│   └── models.py       #   Article dataclass
├── migrations/         # *.sql（ファイル名順に冪等実行）
├── docs/               # ドキュメント（service-overview/architecture/specs/ADR）
├── .github/workflows/  # collector/monitor/digest の cron 定義
├── render.yaml         # Render Blueprint
├── Procfile            # uvicorn 起動定義
└── SPEC.md             # システム全体の基本設計
```

## デプロイ／実行環境

- **collector / monitor / digest**: GitHub Actions（cronはUTC指定。JST-9hに注意）
- **webhook / admin**: Render Free（`render.yaml` Blueprint、`uvicorn webhook.app:app`）
- **シークレット**: GitHub Secrets と Render 環境変数の両方に設定（`CLOUDFLARE_*`, `GEMINI_API_KEY`, `LINE_*`, `SMTP_*`, `ADMIN_TOKEN` 等）
- リモートは SSH（PATにworkflowスコープがなくHTTPSではworkflowファイルをpushできないため）
