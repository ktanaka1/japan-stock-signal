# WIP: EDINET 大量保有報告書アラート（需給シグナル・別レーン）

> 状態: 設計中（未実装）。実装＋テスト完了でこのファイルを削除し、要点を SPEC.md へ統合する。
> 関連: メモリ [[news-source-evaluation]]。捕捉率FBの支配要因 `not_collected`（6/24で24件）の回収策。

## 目的・背景

アクティビスト/機関投資家が「ある銘柄を5%超取得」した大量保有報告書（変更報告含む）は、
翌日以降に個人の追随で出来高が爆発しやすい強い**需給シグナル**。TDnet（適時開示）にも
ニュースRSSにも出ないため、現状の収集網の死角になっている。EDINET公式API（金融庁）は
403にならず、**証券コード(secCode)が権威的に取れる**ため、本システムの「コードはハルシ
ネーションさせず権威ソースから確定する」設計（TDnet同様）と相性が良い。

ニュース→LLM精査の本体とは性質が違う（構造化された需給イベントで sentiment 判定は不要）。
よって**テクニカル版(`technical/`)と同じ「別レーン」**として独立実装する（本体ロジック不可侵）。

## データソース（検証済み事実 + 公式仕様）

- 書類一覧API v2: `GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2`
  - **要・無料APIキー**: ヘッダ `Ocp-Apim-Subscription-Key: <KEY>`（キー無しは200だが結果空＝検証済み）
  - `type=2` はメタデータ付き提出書類一覧
- `results[]` の主なフィールド（公式スキーマ）:
  - `docID`（書類ID・一意）／`docTypeCode`（**130=大量保有報告書 / 140=変更報告書**）
  - `secCode`（**対象=発行会社の証券コード5桁**。4桁＋チェック桁→先頭4桁に正規化）
  - `filerName`（**提出者=買い手**。アクティビスト/ファンド名）
  - `docDescription` / `submitDateTime` / `edinetCode`
- 保有割合(%)はメタデータに無く、書類本体（`type=5` CSV or `type=1` ZIP/XBRL）取得が必要 → **Phase 2**

## 統合方式

### Phase 1（まず出す・割合なし）
新パッケージ `edinet/agent.py`（`technical/agent.py` を踏襲）:

1. `EDINET_API_KEY` を env から取得（無ければ warning ログのみで no-op、既存に影響なし）
2. 対象日 = 当日 と 前営業日（遅れ提出の取りこぼし防止。重複は docID で吸収）の documents.json を取得
3. `docTypeCode in (130,140)` かつ `secCode` ありを抽出
4. `filerName` が**パッシブ/インデックス運用の除外リスト**（settings、後述）に一致するものを除外
5. `large_holdings` テーブルで **docID 未通知のものだけ** を新規として LINE 配信
   - 例: `📨 大量保有 6181 タメニー｜提出: ○○投資事業組合（新規/変更）\n https://disclosure.edinet-fsa.go.jp/...`
6. 結果を `edinet_runs` に記録（`technical_runs` と同じ思想：status/件数/line_ok/error）

### Phase 2（割合・増減を付与）
新規(130)/変更(140)の本体CSV(type=5)を取得し「保有割合」「前回比増減」を抽出して通知に付与。

## スキーマ（migration 追加・追記のみ）

```sql
-- 大量保有報告書の通知済み管理（docID で重複排除）
CREATE TABLE IF NOT EXISTS large_holdings (
    doc_id        TEXT PRIMARY KEY,         -- EDINET docID
    sec_code      TEXT,                     -- 対象銘柄4桁
    issuer_name   TEXT,
    filer_name    TEXT,                     -- 提出者(買い手)
    doc_type_code TEXT,                     -- 130 | 140
    submit_at     TEXT,
    holding_ratio REAL,                     -- Phase2。Phase1はNULL
    notified_at   TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS edinet_runs (   -- technical_runs と同型の実行記録
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL DEFAULT (datetime('now')),
    target_date TEXT NOT NULL, status TEXT NOT NULL,
    pick_count INTEGER DEFAULT 0, line_ok INTEGER DEFAULT 0, line_error INTEGER DEFAULT 0,
    picks TEXT, error_detail TEXT
);
```

## 設定（settings テーブル・管理画面）

- `edinet_enabled`（既定 "false"。キー設定後に有効化）
- `edinet_filer_denylist`（改行区切り。野村アセット/日興AM/ブラックロック/三菱UFJ信託 等の
  パッシブ運用名。部分一致で除外。silent除外を避けログ出力）

## ワークフロー

`.github/workflows/edinet.yml`：平日朝（本体digest 7:30 の前後）に1回。`workflow_dispatch` 併設。
EDINETは当日夜までに出るので、**寄り前（朝7:00台）実行で前日提出分を拾う**のが妥当。

## シークレット（**ユーザー作業**）

1. EDINET（https://disclosure.edinet-fsa.go.jp/）でアカウント登録 → APIキー発行（無料）
2. `EDINET_API_KEY` を **GitHub Secrets と Render 環境変数の両方**に設定

## テスト（FORCE_LOCAL_DB=1）

- documents.json レスポンスをモックし、130/140抽出・denylist除外・docID重複排除・新規のみ通知を検証
- キー未設定時に no-op（例外を投げない）
- `large_holdings` の冪等性（同 docID 再実行で二重通知しない）

## リスク・留意

- 提出義務は「取得日から5営業日以内」＝**シグナルにラグ**。それでも公表直後に出来高が動く
- パッシブ/指数組換えのノイズ → denylist と、Phase2の割合（大口の新規取得に絞る）で軽減
- secCode が無い提出（非上場・投信）は対象外
- レート制限はEDINET規約に従う（1日1回・少数リクエストなので問題になりにくい）

## 段階

- **Phase 1**: 取得→130/140抽出→docID重複排除→LINE（割合なし）。← まずここ
- **Phase 2**: 本体CSVから保有割合・増減を付与。
- **Phase 3（任意）**: 捕捉率FBに「EDINET経由で事前把握していたか」のチャンネルを合算（[[news-source-evaluation]] の2a拡張）。
