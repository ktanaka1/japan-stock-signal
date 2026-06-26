# WIP: EDINET 大量保有報告書アラート（需給シグナル・別レーン・メタデータv1）

> 状態: **Phase 1 実装＆テスト完了（2026-06-26）・要点はSPEC.mdへ反映済**。本ファイルは
> 残る **Phase 2/3（XBRL割合での方向付け 等）** の設計メモとして残置。Phase2完了時に削除する。
> 関連: メモリ [[news-source-evaluation]]。捕捉率FBの支配要因 `not_collected`（ニュース網の外）の回収策。

## 目的・背景

機関投資家/アクティビストが「ある銘柄を5%超取得」した大量保有報告書は、翌日以降に個人の
追随で出来高が動きやすい**需給シグナル**。TDnetにもニュースRSSにも出ない収集網の死角。
ニュース→LLM精査の本体とは性質が違うので、**テクニカル版と同じ別レーン**で独立実装する。

## ✅ ライブ検証で確定した事実（2026-06-26・実APIで取得）

当初案（secCode・120/130 等）は誤り。実データで以下に確定:

1. **APIキー必須**（v1=403廃止、v2=キー無し401）。`EDINET_API_KEY` は GitHub Secrets / ローカル.env に登録済。
   ヘッダ `Ocp-Apim-Subscription-Key`。一覧API: `GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2`。
2. **docTypeCode `350` が大量保有ファミリ**。`docDescription` で区別:
   - 「大量保有報告書」＝**新規**（初回5%超＝買い集めの含意で相対的に強い）
   - 「変更報告書」「変更報告書（短期大量譲渡）」＝**変更**（増減は不明）
   - `360`＝訂正報告書 → **スキップ**
3. **`secCode` は使えない**（提出者=保有者側のコードで大量保有では `null`）。
   **対象銘柄は `issuerEdinetCode`**（例 `E31280`）で取得する。
4. **`issuerEdinetCode` → 4桁証券コードは「EDINETコードリスト」で解決**:
   - DL: `https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip`
     （**キー不要**・約570KB・zip内 `EdinetcodeDlInfo.csv`・**cp932**・**2行目がヘッダ**・約11,356件）
   - 列に `ＥＤＩＮＥＴコード` / `証券コード`（5桁・末尾0）/ `提出者名`。`証券コード[:4]` で4桁化。
     例 `E00004 → 13760 → 1376`（カネコ種苗）。
   - **REIT/投資法人等は証券コードが空** → 解決不可としてスキップ（例 E31280=ヘルスケア＆メディカル投資法人）。
     中小型株デイトレ対象外なので実害なし。
5. **方向（買い/売り）はメタデータでは出せない**（350に新規も変更も混在）。
   v1は方向なしの**中立アラート**。docDescriptionで「新規/変更」タグだけ付ける。方向付けはPhase2（XBRL/CSVの保有割合）。

## 統合方式（Phase 1・XBRLパース無し）

新パッケージ `edinet/agent.py`（`technical/agent.py` を踏襲）:

1. `EDINET_API_KEY` を env から取得（無ければ warning ログのみで no-op＝既存に影響なし）
2. **EDINETコードリスト**を取得しキャッシュ（`edinetCode → (証券コード4桁, 提出者名)`）。
   refresh: 実行のたびにDLしてメモリ展開（約570KBなので軽い。日次cron実行なら毎回でも可）
3. `documents.json`（当日＋前営業日。遅れ提出の取りこぼし防止、重複は docID で吸収）を取得
4. `docTypeCode == "350"` を抽出（360=訂正は除外）
5. `issuerEdinetCode` をコードリストで4桁に解決。**空（REIT/非上場/解決不可）はスキップ**
6. `filerName` がパッシブ/インデックス運用の **denylist**（settings）に部分一致→除外（silent禁止・ログ）
7. `large_holdings` に **docID 未通知のものだけ** を新規として LINE 配信（方向なし中立・新規/変更タグ付き）
   - 例: `📨 大量保有[新規] 1376 カネコ種苗｜提出: ××投資事業組合\n https://disclosure2.edinet-fsa.go.jp/...（docID）`
8. 結果を `edinet_runs` に記録（status/件数/line_ok/error）

### Phase 2（方向付け）
350 の本体CSV(type=5)から保有割合・前回比増減を抽出し、買い増し/売却の方向と大口度を付与
（ここで初めて sentiment が出せる。XBRL/CSVパースの重量級）。

## スキーマ（migration 追加・追記のみ）

```sql
CREATE TABLE IF NOT EXISTS large_holdings (
    doc_id        TEXT PRIMARY KEY,         -- EDINET docID
    sec_code      TEXT,                     -- 対象銘柄4桁（issuerEdinetCodeから解決）
    issuer_edinet_code TEXT,                -- 解決元（監査用）
    issuer_name   TEXT,                     -- 対象銘柄名（コードリスト由来）
    filer_name    TEXT,                     -- 提出者(保有者)
    doc_description TEXT,                    -- 新規/変更の区別
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
- （任意）`edinet_new_only`（"true"で「大量保有報告書（新規）」のみ配信し変更を抑制）

## ワークフロー

`.github/workflows/edinet.yml`：平日 寄り前（朝7:00台 JST）に1回、`workflow_dispatch` 併設。
秘密は他cronと同様 GitHub Secrets / Render 両方（`EDINET_API_KEY`）。

## テスト（FORCE_LOCAL_DB=1）

- documents.json レスポンスをモックし、350抽出・360除外・issuerEdinetCode解決・REIT(空)スキップ・
  denylist除外・docID重複排除・新規のみ通知・新規/変更タグを検証
- コードリスト解決の単体（edinetCode→4桁、空はスキップ）
- キー未設定時に no-op（例外を投げない）

## リスク・留意

- **新規依存：EDINETコードリストの取得・解決**（ただし authoritative・名寄せ不要・約570KB と軽量。
  XBRLパースは一切なし）。コードリストDL失敗時は解決できないので no-op + ログ
- 提出義務は「取得日から5営業日以内」＝**シグナルにラグ**
- パッシブ/指数組換えノイズ → denylist と Phase2の割合で軽減
- REIT/投資法人/非上場（証券コード空）は対象外
- **方向不明（v1）**：強弱は出さず「大量保有に動意」中立アラート。directional は Phase2

## 段階

- **Phase 1**: 350抽出→issuerEdinetCode解決→docID重複排除→中立LINE（新規/変更タグ）。← まずここ
- **Phase 2**: 本体CSVから保有割合・増減で方向付け（強気/弱気 sentiment）。
- **Phase 3（任意）**: 捕捉率FBに「EDINET経由で事前把握していたか」のチャンネルを合算。
