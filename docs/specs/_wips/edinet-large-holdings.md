# WIP: EDINET 大量保有報告書アラート（需給シグナル・別レーン・メタデータv1）

> 状態: 設計中（未実装）。実装＋テスト完了でこのファイルを削除し、要点を SPEC.md へ統合する。
> 関連: メモリ [[news-source-evaluation]]。捕捉率FBの支配要因 `not_collected`（ニュース網の外）の回収策。

## 目的・背景

機関投資家/アクティビストが「ある銘柄を5%超取得」した大量保有報告書は、翌日以降に個人の
追随で出来高が動きやすい**需給シグナル**。TDnet（適時開示）にもニュースRSSにも出ないため
収集網の死角。EDINET公式API（金融庁）は **secCode（証券コード）が権威的に取れる**ため、
本システムの「コードはハルシネーションさせず権威ソースから確定する」設計と相性が良い。

ニュース→LLM精査の本体とは性質が違うので、**テクニカル版と同じ「別レーン」**で独立実装する
（本体ロジック不可侵）。

## ⚠️ 設計の前提補正（2026-06-26・実APIで検証）

当初案にあった以下は誤りだったため補正する:

1. **APIキーは必須**（「不要」は誤り）。実測:
   - v1 `https://disclosure.edinet-fsa.go.jp/api/v1/documents.json` → **HTTP 403（廃止）**
   - v2 キー無し → **HTTP 401 `invalid subscription key`**
   - ⇒ **v2 + 無料サブスクリプションキー必須**。エンドポイント `https://api.edinet-fsa.go.jp/api/v2/...`、
     ヘッダ `Ocp-Apim-Subscription-Key: <KEY>`。
2. **docTypeCode は 350/360**（当初の 120/130・130/140 はいずれも誤り）。EDINET公式コードリスト:
   - `120`=有価証券報告書 / `130`=訂正有報（＝**当初指定だと有報を拾ってしまう**）
   - **`350`=大量保有報告書 / `360`=変更報告書（大量保有）/ `370`=訂正報告書**
   - ※キー取得後に実データで `docTypeCode` を最終確認してから実装に入る。
3. **メタデータだけでは「方向」が出せない**。変更報告書(360)は買い増し(強気)も売却・撤退(弱気)も
   同じコード。保有割合(XBRL)を読まない限り強弱を判定できない ⇒ **v1 は方向なしの中立アラート**
   （sentiment は付けない／付けるなら neutral）。方向付けは Phase 2（XBRL/CSV）へ。

## データソース（検証済み + 公式仕様）

- 書類一覧API v2: `GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2`
  - ヘッダ `Ocp-Apim-Subscription-Key: <EDINET_API_KEY>`（必須）
  - `type=2` = メタデータ付き提出書類一覧（`results[]` が返る）
  - HTTPクライアントは **`httpx`**（既存規約。`requests` は使わない＝新規依存を増やさない）
- `results[]` の使用フィールド:
  - `docID`（書類ID・一意。重複排除キー）
  - `docTypeCode`（**350 / 360** を対象）
  - `secCode`（対象=発行会社の証券コード5桁。`[:4]` で4桁に正規化＝システム内コードと一致）
  - `filerName`（提出者=保有者。ファンド名。denylist用）
  - `submitDateTime` / `docDescription`
- 保有割合(%)はメタデータに無く、本体（`type=5` CSV 等）取得が必要 → **Phase 2**

## 統合方式

### Phase 1（メタデータのみ・XBRL一切なし・方向なし中立アラート）
新パッケージ `edinet/agent.py`（`technical/agent.py` を踏襲）:

1. `EDINET_API_KEY` を env から取得（無ければ warning ログのみで no-op＝既存に影響なし）
2. 当日＋前営業日の documents.json を取得（遅れ提出の取りこぼし防止。重複は docID で吸収）
3. `docTypeCode in (350,360)` かつ `secCode` ありを抽出、`secCode[:4]` に正規化
4. `filerName` がパッシブ/インデックス運用の **denylist**（settings）に部分一致するものを除外（silent禁止・ログ出力）
5. `large_holdings` に **docID 未通知のものだけ** を新規として LINE 配信（方向なし中立アラート）
   - 例: `📨 大量保有 6181 ○○HD｜提出: ××投資事業組合（新規/変更・割合未取得）\n https://disclosure.edinet-fsa.go.jp/...`
6. 結果を `edinet_runs` に記録（`technical_runs` と同型: status/件数/line_ok/error）

### Phase 2（割合・増減で方向付け）
350/360 の本体CSV(type=5)から「保有割合」「前回比増減」を抽出し、買い増し/売却の方向と
大口度を付与。これで初めて strong-buy/exit の sentiment が出せる（XBRL/CSVパースの重量級タスク）。

## スキーマ（migration 追加・追記のみ）

```sql
CREATE TABLE IF NOT EXISTS large_holdings (
    doc_id        TEXT PRIMARY KEY,         -- EDINET docID
    sec_code      TEXT,                     -- 対象銘柄4桁
    issuer_name   TEXT,
    filer_name    TEXT,                     -- 提出者(保有者)
    doc_type_code TEXT,                     -- 350 | 360
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

## 設定（settings・管理画面の「テクニカル設定」隣に追加）

- `edinet_enabled`（既定 "false"。キー設定後に有効化）
- `edinet_filer_denylist`（改行区切り。野村アセット/日興AM/ブラックロック/三菱UFJ信託 等の
  パッシブ運用名。部分一致で除外。silent禁止・ログ出力）

## ワークフロー

`.github/workflows/edinet.yml`：平日 寄り前（朝7:00台 JST）に1回、`workflow_dispatch` 併設。
前日提出分を寄り前に拾う。秘密は他cronと同じく GitHub Secrets / Render 両方に。

## シークレット（**ユーザー作業＝Claudeは取得不可**）

1. EDINET（https://disclosure.edinet-fsa.go.jp/）でアカウント登録 → APIキー発行（無料）
2. `EDINET_API_KEY` を **GitHub Secrets と Render 環境変数の両方**に設定
3. キー設定後、実データで `docTypeCode`（350/360）を最終確認してから Phase1 実装に入る

## テスト（FORCE_LOCAL_DB=1）

- documents.json レスポンスをモックし、350/360抽出・`secCode[:4]`正規化・denylist除外・docID重複排除・新規のみ通知を検証
- キー未設定時に no-op（例外を投げない）
- `large_holdings` の冪等性（同 docID 再実行で二重通知しない）

## リスク・留意

- 提出義務は「取得日から5営業日以内」＝**シグナルにラグ**（公表直後に動く需給として割り切る）
- パッシブ/指数組換えノイズ → denylist と Phase2の割合（大口の新規取得に絞る）で軽減
- **方向不明（v1）**：強弱を出さず「大量保有に動意」中立アラートに留める。directional は Phase2
- secCode が無い提出（非上場・投信・個人）は対象外
- レート制限はEDINET規約に従う（1日数リクエストなので問題になりにくい）

## 段階

- **Phase 1**: メタデータのみ→350/360抽出→docID重複排除→中立LINE（割合なし）。← まずここ（キー取得後）
- **Phase 2**: 本体CSVから保有割合・増減で方向付け（強気/弱気 sentiment）。
- **Phase 3（任意）**: 捕捉率FBに「EDINET経由で事前把握していたか」のチャンネルを合算。
